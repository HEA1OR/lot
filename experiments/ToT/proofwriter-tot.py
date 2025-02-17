# Proofwriter with ToT
import requests
import guidance
import ast
import argparse
import json
import time
import numpy
from tqdm import tqdm
import torch
from string import Template
import os
import re
import random
import queue
from transformers import GPT2TokenizerFast
_TOKENIZER = GPT2TokenizerFast.from_pretrained("gpt2", cache_dir="")


os.environ["OPENAI_API_KEY"] = ''
from proofwriter_prompt import *

TRY_CNT = 16


def strip_markers(s):
    """This strips out the comment markers used by guidance."""
    if s is None:
        return None
    return re.sub(r"{{!--G.*?--}}", r"", s, flags=re.MULTILINE | re.DOTALL)

def get_tokenizer(text, tokens_num):
    tokens = len(_TOKENIZER.tokenize(strip_markers(text._text))) + tokens_num
    print(tokens)
    return tokens


# this code is only for tot.
def get_parser():
    parser = argparse.ArgumentParser(description="Tree of Thought")
    parser.add_argument('--temperature', type=float, default=0.1, help='temperature')
    parser.add_argument('--propnum', type=int, choices=range(0, 21), default=4, help='numbers of props')
    parser.add_argument('--reasoningnum', type=int, choices=range(0, 21), default=4,
                        help='numbers of reasoning, when > 1, majority voting is used')
    parser.add_argument('--choices', type=int, choices=range(0, 21), default=4, help='numbers of premises to be chosen')
    parser.add_argument('--trycnt', type=int, choices=range(1, 1001), default=16, help='numbers of try times')
    parser.add_argument('--exploration_prob', type=float, default=1.00, help='exploration probability')
    parser.add_argument('--min_score', type=float, default=0.5, help='min score')
    parser.add_argument('--verified_reasoning', type=ast.literal_eval, default=False,
                        help='self verified reasoning')
    parser.add_argument('--model', type=str, default='gpt-4', help='model to use')
    parser.add_argument('--dataset', type=str, default='/data/pw/json/dev100.json', help='dataset to use')
    parser.add_argument('--verbose', type=ast.literal_eval, default=True, help='verbose mode')
    parser.add_argument('--con_select', default=False, help='random or prompt')
    parser.add_argument('--memory', default=False, help='use memory or not')
    parser.add_argument('--infer_history', default=False, help='use FOL or not')
    parser.add_argument('--useful_judgement', default=False, help='the forth judgement')
    parser.add_argument('--tot', default=True, help='tot baseline')
    parser.add_argument('--bfs', type=int, default=5, help='tot bfs')
    parser.add_argument('--global_validation', default=False, help='use validation global or not')
    parser.add_argument('--condition_divide', default=False, help='the forth judgement')
    return parser


parser = get_parser()
args = parser.parse_args()

if 'llama' in args.model:
    guidance.llm = guidance.llms.transformers.LLaMA(args.model, device_map="auto", token_healing=True,
                                                    torch_dtype=torch.bfloat16, caching=False)
else:
    guidance.llm = guidance.llms.OpenAI(args.model)


def main():
    tokens_sum = 0
    # Load the data from the JSON file
    with open(args.dataset, 'r', encoding='utf-8') as file:
        data = json.load(file)
    for item in data:
        conclusion = item['conclusion']
        clauses = item['context'].split('.')
        results = re.search(r'If (.*), (?:then )(.*)(?=$)', conclusion, re.S)
        item['premises'] = [clause.strip() + '.' for clause in clauses if clause.strip()]
        if results:
            premise = results.group(1)
            hypothesis = results.group(2)
            item['conclusion'] = hypothesis
            item['premises'].append(premise)
        else:
            item['conclusion'] = conclusion
        if item['answer'] == True:
            item['label'] = 'True'
        if item['answer'] == False:
            item['label'] = 'False'
        item['example_id'] = item['id']
        del item['id']
        del item['answer']
        del item['context']

    t = time.localtime()

    
    dataset_name = args.dataset.split('/')[2].split('.')[0]
    model_name = args.model.replace('/', '-')
    logfilename = model_name + '--t' + str(
        args.temperature) + '--' + dataset_name + '--n_' + str(args.propnum) + '--' + time.strftime("%Y-%m-%d-%H-%M-%S",
                                                                                                    t) + '.jsonl'
    with open(logfilename, 'w') as f:
        f.write(time.strftime("%Y-%m-%d %H:%M:%S", t) + '\n')  # write each result as a new line
        f.write('propnum: ' + str(args.propnum) + '\n')
        f.write('reasnoningnum: ' + str(args.reasoningnum) + '\n')
        f.write('choices: ' + str(args.choices) + '\n')
        f.write('exploration_prob: ' + str(args.exploration_prob) + '\n')
        f.write('trycnt: ' + str(args.trycnt) + '\n')
        f.write("Model: " + args.model + "\n")
        f.write("Temperature: " + str(args.temperature) + "\n")
        f.write("Dataset: " + args.dataset + "\n")
        f.write("condition filter:" + str(args.con_select) + "\n")
        f.write("memory:" + str(args.memory) + "\n")
        f.write("infer_history:" + str(args.infer_history) + "\n")
        f.write("useful_judgment:" + str(args.useful_judgement) + "\n")
        f.write("--------------------------------\n")

    # Initialize counter for correct predictions
    correct_predictions = 0
    cnt = 0
    total_nodes = 0
    total_cnt = len(data)

    # Iterate over the data from the JSON file and call the solve function
    for example in tqdm(data, desc="Evaluating", unit="example"):
        tokens = 0
        cnt += 1
        print("-------------------------\n### Example ID: ", example['example_id'], "\t ( ", cnt, "/", total_cnt, " )")
        conclusion = example['conclusion']
        premises = example['premises']
        memory = []
        propositions = []
        que = queue.Queue()
        determinate_premise = []
        indeterminate_premise = []
        Last_infer_history = "There's no Last_reasoning_history yet, because this is the first derivation."
        last_relevant_premise = " "
        last_prop = " "
        infer_history = []
        failed_cnt = 0
        logs = []
        if args.verbose: print("[Premises]: \t", premises)
        if args.verbose: print("[Hypothesis]: \t", conclusion)

        # memory use
        if args.condition_divide:
            for premise in premises:
                if "If" in premise:
                    print("indeterminate_premise:", premise)
                    indeterminate_premise.append(premise)
                    continue
                if "either" in premise:
                    print("indeterminate_premise:", premise)
                    indeterminate_premise.append(premise)
                    continue
                try_cnt = 0
                while try_cnt < TRY_CNT:
                    try:
                        judgement_token = \
                            useful_deduction(examples=useful_deduction_examples, Premise=premise,
                                             conclusion=conclusion,temperature=args.temperature,
                                             valid_validation=premise_divide_judgement)
                        tokens = get_tokenizer(judgement_token, tokens)
                        judgement = judgement_token['usefulness']
                        break
                    except Exception as e:
                        print("validate_deduction() local failed, try again... (No. {})".format(try_cnt + 1), "Error:",
                              e)
                        try_cnt += 1
                        time.sleep(min(100, 2 ** (try_cnt / 2)))
                        continue
                if judgement == 'True':
                    memory.append(premise)
                    print("determinate_premise:", premise)
                    determinate_premise.append(premise)
                else:
                    print("indeterminate_premise:", premise)
                    indeterminate_premise.append(premise)
        # logs.append({"determinate_premise:", ' '.join(determinate_premise)})
        # logs.append({"indeterminate_premise:", ' '.join(indeterminate_premise)})
        flag = True
        visited_nodes = 0
        deter_num = 0
        indeter_num = 0
        while (len(propositions) < args.propnum and failed_cnt < args.trycnt ):  
            failed_cnt += 1

            if args.verbose: print("\t# <No. {}>".format(len(propositions) + 1))

            if args.con_select:
                # # args.exploration_prob determines the probability of using premises + propositions as the input of gen_proposition
                if failed_cnt >= (args.trycnt / 4):
                    if numpy.random.rand() < args.exploration_prob:  
                        tmp = numpy.random.choice(premises + propositions,
                                                  size=min(len(premises + propositions), args.choices), replace=False)
                    else:
                        tmp = numpy.random.choice(premises, size=min(len(premises), args.choices), replace=False)

                # # args.exploration_prob determines the probability of using premises + propositions as the input of gen_proposition
                
                if failed_cnt < (args.trycnt / 4):
                    try_cnt = 0
                    while try_cnt < TRY_CNT:  
                        try:
                            if args.useful_judgement is not True:  
                                tmp = condition_select_score_1(examples=conditions_scores_examples,
                                                               last_history=Last_infer_history,
                                                               determinate_premise=' '.join(determinate_premise),
                                                               indeterminate_premise=' '.join(
                                                                   premises),
                                                               Hypothesis=conclusion, temperature=args.temperature)
                                tokens = get_tokenizer(tmp, tokens)
                            else:
                                if len(propositions) == 0:
                                    tmp = condition_select_score_1(examples=conditions_scores_examples,
                                                                   last_history=Last_infer_history,
                                                                   determinate_premise=' '.join(determinate_premise),
                                                                   indeterminate_premise=' '.join(
                                                                       indeterminate_premise),
                                                                   Hypothesis=conclusion, temperature=args.temperature)
                                    tokens = get_tokenizer(tmp, tokens)
                                    print("[Last infer]:", Last_infer_history)
                                    print("[Most_relevant_premise]:", tmp['Most_relevant_premise'])
                                    last_relevant_premise = tmp['Most_relevant_premise']
                                else:
                                    if "New Proposition" in Last_infer_history:
                                        tmp = condition_select_score_3(examples=conditions_scores_examples_3,
                                                                       determinate_premise=' '.join(determinate_premise),
                                                                       indeterminate_premise=' '.join(
                                                                       indeterminate_premise),
                                                                       New_proposition=prop,
                                                                       last_history=Last_infer_history,
                                                                       Hypothesis=conclusion, temperature=args.temperature)
                                        tokens = get_tokenizer(tmp, tokens)
                                        print("[Last infer]:", Last_infer_history)
                                        print("[Most_relevant_premise]:", prop)
                                        last_relevant_premise = prop
                                    else:
                                        tmp = condition_select_score_2(examples=conditions_scores_examples_2,
                                                                       determinate_premise=' '.join(
                                                                           determinate_premise),
                                                                       Last_Most_relevant_premise=last_relevant_premise,
                                                                       indeterminate_premise=' '.join(
                                                                           indeterminate_premise),
                                                                       last_history=Last_infer_history,
                                                                       Hypothesis=conclusion,
                                                                       temperature=args.temperature)
                                        tokens = get_tokenizer(tmp, tokens)
                                        print("[Last infer]:", Last_infer_history)
                                        print("[Most_relevant_premise]:", tmp['Most_relevant_premise'])
                                        last_relevant_premise = tmp['Most_relevant_premise']

                                print("[Other_premises_scores]:", tmp['Other_premises_scores'])
                                tmp = tmp['results'].strip()
                            break
                        except Exception as e:
                            print("gen_proposition() failed, try again... (No. {})".format(try_cnt + 1), "Error:", e)
                            try_cnt += 1
                            time.sleep(min(1, 2 ** (try_cnt / 2)))
                            continue
            else:
                if args.tot:
                    tmp = []
                    for i in range(0, args.bfs):
                        if numpy.random.rand() < args.exploration_prob:  
                            tmp.append(numpy.random.choice(premises + propositions,
                                                      size=min(len(premises + propositions), args.choices),
                                                      replace=False))
                        else:
                            tmp.append(numpy.random.choice(premises, size=min(len(premises), args.choices), replace=False))
            my_tmp = tmp

            # generate propositions
            if args.tot:
                for i in range(0, args.bfs):

                    visited_nodes += 1
                    # print("filtered conditions:", my_tmp[i])
                    try_cnt = 0
                    while try_cnt < TRY_CNT:  
                        try:
                            t = gen_proposition(examples=gen_proposition_examples, premises=' '.join(my_tmp[i]),
                                                     conclusion=conclusion, temperature=args.temperature)
                            tokens = get_tokenizer(t, tokens)
                            break
                        except Exception as e:
                            print("gen_proposition() failed, try again... (No. {})".format(try_cnt + 1), "Error:", e)
                            try_cnt += 1
                            time.sleep(min(100, 2 ** (try_cnt / 2)))
                            continue
                    prop = t['proposition'].strip()
                    if 'Proposition\": \"' in prop:
                        prop = prop.split('Proposition\": \"')[1].split('\"')[0]
                    # if the first char of prop is ", then remove it
                    if len(prop) > 0 and prop[0] == '"':
                        prop = prop[1:]
                    # if the last char of prop is ", then remove it
                    if len(prop) > 0 and prop[-1] == '"':
                        prop = prop[:-1]

                    if prop in premises or prop in propositions:  
                        if args.verbose:
                            Last_infer_history = "In the last round,we use this \"most relevant premise\": \"" + last_relevant_premise + "\"" + "and got a \"false Proposition\": \"" + prop + "\""
                            print("\t[Raw propositions]\t", prop)
                            print("\t\t[Is not duplicated]:\t", 'False (literally)')
                        continue

                    if args.verbose:
                        print("\t[Raw propositions]\t", prop)
                        print("\t\t[Deduced from Premises]:\t", tmp[i])

                    # is something to be deduced
                    is_something_selection = 'False'  
                    # if prop begin with 'There is no' or 'No valid' or 'None of the' , then skip
                    if prop.startswith('There is no') or prop.startswith('there is no') or prop.startswith('There are no') or prop.startswith(
                            'No valid') or prop.startswith(
                            'None of the') or 'no information' in prop or 'No information' in prop or 'No direct' in prop or 'No proposition' in prop or 'It is not possible to' in prop or 'the correctness of the hypothesis' in prop or 'new Proposition' in prop or 'new proposition' in prop:
                        if args.verbose:
                            Last_infer_history = "In the last round,we use this \"most relevant premise\": \"" + last_relevant_premise + "\"" + "and got a \"false Proposition\": \"" + prop + "\""
                            print("\t\t[Deduced something]:\t", is_something_selection)
                        continue


                    # soucred deduction 
                    try_cnt = 0
                    while try_cnt < TRY_CNT:
                        try:
                            sourced_local_token = sourced_deduction(examples=sourced_deduction_examples, 
                                                                 premises=' '.join(tmp[i]), proposition=prop, 
                                                                 temperature=args.temperature)
                            tokens = get_tokenizer(sourced_local_token, tokens)
                            sourced_local = sourced_local_token['sourced']
                            
                            break
                        except Exception as e:
                            print("sourced_deduction() local failed, try again... (No. {})".format(try_cnt + 1), "Error:", e)
                            try_cnt += 1
                            time.sleep(min(100, 2 ** (try_cnt / 2)))
                            continue

                    if args.verbose: print("\t\t[Sourced local]:\t", sourced_local)
                    if sourced_local == 'False':
                        Last_infer_history = "In the last round,we use this \"most relevant premise\": \"" + last_relevant_premise + "\"" + "and got a \"false Proposition\": \"" + prop + "\""
                        continue

                    # validate propositions 
                    try_cnt = 0
                    while try_cnt < TRY_CNT:
                        try:
                            validation_local_token = \
                            validate_deduction(examples=validate_deduction_examples, premises=' '.join(tmp[i]), proposition=prop,
                                               temperature=args.temperature)
                            tokens = get_tokenizer(validation_local_token, tokens)
                            validation_local = validation_local_token['validation']
                            break
                        except Exception as e:
                            print("validate_deduction() local failed, try again... (No. {})".format(try_cnt + 1), "Error:", e)
                            try_cnt += 1
                            time.sleep(min(100, 2 ** (try_cnt / 2)))
                            continue

                    if args.verbose: print("\t\t[Validation local]:\t", validation_local)
                    if validation_local == 'False':
                        Last_infer_history = "In the last round,we use this \"most relevant premise\": \"" + last_relevant_premise + "\"" + "and got a \"false Proposition\": \"" + prop + "\""
                        continue

                    if args.global_validation:
                        try_cnt = 0
                        while try_cnt < TRY_CNT:
                            try:
                                validation_global_token = \
                                validate_deduction(examples=validate_deduction_examples, premises=' '.join(premises + propositions),
                                                   proposition=prop, temperature=args.temperature)
                                tokens = get_tokenizer(validation_global_token, tokens)
                                validation_global = validation_global_token['validation']
                                break
                            except Exception as e:
                                print("validate_deduction() global failed, try again... (No. {})".format(try_cnt + 1), "Error:", e)
                                try_cnt += 1
                                time.sleep(min(100, 2 ** (try_cnt / 2)))
                                continue

                        if args.verbose: print("\t\t[Validation global]:\t", validation_global)
                        if validation_global == 'False':
                            Last_infer_history = "In the last round,we use this \"most relevant premise\": \"" + last_relevant_premise + "\"" + "and got a \"false Proposition\": \"" + prop + "\""
                            continue

                    
                    # ALL test passed
                    if args.verbose: print("\t\t<All Test Passed>: \t", prop)
                    que.put(prop)
                    last_prop = prop
                    infer_history.append(
                        "In the NO:{} round,".format(len(propositions)) + " we use these \"premises\": \"" + ' '.join(
                            tmp[i]) + "\"" + "and got a \"New Proposition\": \"" + prop + "\"\n")
                    Last_infer_history = "In the last round,we use this \"most relevant premise\": \"" + last_relevant_premise + "\"" + "and got a \"New Proposition\": \"" + prop + "\""
                    Last_relevant_premise = last_relevant_premise 
            # put the propositions into queue for bfs
            if que.empty():
                break
            else:
                propositions.append(que.get())
            if len(propositions) == args.propnum:
                break
            failed_cnt = 0

        
        if args.verbose: print("[Generated Propositions]: \t", propositions)

        reasoning_num = 0
        reasoning_try_cnt = 0
        judgement_cnt = {"True": 0, "False": 0}
        reasoning_list = []
        while (reasoning_num < args.reasoningnum and reasoning_try_cnt < args.trycnt / 4):
            reasoning_try_cnt += 1
            try_cnt = 0
            my_premises = premises.copy()
            if (reasoning_try_cnt > 0): numpy.random.shuffle(my_premises)
            while try_cnt < TRY_CNT:
                try:

                    t = 0 if args.reasoningnum <= 1 else args.temperature
                    if args.memory:
                        out = structure_program_memory(
                            examples=examples,
                            premises=' '.join(my_premises),
                            propositions=' '.join(propositions),
                            memory=' '.join(determinate_premise),
                            infer_history=infer_history,
                            conclusion=conclusion,
                            temperature=t,
                        )
                        tokens = get_tokenizer(out, tokens)
                    else:
                        out = structure_program(
                            examples=examples,
                            premises=' '.join(my_premises),
                            propositions=' '.join(propositions),
                            conclusion=conclusion,
                            temperature=t
                        )
                        tokens = get_tokenizer(out, tokens)
                    if args.verbose:  # print [Reasoning No. reasoning_num]

                        print("\t[Reasoning <No. {}>]:\t".format(reasoning_num + 1), out["reasoning"])
                    break
                except Exception as e:
                    print("structure_program() failed, try again... (No. {})".format(try_cnt + 1), "Error:", e)
                    try_cnt += 1
                    time.sleep(min(100, 2 ** (try_cnt / 2)))
                    continue

            if args.verified_reasoning == True:
                try_cnt = 0
                while try_cnt < TRY_CNT:
                    try:
                        verified_reasoning_token = validate_deduction(examples=validate_deduction_examples,
                                                                premises=' '.join(premises + propositions),
                                                                proposition=out["reasoning"],
                                                                temperature=args.temperature)
                        tokens = get_tokenizer(verified_reasoning_token, tokens)
                        verified_reasoning = verified_reasoning_token['validation']
                        break
                    except Exception as e:
                        print("validate_deduction() reasoning failed, try again... (No. {})".format(try_cnt + 1),
                              "Error:", e)
                        try_cnt += 1
                        time.sleep(min(100, 2 ** (try_cnt / 2)))
                        continue

                if args.verbose: print("\t\t[Verified reasoning]:\t", verified_reasoning)
                if verified_reasoning == 'False':
                    continue

            reasoning_num += 1
            reasoning_list.append(out["reasoning"])
            if out["judgement"] in judgement_cnt:
                judgement_cnt[out["judgement"]] += 1
            else:
                judgement_cnt["False"] += 1
            reasoning_try_cnt = 0

        if args.reasoningnum == 0:
            try_cnt = 0
            while try_cnt < TRY_CNT:
                try:
                    t = 0 if args.reasoningnum <= 1 else args.temperature
                    out = structure_program_wocot(
                        examples=examples,
                        premises=' '.join(premises),
                        propositions=' '.join(propositions),
                        conclusion=conclusion,
                        temperature=t
                    )
                    tokens = get_tokenizer(out, tokens)
                    break
                except Exception as e:
                    print("structure_program() failed, try again... (No. {})".format(try_cnt + 1), "Error:", e)
                    try_cnt += 1
                    time.sleep(min(100, 2 ** (try_cnt / 2)))
                    continue
            if out["judgement"] in judgement_cnt:
                judgement_cnt[out["judgement"]] += 1
            else:
                judgement_cnt["False"] += 1

        # select the one with the highest count
        majority_judgement = max(judgement_cnt, key=judgement_cnt.get)

        # calculate the number of correct predictions
        if majority_judgement == example["label"]:
            correct_predictions += 1

        print("[Prediction]: ", majority_judgement)
        print("[Actual]: ", example["label"])

        # Calculate and print the running accuracy
        accuracy = correct_predictions / cnt
        total_nodes += visited_nodes
        print("[Running Average Accuracy]: ", accuracy)
        print("[Average visited nodes]:", total_nodes/cnt)
        tokens_sum += tokens
        result = {
            "example_id": example["example_id"],
            "prediction": out["judgement"],
            "actual": example["label"],
            "accuracy": accuracy,
            "determinate_premise:": ' '.join(determinate_premise),
            "indeterminate_premise:": ' '.join(indeterminate_premise),
            "conclusion": conclusion,
            "generated_propositions": propositions,
            "reasoning": reasoning_list,
            "reasoning history": infer_history,
            "visited nodes": total_nodes/cnt,
            "tokens": tokens,
            "tokens_sum": tokens_sum
        }
        
        with open(logfilename, 'a') as f:
            # Use json.dump() with indent=4 to write with indentation
            json.dump(result, f, indent=4)
            f.write('\n')  # Add a newline to separate each result


if __name__ == "__main__":
    main()
