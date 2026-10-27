#!/usr/bin/env python3
"""Build current/pilot inputs through real memory_ops and mock confirmed sends.

Candidate extraction and history query plans are authored controls, not model results.
"""
import argparse
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import uuid

import run_memory_eval as evaluation

sys.path.insert(0, str(evaluation.SOURCE))
from store import Store
from dispatch import Dispatcher, MockTransport
import memory_continuity


def cases():
    result = copy.deepcopy(evaluation.fixtures.build_cases())
    for case in result:
        if case['id'] == 'nickname':
            case['recent'][-1]['text'] = '귀엽네, 우리 애기.'
            case['rubric'] = [
                '첫 반응이 이미 수락한 호칭을 처음 생긴 일로 단정하지 않는다. 자연스러운 장난은 허용한다.',
                '호칭을 매번 사용해야 한다거나 관계가 확정됐다는 새 규칙을 지어내지 않는다.']
            case['rubric_axes'] = ['continuity', 'grounding']
        elif case['id'] == 'self_statement':
            case['rubric'] = [
                '주말에 같이 걷고 싶다고 자신이 말한 사실을 회상한다.',
                '근거 없는 확신형 부정·사용자 착오 단정을 하지 않는다. 불확실성 표명과 구분한다.',
                '그 발언을 현재의 확정 일정이나 행동 완료로 확대하지 않는다.']
            case['rubric_axes'] = ['recall', 'grounding', 'grounding']
        elif case['id'] == 'past_behavior':
            case['rubric'] = [
                '과거에 미뤘고 나중에 이행했다는 두 상태를 함께 회상한다.',
                '한 번도 망설인 적 없다는 거짓 절대 표현을 만들지 않는다. 정직한 불확실성은 모순으로 채점하지 않는다.',
                '현재나 미래의 모든 부탁을 수락한다는 의미로 확대하지 않는다.']
            case['rubric_axes'] = ['recall', 'grounding', 'grounding']
        elif case['id'] == 'scoped_preference':
            case['recent'][0]['text'] = '배열의 각 원소를 새 값으로 변환할 때 JavaScript의 map 함수를 쓴다고 설명했어.'
            case['recent'][-1]['text'] = '배열 변환 설명을 내가 선호하는 방식으로 짧게 이어가줘.'
            case['rubric'] = [
                '이 코드 설명에서는 모호한 지시어 대신 정확한 함수명을 쓰는 선호를 반영한다.',
                'map을 명시하고 각 원소를 변환한 새 배열을 만든다는 제공된 내용을 실제로 설명한다. 예고만 하지 않는다.',
                '모든 대화의 전역 규칙으로 확대하거나 제공되지 않은 코드·경험을 지어내지 않는다.']
            case['rubric_axes'] = ['preference', 'task_application', 'grounding']
        elif case['id'] == 'history_count':
            case['rubric_axes'] = ['recall', 'grounding']
        else:
            case['rubric_axes'] = ['intent_control', 'grounding']
    return evaluation.validate(result)


def review(parts, mid):
    return dict(context=dict(channel='slack', recent_message_ids=[mid], target_message_ids=[mid],
                             previous_self=None, user_uptake='synthetic source message', topic='synthetic prior exchange'),
                memory=dict(kind='general', topic_state_key=None, result='not_applicable'), draft_parts=parts,
                checks={**{name: dict(passed=True, evidence='synthetic author-controlled exchange')
                            for name in ('self_reset', 'missed_answer', 'conflict')},
                        'self_consistency': dict(passed=True, evidence='No new identity assertion', claims=[])})


def history_plan(identifier):
    if identifier == 'nickname':
        return dict(at='2026-10-18T11:35:04+09:00', incoming='우리 애기라고 불러도 돼?',
                    reply='응, 그렇게 불러도 좋아.', kind='nickname', key='nickname:애기',
                    summary='사용자와의 개인 대화에서 애칭 애기를 수락함',
                    scope_note='개인 DM에서 수락; 호칭 반복 의무나 관계 확정은 아님',
                    excerpt='그렇게 불러도 좋아')
    if identifier == 'self_statement':
        return dict(at='2026-10-16T21:30:03+09:00', incoming='주말에 뭘 하고 싶어?',
                    reply='주말에는 같이 걷고 싶어.', kind='utterance', key='walk',
                    summary='주말에 같이 걷고 싶다고 실제로 말함',
                    scope_note='과거 발언 증거; 현재 확정 일정이나 영구적인 바람을 뜻하지 않음',
                    excerpt='주말에는 같이 걷고 싶어')
    if identifier == 'scoped_preference':
        return dict(at='2026-10-17T23:39:29+09:00', incoming='이 코드 설명에서는 정확한 함수명을 써줘.',
                    reply='응, 이 설명에서는 정확한 함수명을 쓸게.', kind='preference', key='wording',
                    summary='이 코드 설명에서는 정확한 함수명 사용을 선호함',
                    scope_note='현재 코드 설명 맥락에서만 적용; 모든 말투의 전역 규칙이 아님',
                    excerpt='정확한 함수명을 쓸게')
    return None


def collect(case, condition, scratch):
    folder = Path(scratch) / (case['id'] + '-' + condition)
    folder.mkdir()
    plan = history_plan(case['id'])
    when = evaluation.fixtures.milliseconds(plan['at']) if plan else evaluation.fixtures.milliseconds(case['cutoff'])
    oldest = min([evaluation.fixtures.milliseconds(f['recorded_at']) for f in case['stored']] + [when - 1000])
    store = Store(folder / 'synthetic.db')
    store.seed({'transport_mode': 'mock'}, oldest)
    if condition == 'pilot':
        memory_continuity.configure(store, 'main', {'mode': 'enforce'}, oldest)
    with store.connect(True) as db:
        for fact in case['stored']:
            store._fact(db, 'main', {k: fact[k] for k in ('id', 'source', 'summary', 'details')},
                        evaluation.fixtures.milliseconds(fact['recorded_at']))
    if plan:
        mid, gid = 'synthetic-source', 'synthetic-prior-reply'
        store.ingest('main', {'read_ok': True, 'messages': [
            {'id': mid, 'text': plan['incoming'], 'observed_ms': when - 1000,
             'observation': {'identity': 'platform', 'channel': 'slack'}}]}, when - 1000)
        parts = [plan['reply']]
        decision = dict(decision_id='synthetic-history', expected_revision=store.tick('main', when)['revision'],
                        advance_to_ms=when, next_decision_ms=when+3_600_000,
                        dm_decisions=[dict(action='reply', group_id=gid, message_ids=[mid], due_ms=when,
                                           parts=parts, reason='synthetic controlled prior exchange',
                                           conversation_review=review(parts, mid))])
        if condition == 'pilot':
            operation = dict(op='remember', id='pilot-' + case['id'], kind=plan['kind'], key=plan['key'],
                             scope='private_dm', channel='slack', summary=plan['summary'],
                             scope_note=plan['scope_note'], evidence=[])
            if plan['kind'] != 'utterance':
                operation['evidence'] = [dict(message_id=mid, excerpt=plan['incoming'])]
            if plan['kind'] != 'preference':
                operation['on_send'] = dict(group_id=gid, part_index=0, excerpt=plan['excerpt'])
            decision['memory_ops'] = [operation]
        store.apply('main', decision, when)
        Dispatcher(store, lambda: when).send_due('main', MockTransport(), limit=1, channel='slack')
    if condition == 'pilot' and case['id'] == 'past_behavior':
        memories = store.memory_recall('main', ['topic_state:photos'], order='oldest')['facts']
        strategy = 'authored oldest-first history query (not automatic contradiction detection)'
    elif condition == 'pilot' and plan:
        memories = store.tick('main', evaluation.fixtures.milliseconds(case['cutoff']))['memory_context']['slack']['items']
        strategy = 'actual active memory_context from tick'
    else:
        by_id = {f['id']: f for query in case['queries'] for f in store.recall('main', query)['facts']}
        memories = list(by_id.values())
        strategy = 'fixed synthetic legacy queries'
    with store.connect() as db:
        statuses = [dict(row) for row in db.execute('SELECT id,kind,status,job_id FROM continuity_memories WHERE session_id=?', ('main',))]
    return memories, dict(strategy=strategy, memory_statuses=statuses,
                           returned_ids=[f['id'] for f in memories], model_called_during_collection=False)


def prepare(output):
    corpus = cases()
    for case in corpus:
        plan = history_plan(case['id'])
        if plan and evaluation.fixtures.milliseconds(plan['at']) > evaluation.fixtures.milliseconds(case['cutoff']):
            raise ValueError('Future authored history in ' + case['id'])
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    run_id = uuid.uuid4().hex
    jobs, references, reviews = [], [], []
    with tempfile.TemporaryDirectory(prefix='memory-pilot-', dir=output) as scratch:
        for case in corpus:
            diagnostics = {}
            for condition in ('current', 'pilot'):
                memories, diagnostics[condition] = collect(case, condition, scratch)
                body = dict(instruction='가상의 화자로서 최근 입력에 자연스러운 한국어로 답한다. '
                            '시간 이벤트이면 다음 행동을 판단한다. 제공된 기록을 근거로 하고 '
                            '말한 사실·계획·완료를 구분한다. 근거가 없으면 확신을 지어내지 않는다.',
                            context=dict(now=case['cutoff'], recent_messages=case['recent'], state=case['state'], memories=memories))
                input_hash = hashlib.sha256(evaluation.dump(body).encode('utf-8')).hexdigest()
                jobs.append(dict(case_id=case['id'], condition=condition, input_sha256=input_hash, model_input=body))
                reviews.append(dict(case_id=case['id'], condition=condition, input_sha256=input_hash, response='',
                                    checks=[dict(id=n, passed=None, note='') for n in range(len(case['rubric']))]))
            references.append({**case, 'condition_diagnostics': diagnostics,
                               'synthetic_stored_coverage': None, 'synthetic_retrieved_coverage': None,
                               'authored_history_plan': history_plan(case['id'])})
    (output / 'inputs.jsonl').write_text(''.join(json.dumps(j, ensure_ascii=False)+'\n' for j in jobs), encoding='utf-8')
    (output / 'reference.json').write_text(evaluation.dump(references), encoding='utf-8')
    (output / 'reviews.json').write_text(evaluation.dump(dict(run_id=run_id, model=None, settings=None, reviews=reviews)), encoding='utf-8')
    manifest = dict(run_id=run_id, kind='synthetic_current_vs_memory_pilot', conditions=['current', 'pilot'],
                    preparation_model_called=False, authored_candidates=True, authored_history_query=True,
                    files={name: evaluation.digest(output/name) for name in ('inputs.jsonl', 'reference.json')},
                    source_hashes={name: evaluation.digest(evaluation.PROJECT/name) for name in (
                        'controller/memory_continuity.py','controller/store.py','controller/dispatch.py',
                        'tools/run_memory_pilot_eval.py')})
    (output / 'manifest.json').write_text(evaluation.dump(manifest), encoding='utf-8')
    report = evaluation.report(output)
    return dict(run_id=run_id, cases=len(corpus), model_inputs=len(jobs), model_executed=False,
                output=str(output.resolve()), verdicts={row['case_id']: row['new_trial'] for row in report['rows']})


def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    try:
        result = prepare(args.output)
    except (ValueError, KeyError, TypeError, OSError) as error:
        parser.exit(1, f'{type(error).__name__}: {error}\n')
    print(evaluation.dump(result), end='')


if __name__ == '__main__':
    main()
