"""Hook-issued deadlines, dead-worker takeover, wait and send budget integration."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from common import ControlError
from dispatch import MockTransport
import claim_heartbeat
import gate
import test_gate
import test_decision_session as lifecycle
import wake_hook
import worker_budget


class WorkerTicketTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config_path, self.state = test_gate.GateTests().fake_cron(Path(self.tmp.name))
        self.cfg = json.loads(self.config_path.read_text())
        self.cfg['_config_path'] = str(self.config_path)
        self.cfg['worker_execution'] = {'enabled': True, 'timeout_seconds': 600,
                                        'finish_reserve_seconds': 20}
        self.cfg['decision_claim'] = {'auto_heartbeat': True}
        self.cfg['wake'] = {'enabled': True}
        self.now = gate.now_ms()

    def claim(self, ticket):
        with patch.object(claim_heartbeat, 'start'), patch.object(gate, 'now_ms', return_value=self.now):
            return gate.claim_work(self.cfg, ticket['invocation_id'])

    def test_cold_start_time_is_deducted_from_original_request_deadline(self):
        ticket = worker_budget.issue(self.cfg, self.now - 169000)
        code, wake = self.claim(ticket)
        self.assertEqual(code, 0)
        self.assertEqual(wake['claim_deadline_ms'], self.now + 431000)
        self.assertEqual(wake['worker_execution']['basis'], 'local_hook_request')
        self.assertEqual(worker_budget.remaining(wake, self.now, self.now, 1800000)['remaining_ms'], 411000)

    def test_dead_worker_keeper_expires_at_hook_deadline_and_new_ticket_takes_over(self):
        ticket = worker_budget.issue(self.cfg, self.now - 169000)
        _, old = self.claim(ticket)
        self.now = old['claim_deadline_ms']
        self.assertEqual(claim_heartbeat.pulse(self.cfg, old['wake_id'], now=self.now), 'claim_deadline')
        self.assertEqual(gate.load_json(self.state / 'wake_ack.json', {})['phase'], 'failed')
        new_ticket = worker_budget.issue(self.cfg, self.now)
        code, new = self.claim(new_ticket)
        self.assertEqual(code, 0)
        self.assertNotEqual(new['wake_id'], old['wake_id'])
        before = (self.state / 'wake_ack.json').read_bytes()
        self.assertEqual(claim_heartbeat.pulse(self.cfg, old['wake_id'], now=self.now), 'ownership_lost')
        self.assertEqual((self.state / 'wake_ack.json').read_bytes(), before)
        self.assertFalse(gate.claim_is_current(self.state, old['wake_id'], self.now))

    def test_existing_fixed_deadline_is_not_extended_by_config_or_heartbeat(self):
        ticket = worker_budget.issue(self.cfg, self.now - 169000)
        _, wake = self.claim(ticket)
        self.cfg['worker_execution']['timeout_seconds'] = 1800
        self.assertIsNone(claim_heartbeat.pulse(self.cfg, wake['wake_id'], now=self.now + 10000))
        self.assertEqual(gate.load_json(self.state / 'wake.json', {})['claim_deadline_ms'],
                         ticket['deadline_ms'])

    def test_missing_expired_and_consumed_tickets_cannot_start_a_new_claim(self):
        self.cfg['worker_execution']['require_invocation'] = True
        for invocation, reason in ((None, 'worker_invocation_required'),
                                    ('a' * 32, 'worker_invocation_unavailable'),
                                    ('../outside', 'invalid_worker_invocation')):
            with self.assertRaisesRegex(ControlError, reason):
                worker_budget.bind(self.cfg, invocation, 'new', self.now)
        expired = worker_budget.issue(self.cfg, self.now - 590000)
        with self.assertRaisesRegex(ControlError, 'worker_invocation_expired'):
            worker_budget.bind(self.cfg, expired['invocation_id'], 'new', self.now)
        ticket = worker_budget.issue(self.cfg, self.now)
        worker_budget.bind(self.cfg, ticket['invocation_id'], 'one', self.now)
        with self.assertRaisesRegex(ControlError, 'worker_invocation_consumed'):
            worker_budget.bind(self.cfg, ticket['invocation_id'], 'two', self.now)

    def test_guarded_claim_rejects_missing_ticket_without_starting_keeper(self):
        self.cfg['worker_execution']['require_invocation'] = True
        with patch.object(claim_heartbeat, 'start') as start:
            code, wake = gate.claim_work(self.cfg)
        self.assertEqual(code, 2)
        self.assertIsNone(wake)
        start.assert_not_called()
        self.assertFalse((self.state / 'wake_ack.json').exists())

    def test_cron_without_ticket_gets_explicit_entry_fallback_not_a_fake_runtime_start(self):
        with patch.object(claim_heartbeat, 'start'), patch.object(gate, 'now_ms', return_value=self.now):
            code, wake = gate.claim_work(self.cfg)
        self.assertEqual(code, 0)
        self.assertEqual(wake['worker_execution']['basis'], 'worker_entry_fallback')
        self.assertEqual(wake['claim_deadline_ms'], self.now + 600000)

    def test_hook_creates_ticket_before_wake_but_dry_run_writes_nothing(self):
        report = lambda: {'wake_eligible': True, 'reasons': ['new_dm'], 'revision': 1}
        dry = wake_hook.check(self.cfg, gate_check=report, now=self.now, dry_run=True)
        self.assertEqual(dry['action'], 'wake')
        self.assertFalse((self.state / 'worker-invocations').exists())
        result = wake_hook.check(self.cfg, gate_check=report, now=self.now)
        invocation = result['payload']['controller_invocation_id']
        saved = json.loads(worker_budget.ticket_path(self.cfg, invocation).read_text())
        self.assertEqual(saved['deadline_ms'], self.now + 600000)
        self.assertNotIn('claimed_wake_id', saved)

    def test_deadline_is_enforced_without_a_keeper_or_auto_heartbeat(self):
        self.cfg['decision_claim']['auto_heartbeat'] = False
        ticket = worker_budget.issue(self.cfg, self.now)
        _, wake = self.claim(ticket)
        self.assertFalse(gate.claim_is_current(self.state, wake['wake_id'], ticket['deadline_ms']))
        state = gate.load_json(self.state / 'gate_state.json', {})
        ack = gate.load_json(self.state / 'wake_ack.json', {})
        detail = {'fingerprint_basis': {'new': 'message'}}
        self.assertEqual(gate.decide(['new_dm'], detail, state, ticket['deadline_ms'], ack), (True, 1))

    def test_life_deadline_does_not_wait_for_retry_delay_when_keeper_is_dead(self):
        reasons = ['activity_due', 'decision_due']
        detail = {'fingerprint_basis': {'same_life_work': True}}
        gate.decide(reasons, detail, {}, self.now)
        state = {'fingerprint': detail['fingerprint'], 'wake_id': 'old', 'attempts': 1,
                 'last_wake_ms': self.now, 'claim_deadline_ms': self.now + 431000}
        ack = {'wake_id': 'old', 'phase': 'started', 'at_ms': self.now + 430999}
        self.assertEqual(gate.decide(reasons, detail, state, self.now + 430999, ack), (False, 1))
        self.assertEqual(gate.decide(reasons, detail, state, self.now + 431000, ack), (True, 2))
        state['attempts'] = 3  # Exhaustion backoff remains bounded.
        self.assertEqual(gate.decide(reasons, detail, state, self.now + 431000, ack), (False, 3))

    def test_invalid_budget_configuration_is_rejected(self):
        for settings in (None, [], {'enabled': 'yes'}, {'timeout_seconds': True},
                         {'timeout_seconds': 0}, {'timeout_seconds': 1801},
                         {'finish_reserve_seconds': 600}):
            with self.subTest(settings=settings), self.assertRaises(ControlError):
                worker_budget.policy({'worker_execution': settings})


class WorkerSessionBudgetTests(unittest.TestCase):
    def setUp(self):
        self.case = lifecycle.DecisionSessionTests()
        self.case.setUp()
        self.addCleanup(self.case.tearDown)
        c = self.case
        self.deadline = c.clock + 431000
        execution = {'invocation_id': 'a' * 32, 'requested_ms': c.clock - 169000,
                     'deadline_ms': self.deadline, 'finish_reserve_ms': 20000,
                     'basis': 'local_hook_request'}
        for name in ('wake', 'gate_state'):
            gate.write_json(c.state_dir / (name + '.json'), {'wake_id': 'wake-1',
                'claim_deadline_ms': self.deadline, 'worker_execution': execution})

    def test_wait_uses_remaining_budget_and_hands_off_without_ending_conversation(self):
        c = self.case
        turn = c.prepared()
        result = c.runner.commit(turn, c.draft())
        self.assertTrue(result['committed'])
        self.assertEqual(result['after']['reason'], 'worker_budget')
        self.assertEqual(result['after']['next'], 'done')
        self.assertLess(c.clock, self.deadline)
        self.assertFalse(c.runner.state()['conversation_ended'])
        self.assertIsNone(c.snapshot()['conversation_lease_until_ms'])
        self.assertEqual(gate.load_json(c.state_dir / 'wake_ack.json', {})['reason'], 'worker_budget')
        with c.store.connect() as db:
            self.assertEqual(db.execute('SELECT status FROM dm_jobs').fetchone()[0], 'confirmed')
        trace = [json.loads(line) for path in c.state_dir.glob('decision-trace-*.jsonl')
                 for line in path.read_text().splitlines()]
        wait = next(event for event in trace if event['phase'] == 'session_wait_started')
        self.assertLessEqual(wait['timeout_seconds'], 411)

    def test_prepare_exposes_actual_remaining_time_not_thirty_minutes(self):
        c = self.case
        c.ingest(); c.recent()
        prepared = c.runner.prepare()
        self.assertEqual(prepared['constraints']['execution_budget']['remaining_ms'], 411000)

    def test_no_time_left_before_prepare_releases_unfinished_work_without_dummy_commit(self):
        c = self.case
        c.ingest(); c.recent()
        c.clock = self.deadline - 19500
        with patch.object(c.runner.store, 'apply', side_effect=AssertionError('no dummy decision')):
            result = c.runner.prepare()
        self.assertEqual(result['next'], 'done')
        self.assertEqual(result['reason'], 'worker_budget')
        self.assertEqual(gate.load_json(c.state_dir / 'wake_ack.json', {})['phase'], 'failed')
        self.assertEqual(c.snapshot()['messages'][0]['status'], 'received')

    def test_near_deadline_saved_reply_stays_pending_for_poller(self):
        c = self.case
        turn = c.prepared()
        c.clock = self.deadline - 19500
        c.recent()
        with patch.object(c.runner.dispatch, 'send_due', side_effect=AssertionError('no time for sends')):
            result = c.runner.commit(turn, c.draft())
        self.assertTrue(result['committed'])
        self.assertEqual(result['after']['reason'], 'worker_budget')
        self.assertEqual(c.snapshot()['jobs'][0]['status'], 'pending')

    def test_send_budget_stops_before_network_claim_and_does_not_drop_queue(self):
        c = self.case
        c.prepared()
        c.store.apply('main', c.draft(), c.clock)
        with patch.object(MockTransport, 'send', side_effect=AssertionError('not enough time')):
            result = c.dispatch.send_due('main', MockTransport(), deadline_ms=c.clock + 20000)
        self.assertEqual(result['reason'], 'worker_budget')
        self.assertEqual(result['results'], [])
        self.assertEqual(c.snapshot()['jobs'][0]['status'], 'pending')

    def test_multibubble_send_yields_before_starting_part_without_enough_time(self):
        c = self.case
        c.prepared()
        decision = c.draft()
        parts = ['one', 'two', 'three']
        decision['dm_decisions'][0]['parts'] = parts
        decision['dm_decisions'][0]['conversation_review']['draft_parts'] = parts
        c.store.apply('main', decision, c.clock)
        transport = MockTransport()
        original = transport.send
        def slow_send(job):
            c.sleep(10)
            return original(job)
        transport.send = slow_send
        result = c.dispatch.send_due('main', transport, deadline_ms=c.clock + 45000)
        self.assertEqual(result['reason'], 'worker_budget')
        with c.store.connect() as db:
            statuses = [row[0] for row in db.execute('SELECT status FROM dm_jobs ORDER BY part_index')]
        self.assertEqual(statuses, ['confirmed', 'confirmed', 'pending'])

    def test_new_dm_composing_lease_cannot_outlive_worker_budget(self):
        c = self.case
        c.clock = self.deadline - 50000
        def incoming(seconds):
            c.sleep(seconds)
            c.ingest('m2')
        result = c.store.wait_dm('main', 30, lambda: c.clock, incoming,
                                 lease_deadline_ms=self.deadline - 20000)
        self.assertEqual(result['reason'], 'new_dm')
        self.assertLessEqual(c.snapshot()['conversation_lease_until_ms'], self.deadline - 20000)


if __name__ == '__main__':
    unittest.main()
