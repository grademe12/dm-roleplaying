#!/usr/bin/env python3
"""Small fresh trial for explicit query arguments, using frozen-input history tooling."""
import memory_query_contract_cases
import run_memory_chain_eval as chain
import run_memory_history_eval as history

SOURCE_FILES=(*history.SOURCE_FILES,'tools/memory_query_contract_cases.py',
              'tools/run_memory_query_contract_eval.py')


def configure():
    chain.memory_chain_cases=memory_query_contract_cases
    chain.SOURCE_FILES=SOURCE_FILES


if __name__=='__main__':
    history.main(configure_profile=configure)
