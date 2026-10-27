#!/usr/bin/env python3
"""Read-only review of a frozen history trial using a byte-identical copy for replay."""
import argparse
from contextlib import contextmanager
import json
from pathlib import Path
import shutil
import tempfile

import audit_memory_chain_trial as original_audit
import run_memory_chain_eval as chain
import run_memory_history_eval as history


@contextmanager
def history_contract(profile='history'):
    old_cases, old_sources = chain.memory_chain_cases, chain.SOURCE_FILES
    if profile == 'history':
        history.configure()
    elif profile == 'query-contract':
        import run_memory_query_contract_eval
        run_memory_query_contract_eval.configure()
    else:
        raise ValueError('Unknown evaluation profile')
    try:
        yield
    finally:
        chain.memory_chain_cases, chain.SOURCE_FILES = old_cases, old_sources


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode('utf-8')


def audit(trial, profile='history'):
    trial = Path(trial).resolve()
    before = original_audit.hashes(trial)
    prompts = []
    with history_contract(profile):
        for case in chain.read(trial / 'reference.json'):
            cid = case['id']; path = trial / cid
            state = chain.read(path / 'state.json')
            for index in range(state['index']):
                expected = {'wrapper': history.WRAPPERS['candidate'],
                            'model_input': chain.read(path / f'input-{index}.json')['model_input']}
                target = path / f'candidate-prompt-{index}.json'
                assert target.read_bytes() == encoded(expected), (cid, 'candidate prompt', index)
                prompts.append(str(target.relative_to(trial)))
            question = chain.read(path / 'question-input.json')
            for index in range(state['query_count'] + 1):
                expected = {'wrapper': history.WRAPPERS['query'], 'question_input': question,
                    'transcript': [{'request_utf8': (path / f'query-request-{n}.json').read_text(encoding='utf-8'),
                                    'result_utf8': (path / f'query-{n}.json').read_text(encoding='utf-8')}
                                   for n in range(index)]}
                target = path / f'query-prompt-{index}.json'
                assert target.read_bytes() == encoded(expected), (cid, 'query prompt', index)
                prompts.append(str(target.relative_to(trial)))
        with tempfile.TemporaryDirectory(prefix='history-audit-') as scratch:
            copy = Path(scratch) / 'trial'
            shutil.copytree(trial, copy / 'run')
            copied = original_audit.hashes(copy / 'run')
            assert copied == before, 'Replay input copy differs from frozen package'
            replay = original_audit.audit(copy)
    assert original_audit.hashes(trial) == before, 'Original trial was modified'
    return {'profile': profile, 'model_called': False, 'original_files_unchanged': True, 'original_database_opened_by_store': False,
            'byte_identical_copy_replayed': True, 'files_in_package': len(before),
            'frozen_prompt_count': len(prompts), 'frozen_prompts_match_complete_inputs_and_traces': True,
            'checked_embedded_hashes': replay['checked_embedded_hashes'], 'source_hashes_match': True,
            'case_steps': replay['case_steps'], 'queries': replay['queries'], 'cases': replay['cases'],
            'original_file_sha256': before, 'prompt_paths': prompts}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--trial', required=True); parser.add_argument('--output', required=True)
    parser.add_argument('--profile', choices=('history', 'query-contract'), default='history')
    args = parser.parse_args()
    output = Path(args.output)
    if output.resolve().is_relative_to(Path(args.trial).resolve()):
        raise ValueError('Audit output must be outside the frozen trial')
    if output.exists(): raise FileExistsError(output)
    result = audit(args.trial, args.profile)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open('x', encoding='utf-8') as target:
        target.write(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({k:v for k,v in result.items() if k not in ('cases','original_file_sha256','prompt_paths')}, indent=2))


if __name__ == '__main__': main()
