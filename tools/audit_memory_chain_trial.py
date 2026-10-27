#!/usr/bin/env python3
"""Verify a frozen synthetic trial and replay submitted JSON in disposable databases.

Reads original SQLite files with immutable/read-only URIs. Never calls a model or
fills omitted candidates; semantic judgments belong in a separate review.
"""
import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile

import run_memory_chain_eval as chain


def hashes(root):
    return {str(p.relative_to(root)): chain.digest(p.read_bytes())
            for p in sorted(root.rglob('*')) if p.is_file()}


def database_rows(path):
    if any(Path(str(path) + suffix).exists() for suffix in ('-wal', '-shm')):
        raise ValueError('Live SQLite sidecar found; audit requires a closed, checkpointed snapshot')
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro&immutable=1', uri=True)) as db:
        db.row_factory = sqlite3.Row
        memories = [dict(r) for r in db.execute('SELECT * FROM continuity_memories ORDER BY id')]
        jobs = [dict(r) for r in db.execute('SELECT id,group_id,part_index,text,status,created_ms,updated_ms '
                                          'FROM dm_jobs ORDER BY id')]
    return {'memories': memories, 'jobs': jobs}


def audit(trial):
    trial = Path(trial).resolve()
    original = trial / 'run'
    before = hashes(trial)
    manifest = chain.read(original / 'manifest.json')
    if manifest['source_sha256'] != chain.source_hashes():
        raise ValueError('Trial source differs from this checkout; use the pinned trial code')
    assert manifest['reference_sha256'] == chain.digest((original / 'reference.json').read_bytes())
    assert chain.read(original / 'reference.json') == chain.memory_chain_cases.cases()
    records, checked_hashes = [], 1
    with tempfile.TemporaryDirectory(prefix='memory-chain-audit-') as scratch:
        replay = Path(scratch) / 'replayed'
        chain.prepare(replay)
        assert chain.read(original / 'reviews.json') == chain.read(replay / 'reviews.json'), 'Raw reviews were graded/changed'
        for case in chain.read(original / 'reference.json'):
            cid = case['id']; path = original / cid
            state = chain.read(path / 'state.json')
            assert state['index'] == len(case['events'])
            for index in range(state['index']):
                record = chain.read(path / f'result-{index}.json')
                assert record['candidate_sha256'] == chain.digest((path / f'candidate-{index}.json').read_bytes())
                assert record['input_sha256'] == chain.digest((path / f'input-{index}.json').read_bytes())
                checked_hashes += 2
                actual_input = chain.next_input(replay, cid)
                assert actual_input == chain.read(path / f'input-{index}.json'), (cid, 'input', index)
                result = chain.submit(replay, cid, path / f'candidate-{index}.json', record['model'],
                                      record['settings'], 'harness_test')
                assert result['apply_ok'] == record['apply_ok'], (cid, 'apply', index)
                for field in ('apply', 'receipt', 'memory_context'):
                    assert result.get(field) == record.get(field), (cid, field, index)
            assert chain.question(replay, cid)['model_input'] == chain.read(path / 'question-input.json')
            for index in range(state['query_count']):
                raw = (path / f'query-request-{index}.json').read_bytes()
                saved = chain.read(path / f'query-{index}.json')
                assert saved['request_sha256'] == chain.digest(raw)
                checked_hashes += 1
                request = json.loads(raw)
                assert request == saved['request'], (cid, 'request', index)
                result = chain.query(replay, cid, request, raw)
                assert result == saved, (cid, 'query', index)
            answer = chain.read(path / 'answer-check.json')
            assert answer['answer_sha256'] == chain.digest((path / 'answer.json').read_bytes())
            assert answer['question_input_sha256'] == chain.digest((path / 'question-input.json').read_bytes())
            assert answer['query_trace_sha256'] == [chain.digest((path / f'query-{i}.json').read_bytes())
                                                    for i in range(state['query_count'])]
            checked_hashes += 2 + state['query_count']
            verified = chain.answer(replay, cid, path / 'answer.json', answer['model'],
                                    answer['settings'], 'harness_test')
            for field in ('format_valid', 'successful_queries', 'failed_queries', 'query_evidence_verified'):
                assert verified[field] == answer[field], (cid, field)
            original_rows = database_rows(path / 'memory.db')
            assert original_rows == database_rows(replay / cid / 'memory.db'), (cid, 'database state')
            row = {'case': cid, 'steps': state['index'], 'queries': state['query_count'],
                   'replayed_inputs_apply_receipts_queries_match': True,
                   'replayed_memory_and_job_rows_match': True,
                   'memory_states': [{k: r[k] for k in ('id', 'kind', 'memory_key', 'status', 'job_id')}
                                     for r in original_rows['memories']],
                   'answer_evidence_verified': verified['query_evidence_verified']}
            # Reviewer probes use the replay copy and never count as model queries.
            with chain.opened(replay, cid) as (_, _, _, store):
                if cid == 'revoked-nickname':
                    memory = original_rows['memories'][0]
                    history = store.memory_context('main', 'slack', key=memory['memory_key'], history=True)
                    row['reviewer_known_key_history_probe'] = {
                        'counted_as_model_query': False, 'key': memory['memory_key'],
                        'items': history['items']}
                if cid == 'details-probe':
                    request = chain.read(path / 'query-request-0.json')
                    legacy = store.recall('main', request['arguments']['keywords'])
                    row['reviewer_legacy_same_keywords_probe'] = {
                        'counted_as_model_query': False, 'ids': [f['id'] for f in legacy['facts']]}
                    row['details_probe_token_in_summary'] = any(
                        case['probe'] in r['summary'] for r in original_rows['memories'])
            records.append(row)
    assert hashes(trial) == before, 'Frozen trial was modified'
    return {'model_called': False, 'candidate_outputs_modified': False, 'original_files_unchanged': True,
            'files_in_package': len(before), 'files_in_run': sum(p.startswith('run/') for p in before),
            'checked_embedded_hashes': checked_hashes,
            'source_hashes_match': True, 'case_steps': sum(r['steps'] for r in records),
            'queries': sum(r['queries'] for r in records), 'cases': records,
            'original_file_sha256': before}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--trial', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    output = Path(args.output)
    if output.exists(): raise FileExistsError(output)
    result = audit(args.trial)
    output.parent.mkdir(parents=True, exist_ok=True)
    chain.write(output, result)
    print(json.dumps({k: v for k, v in result.items() if k not in ('cases', 'original_file_sha256')}, indent=2))


if __name__ == '__main__':
    main()
