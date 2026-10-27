"""Repetition guard (operations/repetition-guard-plan-20261026.md).

All conversations here are synthetic Korean fixtures written for these tests;
no real conversation data is used. Default mode is off and must leave prepare
output, commit results and receipts untouched.
"""
import json
import unittest

from common import ControlError
import conversation_flow
import gate
import prepared_inputs
import repetition_cues as rc
import reply_generator
import test_decision_session as session_fixtures
import test_role_executor as role_fixtures

COPYSENT = '자, 말한 대로 찍었어. 침대에 앉아서 찍은 거야. 계속 말해줘'


def msg(direction, text, mid='x'):
    return {'id': mid, 'direction': direction, 'text': text}


class RepetitionCuesTests(unittest.TestCase):
    def setUp(self):
        self.opts = rc.settings({})

    def test_build_turns_groups_outgoing_and_skips_placeholders(self):
        messages = [msg('outgoing', '첫째'), msg('outgoing', '[사진]'), msg('outgoing', '둘째'),
                    msg('incoming', '응'), msg('outgoing', '[메시지]'),
                    msg('outgoing', '셋째')]
        self.assertEqual(rc.build_turns(messages, 8), [['첫째', '둘째'], ['셋째']])
        self.assertEqual(rc.build_turns(messages, 1), [['셋째']])

    def test_normalize_collapses_laughter_punctuation_and_spaces(self):
        self.assertEqual(rc.normalize('진짜ㅋㅋㅋ  웃겨... 그치ㅠㅠㅠ!!'), '진짜ㅋ 웃겨 그치ㅠ')
        self.assertEqual(rc.normalize('ㅎㅎㅎ 대박, 진짜로?'), 'ㅎ 대박 진짜로')

    def test_phrases_need_distinct_turns(self):
        one_turn = [msg('incoming', 'a'), msg('outgoing', '같은 문장입니다 같은 문장입니다')]
        built = rc.analyze('instagram', one_turn, self.opts)
        self.assertEqual(built['cues']['recent_phrases'], [])
        two_turns = []
        for _ in range(2):
            two_turns += [msg('incoming', 'a'), msg('outgoing', '같은 문장입니다 정말로요')]
        built = rc.analyze('instagram', two_turns, self.opts)
        texts = [p['text'] for p in built['cues']['recent_phrases']]
        self.assertTrue(any('같은 문장입니다' in t for t in texts))

    def test_contained_shorter_gram_is_removed(self):
        messages = []
        for _ in range(3):
            messages += [msg('incoming', 'a'),
                         msg('outgoing', '오늘은 날씨가 좋아서 산책을 다녀왔어요')]
        built = rc.analyze('instagram', messages, self.opts)
        norms = [p['norm'] for p in built['check']['phrases']]
        longest = max(norms, key=lambda n: len(n.split()))
        for norm in norms:
            if norm != longest:
                self.assertNotIn(norm, longest)

    def test_grams_of_only_short_words_are_excluded(self):
        messages = []
        for _ in range(3):
            messages += [msg('incoming', 'a'), msg('outgoing', '오늘 뭐 해 오늘 뭐 해')]
        built = rc.analyze('instagram', messages, self.opts)
        self.assertEqual(built['cues']['recent_phrases'], [])

    def test_short_conversation_gives_empty_cues(self):
        built = rc.analyze('instagram', [msg('outgoing', '안녕')], self.opts)
        self.assertEqual(built['cues']['turns_considered'], 1)
        self.assertEqual(built['cues']['recent_phrases'], [])
        metrics = rc.check_drafts(['안녕'], built['check'], self.opts)
        self.assertLess(metrics['verbatim_max'], 25)
        self.assertFalse(rc.exceeds(metrics, self.opts))

    def repetitive_check(self):
        messages = []
        for index in range(3):
            messages += [msg('incoming', '사진 보여줘'),
                         msg('outgoing', COPYSENT), msg('outgoing', '[사진]')]
        return rc.analyze('slack', messages, self.opts)['check']

    def test_check_drafts_detects_verbatim_phrase_and_edges(self):
        check = self.repetitive_check()
        metrics = rc.check_drafts([COPYSENT], check, self.opts)
        self.assertGreaterEqual(metrics['verbatim_max'], 30)
        self.assertGreaterEqual(metrics['phrase_hits'], 1)
        self.assertTrue(metrics['opener_repeat'])
        self.assertTrue(metrics['closer_repeat'])
        self.assertTrue(rc.exceeds(metrics, self.opts))
        self.assertIn('자, 말한 대로 찍었어.', metrics['matched_phrases'])

    def test_settings_validation_and_safe_fallback(self):
        with self.assertRaisesRegex(ControlError, 'invalid_repetition_guard_config'):
            rc.settings({'repetition_guard': {'mode': 'loud'}})
        self.assertEqual(rc.guard_settings({'repetition_guard': {'mode': 'loud'}})['mode'], 'off')
        self.assertEqual(rc.guard_settings({'repetition_guard': {'mode': 'shadow'}})['mode'], 'shadow')


class RepetitionGuardSessionTests(unittest.TestCase):
    setUp = session_fixtures.DecisionSessionTests.setUp
    free = session_fixtures.DecisionSessionTests.free
    prepared = session_fixtures.DecisionSessionTests.prepared
    ingest = session_fixtures.DecisionSessionTests.ingest
    decision = session_fixtures.DecisionSessionTests.decision
    activity = session_fixtures.DecisionSessionTests.activity
    review = session_fixtures.DecisionSessionTests.review
    sleep = session_fixtures.DecisionSessionTests.sleep
    recent = session_fixtures.DecisionSessionTests.recent
    draft = session_fixtures.DecisionSessionTests.draft
    traces = session_fixtures.DecisionSessionTests.traces
    snapshot = session_fixtures.DecisionSessionTests.snapshot

    def custom_recent(self):
        messages = []
        for index in range(3):
            messages.append({'id': f'o{index}', 'direction': 'outgoing', 'text': COPYSENT})
            messages.append({'id': f'p{index}', 'direction': 'outgoing', 'text': '[사진]'})
            if index < 2:
                messages.append({'id': f'u{index}', 'direction': 'incoming', 'text': '또 보여줘'})
        messages.append({'id': 'm1', 'direction': 'incoming', 'text': 'hello'})
        gate.write_json(self.state_dir / 'recent_thread.json',
                        {'fetched_ms': self.clock, 'messages': messages})

    def prepared_repetitive(self, mode):
        self.cfg['repetition_guard'] = {'mode': mode}
        self.ingest(); self.custom_recent()
        return self.runner.prepare(mode='bundled')

    def test_off_prepare_and_commit_have_no_repetition_surface(self):
        turn = self.prepared()
        result = self.runner.prepare(mode='bundled')
        self.assertNotIn('repetition_cues', result)
        state = self.runner.state(turn)
        self.assertNotIn('repetition_cues', state)
        self.assertNotIn('repetition_check', state)
        committed = self.runner.commit(turn, self.draft())
        self.assertTrue(committed['committed'])
        self.assertFalse([e for e in self.traces() if e['phase'] == 'repetition_checked'])

    def test_observe_prepare_shows_cues_and_commit_only_traces_metrics(self):
        prepared = self.prepared_repetitive('observe')
        cues = prepared['repetition_cues']['instagram']
        self.assertEqual(cues['turns_considered'], 3)
        self.assertTrue(any('말한 대로' in p['text'] for p in cues['recent_phrases']))
        committed = self.runner.commit(prepared['turn_id'], self.draft(COPYSENT))
        self.assertTrue(committed['committed'])
        self.assertNotIn('warning_review', committed['commit'])
        event = next(e for e in self.traces() if e['phase'] == 'repetition_checked')
        self.assertEqual(event['mode'], 'observe')
        self.assertGreaterEqual(event['verbatim_max'], 25)
        self.assertNotIn('말한 대로', json.dumps(event, ensure_ascii=False))

    def test_shadow_blocks_repetitive_draft_until_exact_receipt(self):
        prepared = self.prepared_repetitive('shadow')
        draft = self.draft(COPYSENT)
        blocked = self.runner.commit(prepared['turn_id'], draft)
        self.assertFalse(blocked['committed'])
        self.assertEqual(blocked['reason'], 'warning_review_required')
        warnings = blocked['commit']['conversation_groups']['g1']['warnings']
        self.assertTrue(any(w['reason'] == 'repetition_detected' for w in warnings))
        receipt = blocked['commit']['warning_review']
        changed = self.draft(COPYSENT + ' 그리고 새로운 문장을 덧붙였습니다')
        again = self.runner.commit(prepared['turn_id'], changed, receipt)
        self.assertFalse(again['committed'])
        self.assertNotEqual(again['commit']['warning_review'], receipt)
        committed = self.runner.commit(prepared['turn_id'], draft, receipt)
        self.assertTrue(committed['committed'])

    def test_shadow_combines_with_conversation_warning_in_one_receipt(self):
        conversation_flow.configure(self.store, 'main', {'mode': 'shadow'}, self.clock)
        prepared = self.prepared_repetitive('shadow')
        draft = self.draft(COPYSENT)
        blocked = self.runner.commit(prepared['turn_id'], draft)
        self.assertFalse(blocked['committed'])
        warnings = blocked['commit']['conversation_groups']['g1']['warnings']
        reasons = {w['reason'] for w in warnings}
        self.assertIn('repetition_detected', reasons)
        self.assertGreater(len(reasons), 1)
        committed = self.runner.commit(prepared['turn_id'], draft,
                                       blocked['commit']['warning_review'])
        self.assertTrue(committed['committed'])

    def test_shadow_lets_fresh_draft_commit_directly(self):
        prepared = self.prepared_repetitive('shadow')
        committed = self.runner.commit(prepared['turn_id'], self.draft('도서관에서 공부 중이야'))
        self.assertTrue(committed['committed'])

    def test_store_apply_extra_warnings_share_the_receipt(self):
        self.ingest(); self.recent()
        turn = self.runner.prepare(mode='bundled')['turn_id']
        draft = self.draft()
        extra = {'g1': [{'reason': 'repetition_detected', 'verbatim_max': 30,
                         'phrase_hits': 1, 'opener_repeat': True, 'closer_repeat': False,
                         'phrases': ['합성 구절']}]}
        before = self.snapshot()['revision']
        blocked = self.store.apply('main', draft, self.clock, guard_warnings=True,
                                   extra_warnings=extra)
        self.assertEqual(blocked['reason'], 'warning_review_required')
        self.assertEqual(self.snapshot()['revision'], before)
        applied = self.store.apply('main', draft, self.clock, guard_warnings=True,
                                   warning_review=blocked['warning_review'],
                                   extra_warnings=extra)
        self.assertEqual(applied['reason'], 'applied')

    def test_compact_section_is_optional_and_readable(self):
        self.cfg['repetition_guard'] = {'mode': 'observe'}
        self.ingest(); self.custom_recent()
        compact = self.runner.prepare(mode='compact')
        self.assertIn('repetition_cues', compact['input_manifest']['state_sections'])
        self.assertNotIn('repetition_cues', compact['input_manifest']['required_state_sections'])
        read = self.runner.read_input(compact['turn_id'], compact['input_id'], 'state',
                                      sections=['repetition_cues'])
        content = read['content']['repetition_cues']
        self.assertEqual(content['instagram']['turns_considered'], 3)
        bundle = prepared_inputs.read_bundle(self.runner, compact['turn_id'], compact['input_id'])
        committed = self.runner.commit(compact['turn_id'], self.draft(),
                                       input_receipts=bundle['input_receipts'])
        self.assertTrue(committed['committed'])

    def test_generator_source_carries_cues_only_when_enabled(self):
        prepared = self.prepared_repetitive('observe')
        state = self.runner.state(prepared['turn_id'])
        request = {'schema': 'reply-gen-v1', 'turn_id': prepared['turn_id'],
                   'channel': 'instagram', 'situation': '사진 요청에 답한다',
                   'memory_fact_ids': [], 'target_message_ids': ['m1']}
        dynamic, _ = reply_generator.source(self.runner, state, request, None)
        self.assertIn('repetition_cues', dynamic)
        self.assertEqual(dynamic['repetition_cues']['turns_considered'], 3)


class RepetitionRoleInputTests(unittest.TestCase):
    setUp = role_fixtures.RoleExecutorTests.setUp
    start = role_fixtures.RoleExecutorTests.start
    context = role_fixtures.RoleExecutorTests.context
    answer = role_fixtures.RoleExecutorTests.answer

    def test_writer_and_review_inputs_carry_cues_and_metrics(self):
        self.c.cfg['repetition_guard'] = {'mode': 'observe'}
        first = self.start()
        self.assertEqual(first['role'], 'context')
        record = self.worker.executor.load()
        self.assertIn('repetition_cues', record['prepared'])
        self.answer(self.context())
        writer_task = self.worker.load()['pending']
        self.assertEqual(writer_task['role'], 'writer')
        payload = writer_task['request']['input']
        self.assertIn('repetition_cues', payload['reply'])


if __name__ == '__main__':
    unittest.main()
