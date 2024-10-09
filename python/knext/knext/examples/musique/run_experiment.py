# Copyright 2023 OpenSPG Authors
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may not use this file except
# in compliance with the License. You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software distributed under the License
# is distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express
# or implied.

import sys
import functools
import json
from pathlib import Path

import os
import json
import logging
import asyncio
import random

# logging.basicConfig(level=logging.DEBUG)

from knext.ca.common.base import Question
from knext.ca.tools.info_processor import LoggerIntermediateProcessTool
from knext.ca.runner.parallel_inference_runner import ParallelInferenceRunner
from knext.ca.logic.modules.solver import SolveQuestionWithContext
from knext.ca.common.client import get_llm_client, get_emb_client
from knext.ca.common.utils import logger
from info_retriver import SPOInfoRetriverWithTripleExtractor, TextInfoRetriver
from io_utils import MusiqueDataset, generate_submission_file
from musique_agent import (
    MusiqueSolveQuestionWithSPO,
    MusiqueSolveQuestionWithText,
    HierarchicalSolveQuestion,
    MusiqueDivideAndConquerAgent,
)

CWD = os.path.dirname(os.path.abspath(__file__))
WORKSPACE = os.path.join(CWD, "workspace")
if not os.path.exists(WORKSPACE):
    os.makedirs(WORKSPACE)
DATA_DIR = "MUSIQUE DATA DIR"


def get_official_deepseek_llm():
    client_config = {
        "model_name": "deepseek-chat",
        "base_url": "https://api.deepseek.com",
        "api_key": "API-KEY",
    }

    llm = get_llm_client(
        client_name="openai",
        client_config=client_config,
    )
    return llm


def get_official_aliyun_embed_fn():
    return None


def create_musique_spo_info_tools(
    musique_data, get_llm_fn, get_embed_fn, prompt_template_dir, intermediate_dir
):
    spo_retriver = SPOInfoRetriverWithTripleExtractor(
        musique_dataset=musique_data,
        llm=get_llm_fn(),
        embedding_fn=get_embed_fn(),
        prompt_template_dir=prompt_template_dir,
        intermediate_dir=intermediate_dir,
    )
    return spo_retriver


def create_musique_hierarchical_info_tools(
    musique_data, get_llm_fn, get_embed_fn, prompt_template_dir, intermediate_dir
):
    info_tools = []
    spo_retriver = SPOInfoRetriverWithTripleExtractor(
        musique_dataset=musique_data,
        llm=get_llm_fn(),
        embedding_fn=get_embed_fn(),
        prompt_template_dir=prompt_template_dir,
        intermediate_dir=intermediate_dir,
    )
    info_tools.append(spo_retriver)

    text_retriver = TextInfoRetriver(
        musique_dataset=musique_data,
        llm=get_llm_fn(),
        embedding_fn=get_embed_fn(),
        top_k=3,
    )
    info_tools.append(text_retriver)
    return info_tools


def create_divide_and_conquer_agent_with_spo_info_tool(
    musique_data,
    get_llm_fn,
    get_embed_fn,
    prompt_template_dir,
    intermediate_dir,
    debug_mode,
):
    # spo answer
    llm = get_llm_fn()

    spo_info_tool = create_musique_spo_info_tools(
        musique_data=musique_data,
        get_llm_fn=get_llm_fn,
        get_embed_fn=get_embed_fn,
        prompt_template_dir=prompt_template_dir,
        intermediate_dir=intermediate_dir,
    )

    answer_parent_question = SolveQuestionWithContext(
        llm_module=llm,
        use_default_prompt_template=False,
        prompt_template_dir=prompt_template_dir,
    )

    atom_answer_question = MusiqueSolveQuestionWithSPO(
        llm_module=llm,
        spo_info_tool=spo_info_tool,
        prompt_template_dir=prompt_template_dir,
    )

    agent = MusiqueDivideAndConquerAgent(
        llm=llm,
        answer_parent_question=answer_parent_question,
        answer_atom_question=atom_answer_question,
        prompt_template_dir=prompt_template_dir,
        debug_mode=debug_mode,
    )
    return agent


def create_divide_and_conquer_agent_hierarchical_info_tool(
    musique_data,
    get_llm_fn,
    get_embed_fn,
    prompt_template_dir,
    intermediate_dir,
    debug_mode,
):
    # Hierarchical answer
    llm = get_llm_fn()

    info_tools = create_musique_hierarchical_info_tools(
        musique_data=musique_data,
        get_llm_fn=get_llm_fn,
        get_embed_fn=get_embed_fn,
        prompt_template_dir=prompt_template_dir,
        intermediate_dir=intermediate_dir,
    )

    answer_parent_question = SolveQuestionWithContext(
        llm_module=llm,
        use_default_prompt_template=True,
        prompt_template_dir=prompt_template_dir,
    )

    answer_with_spo = MusiqueSolveQuestionWithSPO(
        llm_module=llm,
        spo_info_tool=info_tools[0],
        prompt_template_dir=prompt_template_dir,
    )

    answer_with_text = MusiqueSolveQuestionWithText(
        llm_module=llm,
        text_info_tool=info_tools[1],
        prompt_template_dir=prompt_template_dir,
    )

    atom_answer_question = HierarchicalSolveQuestion(
        llm_module=llm,
        answer_with_spo=answer_with_spo,
        answer_with_text=answer_with_text,
    )

    agent = MusiqueDivideAndConquerAgent(
        llm=llm,
        answer_parent_question=answer_parent_question,
        answer_atom_question=atom_answer_question,
        prompt_template_dir=prompt_template_dir,
        debug_mode=debug_mode,
    )
    return agent


def run_experiment_impl(
    create_agent_fn,
    get_llm_fn,
    get_embed_fn,
    data_dir,
    data_version,
    data_tag,
    prompt_tag,
    intermediate_tag,
    submit_tag,
    parallel_num=8,
    debug_mode=False,
    continue_run=True,
    seed=9527,
):
    prompt_template_dir = os.path.join(WORKSPACE, "prompt_template", prompt_tag)
    if not os.path.exists(prompt_template_dir):
        os.makedirs(prompt_template_dir)

    intermediate_dir = os.path.join(WORKSPACE, "intermediate", intermediate_tag)
    if not os.path.exists(intermediate_dir):
        os.makedirs(intermediate_dir)

    submit_dir = os.path.join(WORKSPACE, "submit", submit_tag)
    if not os.path.exists(submit_dir):
        os.makedirs(submit_dir)

    musique_data = MusiqueDataset(
        data_dir=data_dir, version=data_version, tag=data_tag, debug_mode=debug_mode
    )

    runner_create_agent_fn = functools.partial(
        create_agent_fn,
        musique_data=musique_data,
        get_llm_fn=get_llm_fn,
        get_embed_fn=get_embed_fn,
        prompt_template_dir=prompt_template_dir,
        intermediate_dir=intermediate_dir,
        debug_mode=debug_mode,
    )

    questions = musique_data.get_all_questions()
    if debug_mode:
        seed = seed
        random.seed(seed)
        question = random.choice(questions)
        questions = [question]
        data_line = musique_data.get_data_line_by_question(question)
        logger.info(f"\n***** DEBUG MODEL DATA INSPECT  *****")
        logger.info(json.dumps(data_line, ensure_ascii=False, indent=4))
        logger.info(f"***** DEBUG MODEL DATA INSPECT  *****\n")

    infer_runner = ParallelInferenceRunner(
        parallel_num=parallel_num,
        continue_run=continue_run,
        workspace_dir=submit_dir,
    )

    agent_results = infer_runner.run(
        question_list=questions,
        create_agent_fn=runner_create_agent_fn,
        debug_mode=debug_mode,
    )

    if debug_mode:
        logger.info(f"\n***** DEBUG MODEL Finish Compare Result *****\n")
        expected_answer = data_line["answer"]
        expected_predicted_support_idxs = [
            q_dict["paragraph_support_idx"]
            for q_dict in data_line["question_decomposition"]
        ]
        actual_answer = agent_results["predicted_answer"]
        actual_predicted_support_idxs = agent_results["predicted_support_idxs"]
        logger.info(
            f"\n  expected answer: {expected_answer}\n"
            f"  actual answer: {actual_answer}\n"
            f"  expected predicted_support_idxs: {expected_predicted_support_idxs}\n"
            f"  actual_predicted_support_idxs: {actual_predicted_support_idxs}\n"
        )
    else:
        output_file_path = os.path.join(
            submit_dir, f"{data_version}_{data_tag}_{submit_tag}.jsonl"
        )
        generate_submission_file(output_file_path, musique_data, agent_results)


def run_deepseek_divide_and_conquer_experiment():
    data_dir = DATA_DIR
    data_version = "ans"
    data_tag = "train"
    prompt_tag = "divide_agent_with_text_extractor_v1"
    intermediate_tag = "divide_agent_with_text_extractor_v1"
    submit_tag = "divide_agent_with_text_extractor_v2"
    parallel_num = 4
    continue_run = True

    debug_mode = True
    seed = 9527

    run_experiment_impl(
        create_agent_fn=create_divide_and_conquer_agent_hierarchical_info_tool,
        get_llm_fn=get_official_deepseek_llm,
        get_embed_fn=get_official_aliyun_embed_fn,
        data_dir=data_dir,
        data_version=data_version,
        data_tag=data_tag,
        prompt_tag=prompt_tag,
        intermediate_tag=intermediate_tag,
        submit_tag=submit_tag,
        parallel_num=parallel_num,
        continue_run=continue_run,
        debug_mode=debug_mode,
        seed=seed,
    )


if __name__ == "__main__":
    run_deepseek_divide_and_conquer_experiment()
