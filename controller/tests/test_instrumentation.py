"""Diagnostics follow real commits and retries without changing their contracts."""
import json
import subprocess
import sys
import unittest
from unittest.mock import patch

import test_decision_session as fixtures
from common import ControlError, encode
from dispatch import MockTransport
import conversation_flow
import instrumentation


class InstrumentationTests(unittest.TestCase):
    def setUp(self):
        self.case = fixtures.DecisionSessionTests()
        self.case.setUp()
        self.addCleanup(self.case.tearDown)

    def revisions(self):
        return [json.loads(line) for path in (self.case.path.parent / 'logs').glob('revision-*.jsonl')
                for line in path.read_text().splitlines()]

    def test_warning_retries_identify_exact_decision_and_changed_draft(self):
        c = self.case
        conversation_flow.configure(c.store, 'main', {'mode': 'shadow'}, c.clock)
        turn = c.prepared()
        draft = c.draft('private_draft_marker')
        first = c.runner.commit(turn, draft)
        c.runner.commit(turn, c.draft('changed_private_draft'))
        with patch.object(c.runner, 'finish', return_value={'next': 'done'}):
            applied = c.runner.commit(turn, draft, first['commit']['warning_review'])
        events = [e for e in c.traces() if e['phase'] == 'apply_finished']
        self.assertEqual([e['warning_codes'] for e in events], [['conversation_plan_required']] * 3)
        self.assertEqual(events[0]['decision_id'], draft['decision_id'])
        self.assertEqual(events[0]['decision_digest'], events[2]['decision_digest'])
        self.assertNotEqual(events[0]['decision_digest'], events[1]['decision_digest'])
        self.assertNotEqual(events[0]['draft_digest'], events[1]['draft_digest'])
        self.assertEqual(events[0]['actual_revision'], draft['expected_revision'])
        self.assertEqual(events[2]['result_revision'], draft['expected_revision'] + 1)
        self.assertFalse(events[0]['warning_review_supplied'])
        self.assertTrue(events[2]['warning_review_supplied'])
        self.assertTrue(applied['committed'])
        serialized = json.dumps(c.traces())
        for private in ('private_draft_marker', 'changed_private_draft', first['commit']['warning_review']):
            self.assertNotIn(private, serialized)

    def test_conflict_revision_is_from_transaction_not_later_snapshot(self):
        c = self.case
        turn = c.prepared()
        draft = c.draft()
        c.ingest('o2', direction='outgoing')
        at_conflict = c.snapshot()['revision']
        original = c.runner.store.apply

        def conflict_then_another_writer(*args, **kwargs):
            try:
                return original(*args, **kwargs)
            except ControlError:
                c.ingest('o3', direction='outgoing')
                raise

        with patch.object(c.runner.store, 'apply', side_effect=conflict_then_another_writer):
            with self.assertRaisesRegex(ControlError, 'revision_conflict'):
                c.runner.commit(turn, draft)
        event = [e for e in c.traces() if e['phase'] == 'apply_finished'][-1]
        self.assertEqual(event['expected_revision'], draft['expected_revision'])
        self.assertEqual(event['actual_revision'], at_conflict)
        self.assertEqual(c.snapshot()['revision'], at_conflict + 1)
        self.assertEqual(event['exit_code'], 2)
        self.assertEqual(c.runner.state()['turn_id'], turn)

    def test_prepare_reports_query_sizes_and_same_turn_input_changes(self):
        c = self.case
        c.store.apply('main', c.decision(facts=[{'id': 'fixture', 'source': 'simulated',
            'summary': 'topic_state:private_lookup_marker', 'details': {'private': 'private_fact_marker'}}]), c.clock)
        c.ingest(); c.recent()
        queries = [['topic_state:private_lookup_marker'], ['identity:education'], ['private_query_marker'], []]
        # Inspect actual recall bodies rather than auto mode's compact catalog.
        first = c.runner.prepare(queries, mode='bundled')
        event = c.traces()[-1]
        self.assertEqual(event['prepare_index'], 1)
        self.assertIsNone(event['revision_changed'])
        self.assertIsNone(event['context_changed'])
        self.assertEqual([q['query_kinds'] for q in event['recall_stats']],
                         [['topic_state'], ['identity'], ['other'], ['recent']])
        self.assertEqual([q['result_count'] for q in event['recall_stats']],
                         [len(r['facts']) for r in first['recalls']])
        self.assertEqual(event['recall_stats'][0]['result_count'], 1)
        self.assertEqual(event['recall_output_bytes'], len(encode(first['recalls']).encode('utf-8')))
        self.assertEqual(event['output_bytes'], len(encode(first).encode('utf-8')))
        c.runner.prepare(queries)
        event = c.traces()[-1]
        self.assertEqual(event['prepare_index'], 2)
        self.assertFalse(event['revision_changed'])
        self.assertFalse(event['context_changed'])
        self.assertFalse(event['target_messages_changed'])
        c.recent(('m1', 'outgoing-context-only'))
        c.runner.prepare()
        event = c.traces()[-1]
        self.assertFalse(event['revision_changed'])
        self.assertTrue(event['context_changed'])
        self.assertFalse(event['target_messages_changed'])
        c.ingest('m2'); c.recent(('m1', 'm2'))
        c.runner.prepare()
        event = c.traces()[-1]
        self.assertTrue(event['revision_changed'])
        self.assertTrue(event['context_changed'])
        self.assertTrue(event['target_messages_changed'])
        with c.runner.owned():
            c.runner._next(c.runner.state(), 'test_next_turn')
        c.runner.prepare()
        self.assertEqual(c.traces()[-1]['prepare_index'], 1)
        self.assertIsNone(c.traces()[-1]['revision_changed'])
        logs = json.dumps(c.traces())
        for private in ('private_lookup_marker', 'private_fact_marker', 'private_query_marker'):
            self.assertNotIn(private, logs)

    def test_only_committed_revision_changes_are_logged_including_dispatch(self):
        c = self.case
        conversation_flow.configure(c.store, 'main', {'mode': 'shadow'}, c.clock)
        turn = c.prepared()
        draft = c.draft()
        before = self.revisions()
        c.store.apply('main', draft, c.clock, dry_run=True)
        warning = c.runner.commit(turn, draft)
        self.assertEqual(self.revisions(), before)
        with patch.object(c.runner, 'finish', return_value={'next': 'done'}):
            c.runner.commit(turn, draft, warning['commit']['warning_review'])
            c.runner.commit(turn, draft)
        changes = self.revisions()[len(before):]
        self.assertEqual([e['operation'] for e in changes], ['decision_apply'])
        self.assertEqual(changes[0]['revision_before'], draft['expected_revision'])
        self.assertEqual(changes[0]['revision_after'], draft['expected_revision'] + 1)
        c.dispatch.send_due('main', MockTransport())
        changes = self.revisions()[len(before):]
        self.assertEqual([e['operation'] for e in changes],
                         ['decision_apply', 'dispatch_claim', 'dispatch_result'])
        self.assertTrue(all(a['revision_after'] == b['revision_before'] for a, b in zip(changes, changes[1:])))
        count = len(self.revisions())
        c.ingest()  # Duplicate ingest is a no-op.
        c.store.tick('main', c.clock)
        c.store._set_lease('main', c.clock + 1000, c.clock)
        c.store.end_conversation('main', c.clock)
        self.assertEqual(len(self.revisions()), count)

    def test_revision_log_failure_does_not_change_successful_write(self):
        c = self.case
        before = c.snapshot()['revision']
        with patch.object(instrumentation.os, 'open', side_effect=OSError('log unavailable')):
            result = c.ingest()
        self.assertTrue(result['ok'])
        self.assertEqual(c.snapshot()['revision'], before + 1)
        self.assertEqual(c.snapshot()['messages'][0]['id'], 'm1')

    def test_cli_recall_and_conflict_include_metadata_without_raw_queries(self):
        c = self.case
        def run(*args):
            return subprocess.run([sys.executable, str(fixtures.baseline.ROOT / 'ctl.py'),
                '--db', str(c.path), *args], capture_output=True, text=True, timeout=10)
        recall = run('recall', '--keywords', 'identity:private_cli_query')
        self.assertEqual(recall.returncode, 0, recall.stderr)
        conflict = run('apply', '--decision', encode(c.decision(expected_revision=999)))
        self.assertEqual(conflict.returncode, 2)
        records = [json.loads(line) for path in (c.path.parent / 'logs').glob('*.log')
                   for line in path.read_text().splitlines()]
        self.assertEqual(records[0]['query_kinds'], ['identity'])
        self.assertEqual(records[0]['result_count'], len(json.loads(recall.stdout)['facts']))
        self.assertEqual(records[0]['output_bytes'], len(recall.stdout.strip().encode('utf-8')))
        self.assertEqual(records[1]['expected_revision'], 999)
        self.assertEqual(records[1]['actual_revision'], c.snapshot()['revision'])
        self.assertNotIn('private_cli_query', json.dumps(records))

    def test_unstructured_decision_id_is_hashed_and_draft_digest_ignores_revision(self):
        draft = self.case.decision(decision_id='private identifier marker')
        original = instrumentation.decision_metadata(draft)
        changed = instrumentation.decision_metadata({**draft, 'expected_revision': 999})
        self.assertTrue(original['decision_id'].startswith('sha256:'))
        self.assertNotIn('private identifier marker', json.dumps(original))
        self.assertEqual(original['draft_digest'], changed['draft_digest'])
        self.assertNotEqual(original['decision_digest'], changed['decision_digest'])

    def test_prepare_race_records_recall_stats_but_keeps_existing_error(self):
        c = self.case
        turn = c.prepared()
        original = c.runner.store.recall

        def racing(*args, **kwargs):
            c.ingest('m2')
            return original(*args, **kwargs)

        with patch.object(c.runner.store, 'recall', side_effect=racing):
            with self.assertRaisesRegex(ControlError, 'prepare_inputs_changed'):
                c.runner.prepare([['identity:private_racing_query']])
        event = c.traces()[-1]
        self.assertEqual(event['phase'], 'session_prepare_failed')
        self.assertEqual(event['turn_id'], turn)
        self.assertEqual(event['actual_revision'], event['revision'] + 1)
        self.assertEqual(event['recall_stats'][0]['query_kinds'], ['identity'])
        self.assertEqual(c.runner.state()['turn_id'], turn)
        self.assertNotIn('private_racing_query', json.dumps(event))

    def test_hard_conflict_details_log_only_codes_and_do_not_weaken_rejection(self):
        c = self.case
        turn = c.prepared()
        before = self.revisions()
        error = ControlError('conversation_recent_reintroduction', encode({'conflicts': [
            {'reason': 'conversation_recent_reintroduction', 'private': 'private_conflict_marker'},
            {'reason': 'conversation_recent_reintroduction'}]}))
        with patch.object(c.runner.store, '_dm_decision', side_effect=error):
            with self.assertRaisesRegex(ControlError, 'conversation_recent_reintroduction'):
                c.runner.commit(turn, c.draft())
        event = c.traces()[-1]
        self.assertEqual(event['warning_codes'], ['conversation_recent_reintroduction'])
        self.assertEqual(event['exit_code'], 1)
        self.assertEqual(self.revisions(), before)
        self.assertEqual(c.snapshot()['jobs'], [])
        self.assertNotIn('private_conflict_marker', json.dumps(c.traces()))
