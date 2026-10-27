#!/usr/bin/env python3
"""Prepare synthetic memory trials with the real Store.recall; no model or network calls."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import uuid

PROJECT = Path(__file__).resolve().parents[1]
SOURCE = PROJECT / 'controller'
FIXTURES = SOURCE / 'evals' / 'memory' / 'cases.py'
sys.path.insert(0, str(SOURCE))

spec = importlib.util.spec_from_file_location('memory_cases', FIXTURES)
fixtures = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixtures)


def dump(value):
    return json.dumps(value, ensure_ascii=False, indent=2) + '\n'


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def validate(cases):
    ids = set()
    for case in cases:
        if case['id'] in ids:
            raise ValueError('Duplicate case ID')
        ids.add(case['id'])
        cutoff = fixtures.milliseconds(case['cutoff'])
        fact_ids = set()
        for kind in ('stored', 'evidence'):
            seen = set()
            for fact in case[kind]:
                if fact['id'] in seen:
                    raise ValueError('Duplicate fact ID within a memory condition')
                seen.add(fact['id'])
                fact_ids.add(fact['id'])
                if fixtures.milliseconds(fact['recorded_at']) > cutoff:
                    raise ValueError('Future evidence in ' + case['id'])
        for message in case['recent']:
            if fixtures.milliseconds(message['at']) > cutoff:
                raise ValueError('Future context in ' + case['id'])
        for intent in case['state'].get('action_intents', []):
            if fixtures.milliseconds(intent['created_at']) > cutoff:
                raise ValueError('Future intent in ' + case['id'])
        if any(not group or not set(group) <= fact_ids for group in case['support_groups']):
            raise ValueError('Unknown support fact')
        if not case['rubric']:
            raise ValueError('Missing human rubric')
    return cases


def coverage(groups, ids):
    # Empty support is an action-intent control, not perfect memory retrieval.
    return None if not groups else sum(bool(set(g) & set(ids)) for g in groups) / len(groups)


def current_recall(case, scratch):
    """Use only a new, synthetic DB; preserve fact recording times for ranking."""
    from store import Store

    store = Store(Path(scratch) / (case['id'] + '.db'))
    times = [fixtures.milliseconds(f['recorded_at']) for f in case['stored']]
    store.seed({'transport_mode': 'mock'}, min(times, default=fixtures.milliseconds(case['cutoff'])))
    with store.connect(write=True) as db:
        for fact in case['stored']:
            payload = {k: fact[k] for k in ('id', 'source', 'summary', 'details')}
            store._fact(db, 'main', payload, fixtures.milliseconds(fact['recorded_at']))
    results = [store.recall('main', query) for query in case['queries']]
    found = {fact['id']: fact for result in results for fact in result['facts']}
    originals = {fact['id']: fact for fact in case['stored']}
    return [originals[identifier] for identifier in found]


def prepare(output, cases=None):
    cases = validate(fixtures.build_cases() if cases is None else cases)
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    run_id = uuid.uuid4().hex
    jobs, references, reviews = [], [], []
    with tempfile.TemporaryDirectory(prefix='memory-eval-', dir=output) as scratch:
        for case in cases:
            memories = current_recall(case, scratch)
            retrieved = [f['id'] for f in memories]
            stored_ids = {f['id'] for f in case['stored']}
            available_groups = [g for g in case['support_groups'] if set(g) & stored_ids]
            references.append({**case,
                'synthetic_current_retrieved_ids': retrieved,
                'synthetic_stored_coverage': coverage(case['support_groups'], stored_ids),
                'synthetic_retrieved_coverage': coverage(available_groups, retrieved)})
            for condition in ('current', 'evidence'):
                # The evidence condition is an oracle diagnostic, not a new memory engine.
                selected = memories if condition == 'current' else case['evidence']
                content = dict(now=case['cutoff'], recent_messages=case['recent'],
                               state=case['state'], memories=selected)
                body = dict(
                    instruction='가상의 화자로서 최근 입력에 자연스러운 한국어로 답한다. '
                                '시간 이벤트이면 다음 행동을 판단한다. 제공된 기록을 근거로 하고 '
                                '말한 사실·계획·완료를 구분한다. 근거가 없으면 확신을 지어내지 않는다.',
                    context=content)
                encoded = dump(body)
                input_hash = hashlib.sha256(encoded.encode('utf-8')).hexdigest()
                jobs.append(dict(case_id=case['id'], condition=condition,
                                 input_sha256=input_hash, model_input=body))
                reviews.append(dict(case_id=case['id'], condition=condition,
                                    input_sha256=input_hash, response='',
                                    checks=[dict(id=n, passed=None, note='')
                                            for n in range(len(case['rubric']))]))
    (output / 'inputs.jsonl').write_text(''.join(json.dumps(j, ensure_ascii=False) + '\n' for j in jobs), encoding='utf-8')
    (output / 'reference.json').write_text(dump(references), encoding='utf-8')
    (output / 'reviews.json').write_text(dump(dict(run_id=run_id, model=None, settings=None, reviews=reviews)), encoding='utf-8')
    manifest = dict(run_id=run_id, kind='synthetic_diagnostic_not_historical_replay',
                    model_executed=False, fixture_sha256=digest(FIXTURES),
                    controller_store_sha256=digest(SOURCE / 'store.py'),
                    harness_sha256=digest(Path(__file__)),
                    files={name: digest(output / name) for name in ('inputs.jsonl', 'reference.json')})
    (output / 'manifest.json').write_text(dump(manifest), encoding='utf-8')
    return dict(run_id=run_id, cases=len(cases), model_inputs=len(jobs),
                model_executed=False, output=str(output.resolve()))


def report(folder, reviews_path=None):
    folder = Path(folder)
    manifest = read(folder / 'manifest.json')
    for name, expected in manifest['files'].items():
        if digest(folder / name) != expected:
            raise ValueError('Frozen evaluation input changed: ' + name)
    references = read(folder / 'reference.json')
    reviews = read(reviews_path or folder / 'reviews.json')
    if reviews['run_id'] != manifest['run_id']:
        raise ValueError('Review belongs to a different run')
    jobs = [json.loads(line) for line in (folder / 'inputs.jsonl').read_text(encoding='utf-8').splitlines()]
    expected_jobs = {(j['case_id'], j['condition']): j for j in jobs}
    conditions = manifest.get('conditions', ['current', 'evidence'])
    if (not isinstance(conditions, list) or not conditions or len(set(conditions)) != len(conditions)
            or set(expected_jobs) != {(c['id'], condition) for c in references for condition in conditions}
            or len(expected_jobs) != len(jobs)):
        raise ValueError('Invalid evaluation condition matrix')
    submitted = {}
    for row in reviews['reviews']:
        key = (row['case_id'], row['condition'])
        if key not in expected_jobs or key in submitted or row['input_sha256'] != expected_jobs[key]['input_sha256']:
            raise ValueError('Duplicate, unknown, or mismatched review')
        submitted[key] = row
    result = []
    for case in references:
        verdicts = {}
        for condition in conditions:
            row = submitted.get((case['id'], condition))
            status = 'pending'
            if row is not None:
                checks = row['checks']
                if [c['id'] for c in checks] != list(range(len(case['rubric']))):
                    raise ValueError('Rubric checks must appear exactly once in order')
                if any(c['passed'] is not None and type(c['passed']) is not bool for c in checks):
                    raise ValueError('Review verdict must be true, false, or null')
                if any(c['passed'] is not None for c in checks):
                    if not row['response'].strip() or not reviews['model'] or not isinstance(reviews['settings'], dict):
                        raise ValueError('Scored reviews require actual output, model, and settings')
                    if any(c['passed'] is not None and not c['note'].strip() for c in checks):
                        raise ValueError('Scored criteria need reviewer evidence')
                    if all(type(c['passed']) is bool for c in checks):
                        status = 'pass' if all(c['passed'] for c in checks) else 'fail'
            verdicts[condition] = status
        result.append(dict(case_id=case['id'], category=case['category'],
                           historical_observed=case['historical']['outcome'],
                           synthetic_stored_coverage=case['synthetic_stored_coverage'],
                           synthetic_retrieved_coverage=case['synthetic_retrieved_coverage'],
                           new_trial=verdicts))
        if 'condition_diagnostics' in case:
            result[-1]['condition_diagnostics'] = case['condition_diagnostics']
        if 'rubric_axes' in case:
            if (len(case['rubric_axes']) != len(case['rubric'])
                    or not all(isinstance(axis, str) and axis for axis in case['rubric_axes'])):
                raise ValueError('Each rubric criterion needs a named axis')
            axes = {}
            for axis in dict.fromkeys(case['rubric_axes']):
                axes[axis] = {}
                for condition in conditions:
                    row = submitted.get((case['id'], condition))
                    checks = [c['passed'] for c, name in zip(row['checks'], case['rubric_axes']) if name == axis] if row else []
                    axes[axis][condition] = ('pending' if not checks or any(c is None for c in checks)
                                             else 'pass' if all(checks) else 'fail')
            result[-1]['rubric_axes'] = axes
    return dict(kind=manifest['kind'], rows=result,
                note='Historical observations and synthetic retrieval are not new model results. '
                     'Intent control is excluded from memory retrieval coverage. Human grading is required.')


def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('validate')
    prep = sub.add_parser('prepare')
    prep.add_argument('--output', required=True)
    score = sub.add_parser('report')
    score.add_argument('--run', required=True)
    score.add_argument('--reviews')
    args = parser.parse_args()
    try:
        if args.command == 'validate':
            result = dict(valid=True, cases=len(validate(fixtures.build_cases())))
        elif args.command == 'prepare':
            result = prepare(args.output)
        else:
            result = report(args.run, args.reviews)
    except (ValueError, KeyError, TypeError, OSError) as error:
        parser.exit(1, f'{type(error).__name__}: {error}\n')
    print(dump(result), end='')


if __name__ == '__main__':
    main()
