"""Offline real-SQLite coverage for the bundled decision lifecycle."""
import fcntl
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

import test_controller as baseline
import test_reply_latency as latency
from common import ControlError
from dispatch import MockTransport
from decision_session import DecisionSession, MAX_RUN_MS
import conversation_flow
import memory_continuity
import prepared_inputs
import gate


class DecisionSessionTests(unittest.TestCase):
    def test_newly_realized_event_can_register_conversation_material_in_same_apply(self):
        from test_world_events import iso
        self.store.event_import('main',{'schema_version':1,'request_id':'pending',
            'events':[{'event_id':'scene','mode':'scene','summary':'small scene','known_by':['self'],
                       'not_before':iso(self.clock),'expires_at':iso(self.clock+3600000)}]},self.clock,'user:test')
        result=self.store.apply('main',self.decision(
            world_event_ops=[{'action':'realize','event_id':'scene','reason':'scene progressed'}],
            conversation_updates=[{'op':'register_material','ref':'event-material',
                'canonical_key':'experience:scene','subject_key':'daily:scene','kind':'experience',
                'fact_ids':['world-event:scene']}]),self.clock)
        self.assertIn('event-material',result['conversation_refs'])

    def test_pending_events_do_not_enter_pure_dm_prepare(self):
        from test_world_events import iso
        self.store.event_import('main',{'schema_version':1,'request_id':'pending',
            'events':[{'event_id':'candidate','mode':'candidate','summary':'small event','known_by':['self'],
                       'not_before':iso(self.clock),'expires_at':iso(self.clock+3600000)}]},self.clock,'user:test')
        self.ingest(); self.recent()
        prepared=self.runner.prepare()
        self.assertEqual(prepared['snapshot']['work_manifest']['primary'],'dm')
        self.assertNotIn('pending',prepared['snapshot']['world_events'])
        self.assertNotIn('runbooks/world-events.md',prepared['documents'])

    def test_life_prepare_includes_event_contract_and_preserves_optional_review(self):
        from test_world_events import iso
        self.store.event_import('main',{'schema_version':1,'request_id':'pending',
            'events':[{'event_id':'scene','mode':'scene','summary':'small scene','known_by':['self'],
                       'not_before':iso(self.clock),'expires_at':iso(self.clock+3600000)}]},self.clock,'user:test')
        prepared=self.runner.prepare()
        self.assertEqual(prepared['snapshot']['world_events']['pending'][0]['event_id'],'scene')
        self.assertIn('runbooks/world-events.md',prepared['documents'])

    tearDown = baseline.ControllerTests.tearDown
    snapshot = baseline.ControllerTests.snapshot
    decision = baseline.ControllerTests.decision
    review = baseline.ControllerTests.review
    ingest = baseline.ControllerTests.ingest
    activity = baseline.ControllerTests.activity
    free = latency.ReplyLatencyTests.free

    def setUp(self):
        baseline.ControllerTests.setUp(self)
        self.free()
        self.state_dir = Path(self.tmp.name) / 'automation-state'
        self.cfg = {'db': str(self.path), 'state_dir': str(self.state_dir), 'session': 'main'}
        for name in ('wake', 'gate_state'):
            gate.write_json(self.state_dir / (name + '.json'), {'wake_id': 'wake-1'})
        gate.write_json(self.state_dir / 'wake_ack.json',
                        {'wake_id': 'wake-1', 'phase': 'started', 'at_ms': self.clock})
        self.runner = DecisionSession(self.cfg, 'wake-1', lambda: self.clock, self.sleep)
        self.runner.transport = lambda channel: MockTransport()

    def sleep(self, seconds):
        self.clock += int(seconds * 1000)

    def recent(self, ids=('m1',), channel='instagram', outgoing=()):
        name = 'recent_thread_slack.json' if channel == 'slack' else 'recent_thread.json'
        gate.write_json(self.state_dir / name, {'fetched_ms': self.clock,
            'messages': [{'id': mid, 'direction': 'incoming', 'text': 'hello'} for mid in ids]
                        + [{'id': mid, 'direction': 'outgoing', 'text': 'echo'} for mid in outgoing]})
        if channel == 'slack':
            gate.write_json(self.state_dir / 'slack_health.json',
                            {'connected': True, 'last_heartbeat_ms': self.clock})

    def draft(self, body='reply'):
        return self.decision(dm_decisions=[{'action': 'reply', 'message_ids': ['m1'],
            'group_id': 'g1', 'parts': [body], 'due_ms': self.clock, 'reason': 'answer'}],
            processed_event_ids=['dm:m1'])

    def prepared(self):
        self.ingest(); self.recent()
        # These lifecycle tests exercise bundled commits; auto is tested separately.
        return self.runner.prepare(mode='bundled')['turn_id']

    def test_prepare_batches_reads_and_preserves_turn_and_revision(self):
        turn = self.prepared()
        first = self.runner.prepare([['topic_state:book'], ['identity:education']], mode='bundled')
        second = self.runner.prepare(known_core_rules_hash=first['snapshot']['core_rules_hash'], mode='bundled')
        self.assertEqual(first['turn_id'], turn)
        self.assertEqual(first['snapshot']['revision'], second['snapshot']['revision'])
        self.assertEqual(len(first['recalls']), 2)
        self.assertIn('CORE_RULES.md', first['documents'])
        self.assertNotIn('CORE_RULES.md', second['documents'])
        self.assertEqual(first['contexts']['instagram']['missing_target_ids'], [])
        self.assertFalse(first['constraints']['context_is_atomic_with_db'])

    def test_dm_policy_sources_are_delivered_in_both_modes_and_require_compact_receipts(self):
        self.ingest(); self.recent()
        names = ('design/conversation-rules.md', 'design/reply-context.md',
                 'runbooks/memory-continuity.md')
        bundled = self.runner.prepare(mode='bundled')
        for name in names:
            self.assertEqual(bundled['documents'][name],
                             (Path(__file__).resolve().parents[1] / name).read_text(encoding='utf-8'))
        compact = self.runner.prepare(mode='compact')
        bundle = prepared_inputs.read_bundle(self.runner, compact['turn_id'], compact['input_id'])
        documents = {item['content']['name']: item['content']['text']
                     for item in bundle['components'] if item['kind'] == 'document'}
        for name in names:
            self.assertEqual(documents[name], bundled['documents'][name])
        receipt = bundle['document_receipts']['design/conversation-rules.md']
        with self.assertRaisesRegex(ControlError, 'document_input_required'):
            self.runner.commit(compact['turn_id'], self.draft(),
                               input_receipts=[r for r in bundle['input_receipts'] if r != receipt])
        self.assertEqual(self.snapshot()['jobs'], [])
        result = self.runner.commit(compact['turn_id'], self.draft(),
                                    input_receipts=bundle['input_receipts'])
        self.assertTrue(result['committed'])

    def test_non_dm_prepare_keeps_shared_rules_for_possible_initiate(self):
        prepared = self.runner.prepare(mode='bundled')
        self.assertNotEqual(prepared['snapshot']['work_manifest']['primary'], 'dm')
        self.assertIn('design/conversation-rules.md', prepared['documents'])

    def test_activity_boundary_prepare_includes_history_and_selection_guidance(self):
        self.clock += 3601000
        self.ingest(); self.recent()
        # This assertion reads the payload; auto may choose compact as documents grow.
        prepared = self.runner.prepare(mode='bundled')
        self.assertEqual(prepared['snapshot']['work_manifest']['primary'],'dm')
        self.assertIn('activity_due',prepared['snapshot']['work_manifest']['life_required'])
        self.assertEqual(prepared['snapshot']['activity_history']['today'][0]['id'],'a1')
        self.assertIn('runbooks/activity-selection.md',prepared['documents'])
        budgets = prepared['snapshot']['activity_history']['duration_guidance']
        self.assertEqual(next(b['default_minutes'] for b in budgets if b['kind']=='laundry'),10)

    def test_plain_dm_prepare_omits_activity_selection_payload(self):
        self.prepared()
        prepared = self.runner.prepare()
        self.assertNotIn('activity_history',prepared['snapshot'])
        self.assertNotIn('runbooks/activity-selection.md',prepared['documents'])

    def test_continuation_commit_and_refill_prepare_reuse_owned_session(self):
        turn = self.prepared()
        groups = [
            {'action':'reply','message_ids':['m1'],'group_id':'continue1','parts':['first'],
             'due_ms':self.clock,'reason':'requested continuation','continuation':{'id':'run1','generation':1}},
            *[{'action':'initiate','channel':'instagram','group_id':'continue'+str(i),'parts':[str(i)],
               'due_ms':self.clock,'reason':'next segment','continuation':{'id':'run1','generation':1}}
              for i in (2,3)]]
        draft = self.decision(continuation_ops=[{'action':'start','id':'run1',
            'source_message_id':'m1','reason':'user requested continuation'}],dm_decisions=groups,
            processed_event_ids=['dm:m1'])
        committed = self.runner.commit(turn,draft,conversation_ended=True)
        self.assertTrue(committed['committed'])
        self.assertEqual(self.snapshot()['continuations'][0]['buffered'],2)
        self.clock += 60000
        self.runner.dispatch.send_due('main',MockTransport())
        # Re-enter prepare with a fresh wake, as the gate would after decision_due.
        for name in ('wake','gate_state'):
            gate.write_json(self.state_dir/(name+'.json'),{'wake_id':'wake-2'})
        gate.write_json(self.state_dir/'wake_ack.json',
                        {'wake_id':'wake-2','phase':'started','at_ms':self.clock})
        self.runner = DecisionSession(self.cfg,'wake-2',lambda:self.clock,self.sleep)
        self.recent()
        prepared = self.runner.prepare()
        self.assertEqual(prepared['snapshot']['continuation_due_ids'],['run1'])
        self.assertIn('runbooks/continuation.md',prepared['documents'])
        self.assertEqual(prepared['snapshot']['work_manifest']['primary'],'life')

    def test_cli_prepare_resolves_paths_relative_to_config(self):
        self.ingest(); self.recent()
        config = Path(self.tmp.name) / 'automation.json'
        config.write_text(json.dumps({**self.cfg, 'db': 'test.db', 'state_dir': 'automation-state'}))
        command = [sys.executable, str(baseline.ROOT / 'decision_session.py'),
                   '--config', str(config), '--wake-id', 'wake-1', 'prepare']
        result = subprocess.run(command, cwd='/tmp', capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['reason'], 'prepared')

    def test_state_write_error_after_commit_reports_success_and_recovers(self):
        turn = self.prepared(); draft = self.draft()
        original = gate.write_json
        def broken(path, data):
            if Path(path) == self.runner.path and data.get('committed_turn'):
                raise OSError('disk full')
            return original(path, data)
        with patch.object(gate, 'write_json', side_effect=broken):
            result = self.runner.commit(turn, draft)
        self.assertTrue(result['committed'])
        self.assertEqual(result['after']['next'], 'retry_same_commit')
        with patch.object(self.runner, 'finish', return_value={'next': 'done'}):
            retry = self.runner.commit(turn, draft)
        self.assertEqual(retry['commit']['reason'], 'already_applied')
        self.assertEqual(self.runner.state()['turn_count'], 1)

    def test_prepare_detects_mutation_during_recall(self):
        self.prepared()
        original = self.runner.store.recall
        def racing(*args, **kwargs):
            self.ingest('m2')
            return original(*args, **kwargs)
        with patch.object(self.runner.store, 'recall', side_effect=racing):
            with self.assertRaisesRegex(ControlError, 'prepare_inputs_changed'):
                self.runner.prepare([['topic_state']])

    def test_context_lag_blocks_reply_without_writes(self):
        self.ingest(); self.recent(ids=())
        prepared = self.runner.prepare()
        self.assertEqual(prepared['contexts']['instagram']['missing_target_ids'], ['m1'])
        with self.assertRaisesRegex(ControlError, 'reply_context_not_ready'):
            self.runner.commit(prepared['turn_id'], self.draft())
        self.assertEqual(self.snapshot()['jobs'], [])

    def test_shadow_warning_rolls_back_then_exact_review_allows_commit(self):
        conversation_flow.configure(self.store, 'main', {'mode': 'shadow'}, self.clock)
        turn = self.prepared(); draft = self.draft()
        draft['facts'] = [{'id': 'new-fact', 'source': 'observed', 'summary': 'private_marker',
                           'details': {'evidence': 'fixture'}}]
        before = self.snapshot()['revision']
        blocked = self.runner.commit(turn, draft)
        self.assertFalse(blocked['committed'])
        self.assertEqual(self.snapshot()['revision'], before)
        self.assertEqual(self.store.recall('main', ['private_marker'])['facts'], [])
        receipt = blocked['commit']['warning_review']
        changed = json.loads(json.dumps(draft)); changed['facts'][0]['summary'] = 'changed'
        self.assertFalse(self.runner.commit(turn, changed, receipt)['committed'])
        with patch.object(self.runner, 'finish', return_value={'next': 'done'}):
            committed = self.runner.commit(turn, draft, receipt)
            replay = self.runner.commit(turn, draft, receipt)
        self.assertTrue(committed['committed'])
        self.assertEqual(replay['commit']['reason'], 'already_applied')
        self.assertEqual(self.runner.state()['turn_count'], 1)
        self.assertEqual(len(self.snapshot()['jobs']), 1)

    def test_revision_conflict_after_warning_review_does_not_apply(self):
        conversation_flow.configure(self.store, 'main', {'mode': 'shadow'}, self.clock)
        turn = self.prepared(); draft = self.draft()
        receipt = self.runner.commit(turn, draft)['commit']['warning_review']
        self.ingest('o2', direction='outgoing')
        with self.assertRaisesRegex(ControlError, 'revision_conflict'):
            self.runner.commit(turn, draft, receipt)
        self.assertEqual(self.snapshot()['jobs'], [])

    def test_shadow_memory_failure_commits_reply_once_and_traces_rejection(self):
        memory_continuity.configure(self.store,'main',{'mode':'shadow'},self.clock)
        turn=self.prepared();draft=self.draft();draft['memory_ops']=[{'op':'bad'}]
        with patch.object(self.runner,'finish',return_value={'next':'done'}):
            result=self.runner.commit(turn,draft,conversation_ended=True)
            replay=self.runner.commit(turn,draft,conversation_ended=True)
        self.assertTrue(result['committed'])
        self.assertEqual(result['commit']['memory_report']['status'],'shadow_rejected')
        self.runner.dispatch.send_due('main',MockTransport(),channel='instagram')
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT status FROM dm_jobs WHERE id='g1:0'").fetchone()[0],'confirmed')
            self.assertEqual(db.execute("SELECT count(*) FROM dm_jobs WHERE session_id='main'").fetchone()[0],1)
        self.assertEqual(replay['commit']['reason'],'already_applied')
        self.assertEqual(self.runner.state()['turn_count'],1)
        self.assertEqual(self.store.memory_diagnostics('main')['total'],1)
        event=next(p for p in self.traces() if p['phase']=='apply_finished' and p.get('reason')=='applied')
        self.assertEqual(event['exit_code'],0)
        self.assertEqual(event['memory_status'],'shadow_rejected')
        self.assertEqual(event['memory_error_code'],'invalid_memory_remember')

    def test_shadow_memory_diagnostic_waits_for_exact_conversation_review(self):
        memory_continuity.configure(self.store,'main',{'mode':'shadow'},self.clock)
        conversation_flow.configure(self.store,'main',{'mode':'shadow'},self.clock)
        turn=self.prepared();draft=self.draft();draft['memory_ops']=[{'op':'bad'}]
        blocked=self.runner.commit(turn,draft)
        self.assertFalse(blocked['committed'])
        self.assertEqual(self.store.memory_diagnostics('main')['total'],0)
        self.assertEqual(self.snapshot()['jobs'],[])
        receipt=blocked['commit']['warning_review']
        committed=self.runner.commit(turn,draft,receipt,conversation_ended=True)
        self.assertTrue(committed['committed'])
        self.assertEqual(self.store.memory_diagnostics('main')['total'],1)
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT status FROM dm_jobs WHERE id='g1:0'").fetchone()[0],'confirmed')

    def test_retry_after_db_commit_before_session_state_write_uses_saved_digest(self):
        turn = self.prepared(); draft = self.draft()
        self.store.apply('main', draft, self.clock)
        (self.state_dir / 'recent_thread.json').unlink()
        result = self.runner.commit(turn, draft, conversation_ended=True)
        self.assertEqual(result['commit']['reason'], 'already_applied')
        self.assertTrue(result['committed'])
        self.assertEqual(self.runner.state()['turn_count'], 1)
        self.assertEqual(len(self.snapshot()['jobs']), 1)

    def test_context_and_dispatch_use_slack_instead_of_default_instagram(self):
        self.ingest('s1', observation={'identity': 'platform', 'channel': 'slack'})
        self.recent(('s1',), 'slack')
        turn = self.runner.prepare(mode='bundled')['turn_id']
        draft = self.decision(dm_decisions=[{'action': 'reply', 'message_ids': ['s1'],
            'group_id': 'slack-group', 'parts': ['answer'], 'due_ms': self.clock, 'reason': 'answer'}])
        with patch.object(self.runner.dispatch, 'send_due', return_value={'ok': True, 'reason': 'busy'}) as send:
            result = self.runner.commit(turn, draft, conversation_ended=True)
        self.assertTrue(result['committed'])
        self.assertEqual(send.call_args.kwargs['channel'], 'slack')

    def test_legacy_apply_still_accepts_shadow_warnings(self):
        conversation_flow.configure(self.store, 'main', {'mode': 'shadow'}, self.clock)
        self.prepared()
        result = self.store.apply('main', self.draft(), self.clock)
        self.assertEqual(result['reason'], 'applied')
        self.assertTrue(result['conversation_groups']['g1']['warnings'])

    def test_enforce_error_is_not_weakened_by_warning_guard(self):
        conversation_flow.configure(self.store, 'main', {'mode': 'enforce'}, self.clock)
        turn = self.prepared()
        with self.assertRaisesRegex(ControlError, 'conversation_plan_required'):
            self.runner.commit(turn, self.draft())
        self.assertEqual(self.snapshot()['jobs'], [])

    def test_night_remains_quiet_and_does_not_wait(self):
        # T is 12:00 KST; use the following 01:00 with a valid current activity.
        self.clock += 13 * 3600000
        with self.store.connect(True) as db:
            db.execute('UPDATE activities SET planned_end_ms=?', (self.clock + 3600000,))
            db.execute('UPDATE sessions SET next_decision_ms=?', (self.clock + 3600000,))
        self.assertEqual(self.snapshot()['conversation_mode'], 'quiet')
        turn = self.prepared()
        with patch.object(self.runner.store, 'wait_dm', side_effect=AssertionError('night must not wait')):
            result = self.runner.commit(turn, self.draft())
        self.assertTrue(result['committed'])
        self.assertEqual(self.snapshot()['jobs'], [])  # confirmed, hidden from pending snapshot
        phases = self.traces()
        self.assertTrue(any(p.get('reason') == 'mode_quiet' for p in phases))

    def test_live_wait_receives_new_dm_without_gate_or_db_lock_during_sleep(self):
        turn = self.prepared()
        def incoming(seconds):
            with open(self.state_dir / 'gate.lock', 'a+b') as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.sleep(seconds)
                self.ingest('m2')
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        self.runner.sleep = incoming
        result = self.runner.commit(turn, self.draft())
        self.assertEqual(result['after']['reason'], 'new_dm')
        self.assertEqual(result['after']['next'], 'prepare')
        self.assertIsNone(self.runner.state()['turn_id'])
        self.recent(('m1', 'm2'))
        self.assertNotEqual(self.runner.prepare()['turn_id'], turn)

    def test_replaced_claim_cannot_delete_new_owner_lease(self):
        turn = self.prepared()
        def takeover(seconds):
            self.sleep(seconds)
            gate.write_json(self.state_dir / 'wake.json', {'wake_id': 'replacement'})
            self.store._set_lease('main', self.clock + 999999, self.clock)
        self.runner.sleep = takeover
        result = self.runner.commit(turn, self.draft())
        self.assertTrue(result['committed'])
        self.assertEqual(result['after']['reason'], 'stale_claim')
        self.assertEqual(self.snapshot()['conversation_lease_until_ms'], self.clock + 999999)
        with self.assertRaisesRegex(ControlError, 'stale_claim'):
            self.runner.close(turn)

    def test_transport_errors_do_not_undo_or_regenerate_saved_reply(self):
        turn = self.prepared()
        def invalid_transport(channel):
            raise ControlError('transport_config_required')
        self.runner.transport = invalid_transport
        result = self.runner.commit(turn, self.draft(), conversation_ended=True)
        self.assertTrue(result['committed'])
        self.assertEqual(result['after']['send']['instagram']['reason'], 'transport_config_required')
        self.assertEqual(len(self.snapshot()['jobs']), 1)

    def test_no_transport_config_never_defaults_to_mock(self):
        real = DecisionSession(self.cfg, 'wake-1', lambda: self.clock, self.sleep)
        with self.assertRaisesRegex(ControlError, 'transport_config_required'):
            real.transport('instagram')

    def test_session_limit_finishes_and_keeps_due_work_for_next_gate(self):
        turn = self.prepared()
        state = self.runner.state(); state['started_ms'] = self.clock - MAX_RUN_MS
        gate.write_json(self.runner.path, state)
        result = self.runner.commit(turn, self.draft())
        self.assertEqual(result['after']['reason'], 'session_limit')
        self.assertEqual(result['after']['next'], 'done')
        self.assertEqual(gate.load_json(self.state_dir / 'wake_ack.json', {})['phase'], 'finished')

    def test_wait_deadline_does_not_extend_session_into_background(self):
        turn = self.prepared()
        state = self.runner.state(); state['started_ms'] = self.clock - MAX_RUN_MS + 2000
        gate.write_json(self.runner.path, state)
        result = self.runner.commit(turn, self.draft())
        self.assertEqual(result['after']['reason'], 'session_limit')
        self.assertEqual(result['after']['next'], 'done')
        self.assertIsNone(self.snapshot()['conversation_lease_until_ms'])
        self.assertTrue(any(p['phase'] == 'session_wait_started' for p in self.traces()))

    def test_background_check_runs_once_and_can_close_without_dummy_apply(self):
        turn = self.prepared()
        result = self.runner.commit(turn, self.draft(), conversation_ended=True)
        self.assertEqual(result['after']['next'], 'prepare')
        self.assertEqual(result['after']['reason'], 'background_check')
        prepared = self.runner.prepare()
        self.assertEqual(prepared['snapshot']['work_manifest']['primary'], 'background')
        self.assertEqual(self.runner.close(prepared['turn_id'])['next'], 'done')

    def test_trace_contains_metadata_not_message_or_decision_body(self):
        self.ingest(text='private_message_marker'); self.recent()
        self.runner.prepare([['private_lookup_marker']])
        logs = '\n'.join(p.read_text() for p in self.state_dir.glob('decision-trace-*.jsonl'))
        self.assertNotIn('private_message_marker', logs)
        self.assertNotIn('private_lookup_marker', logs)
        self.assertIn('session_prepare', logs)

    def traces(self):
        return [json.loads(line) for p in self.state_dir.glob('decision-trace-*.jsonl')
                for line in p.read_text().splitlines()]

    def boundary_fixture(self):
        with self.store.connect(True) as db:
            for i,label in enumerate(('쉬기','폰 보기','영상 보기')):
                start=self.clock-(3-i)*3600000
                db.execute("""INSERT INTO activities(session_id,id,category,label,status,start_ms,
                    planned_end_ms,actual_end_ms,location,attention,interruptibility,visibility,details_json)
                    VALUES ('main',?,'rest',?,'completed',?,?,?,'home','free','anytime','personal',?)""",
                    ('past-rest-'+str(i),label,start,start+1800000,start+1800000,
                     json.dumps({'reason':'fixture','activity_key':'rest:variant'+str(i)})))

    def check_boundary_inputs(self, mode):
        self.boundary_fixture(); self.clock+=3601000
        before=self.snapshot()
        prepared=self.runner.prepare(mode=mode)
        names=('design/lifestyle.md','design/hobby-baselines.md')
        if mode=='compact':
            for name in names:
                self.assertIn(name,prepared['input_manifest']['documents'])
                doc=self.runner.read_input(prepared['turn_id'],prepared['input_id'],'document',name=name)
                self.assertEqual(doc['content']['text'],(baseline.ROOT/name).read_text())
        else:
            for name in names:
                self.assertEqual(prepared['documents'][name],(baseline.ROOT/name).read_text())
        after=self.snapshot()
        self.assertEqual(before['revision'],after['revision'])
        self.assertEqual(before['needs_decision'],after['needs_decision'])

    def test_boundary_inputs_deliver_own_documents_in_compact_mode(self):
        self.check_boundary_inputs('compact')

    def test_boundary_inputs_deliver_own_documents_in_bundled_mode(self):
        self.check_boundary_inputs('bundled')

    def test_plain_dm_does_not_expand_for_passive_history(self):
        self.boundary_fixture(); self.ingest(); self.recent()
        prepared=self.runner.prepare(mode='compact')
        self.assertEqual(prepared['snapshot']['work_manifest']['primary'],'dm')
        for name in ('design/lifestyle.md','design/hobby-baselines.md'):
            self.assertNotIn(name,prepared['input_manifest']['documents'])

    def test_due_life_boundary_during_dm_retains_advisory_and_documents(self):
        self.boundary_fixture(); self.clock+=3601000; self.ingest(); self.recent()
        prepared=self.runner.prepare(mode='compact')
        self.assertEqual(prepared['snapshot']['work_manifest']['primary'],'dm')
        self.assertIn('runbooks/life-boundaries.md',prepared['input_manifest']['documents'])
        self.assertIn('design/hobby-baselines.md',prepared['input_manifest']['documents'])


class ActiveClaimTests(unittest.TestCase):
    def test_new_fingerprint_cannot_replace_live_worker(self):
        state = {'fingerprint': 'old', 'wake_id': 'worker', 'attempts': 1, 'last_wake_ms': 1000}
        ack = {'wake_id': 'worker', 'phase': 'started', 'at_ms': 1000}
        detail = {'fingerprint_basis': {'messages': ['new-dm']}}
        self.assertEqual(gate.decide(['new_dm'], detail, state, 1001, ack), (False, 1))
        self.assertEqual(gate.decide(['new_dm'], detail, state, 1000 + gate.WORKER_STALE_MS, ack), (True, 1))
