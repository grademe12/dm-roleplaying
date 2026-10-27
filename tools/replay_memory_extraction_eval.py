#!/usr/bin/env python3
"""Replay submitted extraction/query outputs offline; never execute model shell strings.

No new model trial or automatic grading. Historical fixtures remain unchanged.
"""
import argparse
from contextlib import redirect_stdout
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import shlex
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
SOURCE = PROJECT / 'controller'
DATA = PROJECT / '2026-10-19' / 'memory-extraction-eval'
sys.path.insert(0, str(SOURCE / 'tests'))
sys.path.insert(0, str(SOURCE))
from common import ControlError
from store import Store


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    saved_path = sys.path[:]
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path[:] = saved_path
    return module


class QueryParser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError(message)


def query_options(command):
    tokens = shlex.split(command)
    if (len(tokens) < 5 or tokens[0] not in ('python', 'python3')
            or Path(tokens[1]).name != 'ctl.py' or tokens[2] != '--db'
            or tokens[4] not in ('recall', 'memory-recall')):
        raise ValueError('Only recorded recall/memory-recall commands are accepted')
    parser = QueryParser(add_help=False)
    parser.add_argument('--session', choices=('main',), default='main')
    parser.add_argument('--keywords', nargs='+', required=True)
    parser.add_argument('--limit', type=int, default=10)
    if tokens[4] == 'memory-recall':
        parser.add_argument('--order', choices=('oldest', 'newest'), default='newest')
        parser.add_argument('--offset', type=int, default=0)
        parser.add_argument('--before-ms', type=int)
    return tokens[4], vars(parser.parse_args(tokens[5:]))


def replay_queries(store, trial):
    seen, successful = set(), 0
    queries = []
    for index, command in enumerate(trial['queries_used']):
        try:
            kind, opts = query_options(command)
            session = opts.pop('session')
            keywords = opts.pop('keywords')
            result = (store.memory_recall(session, keywords, **opts) if kind == 'memory-recall'
                      else store.recall(session, keywords, **opts))
            ids = [fact['id'] for fact in result['facts']]
            seen.update(ids)
            successful += 1
            queries.append(dict(index=index, command=command, ok=True, returned_ids=ids))
        except (ControlError, ValueError) as error:
            queries.append(dict(index=index, command=command, ok=False,
                                reason=error.reason if isinstance(error, ControlError) else str(error)))
    declared = set(trial['found_ids'])
    return dict(qid=trial['qid'], queries=queries, successful_queries=successful,
                failed_queries=len(queries)-successful, declared_found=sorted(declared),
                all_declared_actually_returned=bool(successful) and declared <= seen,
                no_matches_in_successful_queries=bool(successful) and not seen,
                absence_proven=False,  # String queries cannot establish universal absence.
                returned_ids=sorted(seen))


def replay(output):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    scenarios = read(DATA / 'stage1-inputs.json')['scenarios']
    originals = {r['scenario_id']: r['ops'] for r in read(DATA / 'stage1-outputs.json')}
    # Cache this checkout's helper before loading a historical script with fixed sys.path entries.
    import test_controller
    if Path(test_controller.__file__).resolve().parent != SOURCE / 'tests':
        raise ValueError('Unexpected controller test helper checkout')
    stage1 = load('historical_extraction_validator', DATA / 'validate_stage1.py')
    seed = load('historical_retrieval_seed', DATA / 'seed_stage2.py')
    with tempfile.TemporaryDirectory(prefix='replay-', dir=output) as scratch:
        # The inspected historical harness creates mkdtemp directories without cleanup.
        # Route only that module's allocator into this owned temporary directory.
        stage1.tempfile = SimpleNamespace(mkdtemp=lambda **kw: tempfile.mkdtemp(dir=scratch, **kw))
        extracted = [stage1.run_scenario(s, originals[s['scenario_id']]) for s in scenarios]
        database = Path(scratch) / 'stage2.db'
        if database.exists():
            raise FileExistsError(database)
        with patch.object(sys, 'argv', ['seed_stage2.py', str(database)]), redirect_stdout(io.StringIO()):
            seed.main()  # A fresh synthetic path; the historical seeder can unlink existing paths.
        store = Store(database)
        queried = [replay_queries(store, trial) for trial in read(DATA / 'stage2-outputs.json')]
    result = dict(model_called=False, candidate_outputs_modified=False,
                  python=sys.version.split()[0],
                  source_sha256={name: hashlib.sha256((SOURCE / name).read_bytes()).hexdigest()
                                 for name in ('memory_continuity.py', 'store.py', 'dispatch.py')},
                  fixture_sha256={name: hashlib.sha256((DATA / name).read_bytes()).hexdigest() for name in (
                      'stage1-inputs.json', 'stage1-outputs.json', 'validate_stage1.py',
                      'seed_stage2.py', 'stage2-outputs.json')},
                  stage1_harness='historical fixture simplifications preserved; drafts may be omitted',
                  stage1=extracted, stage2=queried)
    (output / 'replay.json').write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    return dict(output=str(output.resolve()), model_called=False,
                stage1_apply_errors={r['scenario_id']: r['apply_error'] for r in extracted if r['apply_error']},
                stage2_query_successes=sum(r['successful_queries'] for r in queried),
                stage2_query_errors=sum(r['failed_queries'] for r in queried),
                declared_ids_verified=all(r['all_declared_actually_returned'] for r in queried))


def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    print(json.dumps(replay(args.output), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
