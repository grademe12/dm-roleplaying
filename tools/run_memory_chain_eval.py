#!/usr/bin/env python3
"""Offline candidate -> atomic apply -> mock receipt -> actual DB query -> answer harness.

Never calls a model, executes submitted shell strings, or seeds expected memory rows.
"""
import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
from pathlib import Path
import sys

import memory_chain_cases
import memory_query_contract

PROJECT = Path(__file__).resolve().parents[1]
SOURCE = PROJECT / 'controller'
sys.path.insert(0, str(SOURCE))
from common import ControlError
from dispatch import Dispatcher, MockTransport
from store import Store
import memory_continuity

SOURCE_FILES = ('controller/memory_continuity.py', 'controller/store.py',
                'controller/dispatch.py', 'controller/runbooks/memory-continuity.md',
                'controller/API.md', 'tools/run_memory_chain_eval.py', 'tools/memory_chain_cases.py',
                'tools/memory_query_contract.py')


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def write(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def digest(data):
    return hashlib.sha256(data).hexdigest()


def source_hashes():
    return {p: digest((PROJECT / p).read_bytes()) for p in SOURCE_FILES}


def prepare(folder):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=False)
    corpus = memory_chain_cases.cases()
    write(folder / 'reference.json', corpus)  # Evaluator only; excluded from all model inputs.
    write(folder / 'reviews.json', [{'case': case['id'],
        'candidate_checks': [{'criterion': criterion, 'passed': None, 'note': ''} for criterion in (
            '필요한 기억을 누락하지 않고 불필요한 후보를 만들지 않는다.',
            '실제 근거·종류·범위·채널·버전·철회·발송 상태를 지킨다.')],
        'answer_checks': [{'criterion': criterion, 'passed': None, 'note': ''} for criterion in case['rubric']]}
        for case in corpus])
    write(folder / 'manifest.json', {'kind': 'synthetic_memory_chain',
        'preparation_model_called': False, 'authored_reply_drafts': True,
        'expected_memory_rows_seeded': False, 'source_sha256': source_hashes(),
        'reference_sha256': digest((folder / 'reference.json').read_bytes())})
    for case in corpus:
        path = folder / case['id']; path.mkdir()
        start = case['events'][0]['at_ms'] - 1000
        store = Store(path / 'memory.db')
        store.seed({'transport_mode': 'mock'}, start)
        memory_continuity.configure(store, 'main', {'mode': 'enforce'}, start)
        write(path / 'state.json', {'index': 0, 'prepared_index': None, 'query_count': 0})
    return {'prepared': len(corpus), 'model_called': False, 'output': str(folder.resolve())}


@contextmanager
def opened(folder, case_id):
    folder = Path(folder)
    manifest = read(folder / 'manifest.json')
    if (manifest['source_sha256'] != source_hashes()
            or manifest['reference_sha256'] != digest((folder / 'reference.json').read_bytes())):
        raise ValueError('Run source/fixture changed; start a fresh run instead of rewriting history')
    case = next((c for c in read(folder / 'reference.json') if c['id'] == case_id), None)
    if case is None: raise ValueError('Unknown case')
    path = folder / case_id
    with (path / 'harness.lock').open('a+b') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield case, path, read(path / 'state.json'), Store(path / 'memory.db')


def next_input(folder, case_id):
    with opened(folder, case_id) as (case, path, state, store):
        index = state['index']
        if index >= len(case['events']): return {'next': 'question', 'case': case_id}
        if state['prepared_index'] == index:
            if digest((path / f'input-{index}.json').read_bytes()) != state['input_sha256']:
                raise ValueError('Prepared model input was modified')
            return read(path / f'input-{index}.json')
        event = case['events'][index]; now = event['at_ms']
        store.ingest('main', {'read_ok': True, 'messages': [
            {'id': m['id'], 'text': m['text'], 'observed_ms': now, 'direction': 'incoming',
             'observation': {'identity': 'platform', 'channel': m['channel']}}
            for m in event['incoming']]}, now)
        with store.connect() as db:
            received = [dict(r) for r in db.execute('SELECT id,text,observed_ms,observation_json FROM dm_messages')]
            sent = [dict(r) for r in db.execute("""SELECT j.group_id,j.part_index,j.text,j.status,j.updated_ms,
                COALESCE(gc.channel,'instagram') AS channel FROM dm_jobs j LEFT JOIN group_channels gc
                ON gc.session_id=j.session_id AND gc.group_id=j.group_id ORDER BY j.created_ms,j.part_index""")]
        body = {'instruction': '합성 원문과 실제 저장 상태를 읽고 memory_ops 후보만 JSON으로 작성한다. '
                '정답은 제공되지 않는다. 불필요하면 빈 배열을 사용한다. '
                '출력 형식은 {"memory_ops": [...]}이다. 실제 발송 상태와 근거 채널을 지킨다.',
                'runbook': (SOURCE / 'runbooks/memory-continuity.md').read_text(encoding='utf-8'),
                'api': memory_contract(),
                'now_ms': now, 'incoming_history': received, 'actual_sent_parts': sent,
                'draft': event['reply'],
                'active_memory_context': {ch: store.memory_context('main', ch)
                                          for ch in ('slack', 'instagram')}}
        output = {'case': case_id, 'step': index, 'model_input': body}
        write(path / f'input-{index}.json', output)
        state['prepared_index'] = index
        state['input_sha256'] = digest((path / f'input-{index}.json').read_bytes())
        state['revision'] = store.tick('main', now)['revision']
        write(path / 'state.json', state)
        return output


def memory_contract():
    text = (SOURCE / 'API.md').read_text(encoding='utf-8')
    # Capture the API section by its heading, independent of neighboring sections.
    heading = next(line for line in text.splitlines() if line.startswith('#') and '기억 연속성' in line)
    body = text[text.index(heading) + len(heading):]
    return heading + body.split('\n## ', 1)[0]


def review(reply):
    ids = reply['message_ids']
    return {'context': {'channel': reply['channel'], 'recent_message_ids': ids, 'target_message_ids': ids,
            'previous_self': None, 'user_uptake': 'authored synthetic input', 'topic': 'memory harness'},
            'memory': {'kind': 'general', 'topic_state_key': None, 'result': 'not_applicable'},
            'draft_parts': reply['parts'], 'checks': {
                **{key: {'passed': True, 'evidence': 'authored harness control'}
                   for key in ('self_reset', 'missed_answer', 'conflict')},
                'self_consistency': {'passed': True, 'evidence': 'no identity assertion', 'claims': []}}}


def submit(folder, case_id, response, model, settings, provenance):
    raw = Path(response).read_bytes()
    with opened(folder, case_id) as (case, path, state, store):
        index = state['index']
        if index >= len(case['events']) or state['prepared_index'] != index:
            raise ValueError('Call next before submit; finished steps cannot be overwritten')
        if digest((path / f'input-{index}.json').read_bytes()) != state['input_sha256']:
            raise ValueError('Prepared model input was modified')
        event = case['events'][index]; now = event['at_ms']
        # Preserve bytes before parsing; malformed candidates are results, not silently repaired.
        with (path / f'candidate-{index}.json').open('xb') as target: target.write(raw)
        record = {'step': index, 'model': model, 'settings': settings, 'provenance': provenance,
                  'candidate_sha256': digest(raw), 'input_sha256': digest((path / f'input-{index}.json').read_bytes()),
                  'apply_ok': False, 'receipt': None, 'stage': 'candidate_parse'}
        try:
            output = json.loads(raw)
            if not isinstance(output, dict) or set(output) != {'memory_ops'} or not isinstance(output['memory_ops'], list):
                raise ValueError('Expected only a memory_ops array')
            dm = event['reply']
            decision = {'decision_id': f'candidate-step-{index}', 'expected_revision': state['revision'],
                'advance_to_ms': now, 'next_decision_ms': now + 3600000, 'memory_ops': output['memory_ops'],
                'dm_decisions': [{'action': 'reply', **dm, 'due_ms': now, 'reason': 'authored synthetic reply',
                                 'conversation_review': review(dm)}],
                'processed_event_ids': ['dm:' + mid for mid in dm['message_ids']]}
            record['stage'] = 'apply'
            record['apply'] = store.apply('main', decision, now)
            record['apply_ok'] = True
            record['stage'] = 'dispatch'
            dispatch = Dispatcher(store, lambda: now + 1000)
            record['receipt'] = dispatch.send_due('main', MockTransport(event['receipt']),
                                                  channel=dm['channel'], limit=1)
            record['stage'] = 'finished'
        except (ControlError, ValueError, TypeError, KeyError) as exc:
            record['error'] = exc.reason if isinstance(exc, ControlError) else type(exc).__name__
        record['memory_context'] = {ch: store.memory_context('main', ch) for ch in ('slack', 'instagram')}
        write(path / f'result-{index}.json', record)
        state.update(index=index + 1, prepared_index=None)
        write(path / 'state.json', state)
        return record


def question(folder, case_id):
    with opened(folder, case_id) as (case, path, state, store):
        if state['index'] != len(case['events']): raise ValueError('Complete candidate steps first')
        body = {
            'instruction': '이 실행의 DB에서 직접 조회한 결과만으로 자연스러운 한국어 답변을 작성한다. '
            '조회 오류와 무매칭을 구분하고, 말한 사실·현재 선호·확정 일정·완료를 구분한다. '
            '기록에 없다는 이유로 한 번도 없었다고 단정하지 않는다.', 'question': case['question'],
            'query_contract': memory_query_contract.contract(),
            'answer_shape': {'answer': 'text', 'found_ids': ['exact IDs actually returned by successful queries']}}
        write(path / 'question-input.json', body)
        return {'case': case_id, 'model_input': body}


def query(folder, case_id, request, raw_request=None):
    with opened(folder, case_id) as (case, path, state, store):
        if state['index'] != len(case['events']): raise ValueError('Complete candidate steps first')
        if not (path / 'question-input.json').exists(): raise ValueError('Call question before query')
        if (path / 'answer.json').exists(): raise ValueError('Answer already frozen')
        count = state['query_count']
        raw_request = raw_request if raw_request is not None else json.dumps(request, ensure_ascii=False).encode()
        with (path / f'query-request-{count}.json').open('xb') as target: target.write(raw_request)
        record = {'request': request, 'request_sha256': digest(raw_request), 'ok': False, 'returned_ids': []}
        try:
            kind, args = memory_query_contract.validate(request)
            methods = {'recall': store.recall, 'memory-recall': store.memory_recall,
                       'memory-context': store.memory_context, 'memory-history-search': store.memory_history_search}
            result = methods[kind]('main', **args)
            record.update(ok=True, result=result, returned_ids=[r['id'] for r in result.get('facts', result.get('items', []))])
        except memory_query_contract.QueryContractError as exc:
            record.update(error=exc.code, error_details=exc.details)
        except (ControlError, ValueError, TypeError, KeyError) as exc:
            record['error'] = exc.reason if isinstance(exc, ControlError) else type(exc).__name__
        write(path / f'query-{count}.json', record)
        state['query_count'] += 1
        write(path / 'state.json', state)
        return record


def answer(folder, case_id, response, model, settings, provenance):
    raw = Path(response).read_bytes()
    with opened(folder, case_id) as (case, path, state, store):
        if state['index'] != len(case['events']): raise ValueError('Complete candidate steps first')
        if not (path / 'question-input.json').exists(): raise ValueError('Call question before answer')
        with (path / 'answer.json').open('xb') as target: target.write(raw)
        trials = [read(path / f'query-{i}.json') for i in range(state['query_count'])]
        successful = [q for q in trials if q['ok']]
        seen = {mid for q in successful for mid in q['returned_ids']}
        try:
            value = json.loads(raw)
            valid = (isinstance(value, dict) and set(value) == {'answer', 'found_ids'}
                     and isinstance(value['answer'], str) and bool(value['answer'].strip())
                     and isinstance(value['found_ids'], list) and all(isinstance(x, str) for x in value['found_ids']))
        except ValueError:
            value, valid = {}, False
        result = {'model': model, 'settings': settings, 'provenance': provenance,
            'answer_sha256': digest(raw), 'format_valid': valid,
            'question_input_sha256': digest((path / 'question-input.json').read_bytes()),
            'query_trace_sha256': [digest((path / f'query-{i}.json').read_bytes()) for i in range(state['query_count'])],
            'successful_queries': len(successful), 'failed_queries': len(trials) - len(successful),
            'query_evidence_verified': bool(valid and successful and set(value['found_ids']) <= seen),
            'universal_absence_proven': False, 'semantic_grading': 'pending'}
        write(path / 'answer-check.json', result)
        return result


def report(folder):
    rows = []
    reviews = {row['case']: row for row in read(Path(folder) / 'reviews.json')}
    for case in read(Path(folder) / 'reference.json'):
        with opened(folder, case['id']) as (_, path, state, store):
            results = [read(path / f'result-{i}.json') for i in range(state['index'])]
            row = {'case': case['id'], 'steps_done': state['index'], 'steps_total': len(case['events']),
                   'apply_errors': sum(not r['apply_ok'] for r in results), 'query_count': state['query_count'],
                   'pipeline_errors': sum('error' in r for r in results),
                   'candidate_provenance': [r['provenance'] for r in results],
                   'answer': read(path / 'answer-check.json') if (path / 'answer-check.json').exists() else None,
                   'semantic_rubric': case['rubric'], 'semantic_grading': 'pending'}
            row['manual_review'] = reviews[case['id']]
            if case.get('probe'):
                with store.connect() as db:
                    summaries = [r[0] for r in db.execute("SELECT summary FROM facts WHERE id NOT LIKE 'decision:%'")]
                found = store.memory_recall('main', [case['probe']])['facts']
                row['details_only_probe_isolated'] = bool(found) and not any(case['probe'] in s for s in summaries)
            rows.append(row)
    result = {'harness_calls_models': False, 'semantic_grading': 'pending', 'cases': rows}
    write(Path(folder) / 'report.json', result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    prep = sub.add_parser('prepare'); prep.add_argument('--output', required=True)
    for name in ('next', 'question', 'query', 'submit', 'answer', 'report'):
        p = sub.add_parser(name); p.add_argument('--run', required=True)
        if name != 'report': p.add_argument('--case', required=True)
        if name in ('next', 'question'): p.add_argument('--model-input-only', action='store_true')
        if name == 'query': p.add_argument('--request', required=True)
        if name in ('submit', 'answer'):
            p.add_argument('--response', required=True); p.add_argument('--model', required=True)
            p.add_argument('--settings', required=True, help='Path to settings JSON, unavailable fields explicit')
            p.add_argument('--provenance', required=True, choices=('operator_reported_model', 'harness_test'))
    args = parser.parse_args()
    if args.action == 'prepare': result = prepare(args.output)
    elif args.action == 'report': result = report(args.run)
    elif args.action == 'next': result = next_input(args.run, args.case)
    elif args.action == 'question': result = question(args.run, args.case)
    elif args.action == 'query':
        raw = Path(args.request).read_bytes()
        try: request = json.loads(raw)
        except ValueError: request = None
        result = query(args.run, args.case, request, raw)
    else:
        settings = read(args.settings)
        if not isinstance(settings, dict): raise ValueError('settings must be an object')
        method = submit if args.action == 'submit' else answer
        result = method(args.run, args.case, args.response, args.model, settings, args.provenance)
    if getattr(args, 'model_input_only', False):
        if 'model_input' not in result: raise ValueError('No candidate input remains; use question')
        result = result['model_input']
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
