"""Messages arriving while a reply is composed, and typing-burst settling; offline only."""
import unittest
from unittest.mock import patch

from common import ControlError
from dispatch import MockTransport
import gate
import prepared_inputs
import store as store_module
import test_decision_session as lifecycle


class LateIncomingTests(unittest.TestCase):
    setUp = lifecycle.DecisionSessionTests.setUp
    tearDown = lifecycle.DecisionSessionTests.tearDown
    snapshot = lifecycle.DecisionSessionTests.snapshot
    decision = lifecycle.DecisionSessionTests.decision
    review = lifecycle.DecisionSessionTests.review
    ingest = lifecycle.DecisionSessionTests.ingest
    activity = lifecycle.DecisionSessionTests.activity
    free = lifecycle.DecisionSessionTests.free
    sleep = lifecycle.DecisionSessionTests.sleep
    recent = lifecycle.DecisionSessionTests.recent
    draft = lifecycle.DecisionSessionTests.draft
    prepared = lifecycle.DecisionSessionTests.prepared

    def commit(self, turn, draft):
        with patch.object(self.runner, 'finish', return_value={'next': 'done'}):
            return self.runner.commit(turn, draft)

    def test_reply_prepared_before_new_messages_is_saved_and_sent(self):
        turn = self.prepared(); draft = self.draft()
        self.ingest('m2'); self.ingest('m3'); self.recent(('m1', 'm2', 'm3'))
        result = self.commit(turn, draft)
        self.assertTrue(result['committed'])
        self.assertEqual(result['commit']['late_incoming_ids'], ['m2', 'm3'])
        sent = self.runner.dispatch.send_due('main', MockTransport(), channel='instagram')
        self.assertEqual([r['job_id'] for r in sent['results']], ['g1:0'])
        self.assertEqual([m['id'] for m in self.snapshot()['messages'] if m['status'] == 'received'],
                         ['m2', 'm3'])

    def test_on_time_commit_reports_no_late_messages(self):
        turn = self.prepared()
        self.assertNotIn('late_incoming_ids', self.commit(turn, self.draft())['commit'])

    def test_other_change_mixed_with_late_messages_is_still_a_conflict(self):
        turn = self.prepared(); draft = self.draft()
        self.ingest('m2')
        self.ingest('o2', direction='outgoing')
        with self.assertRaisesRegex(ControlError, 'revision_conflict'):
            self.commit(turn, draft)
        self.assertEqual(self.snapshot()['jobs'], [])

    def test_input_freshness_accepts_only_incoming_arrivals(self):
        self.prepared()
        state = self.runner.state()
        self.ingest('m2')
        prepared_inputs.fresh(self.runner, state)
        self.ingest('o2', direction='outgoing')
        with self.assertRaisesRegex(ControlError, 'prepare_inputs_changed'):
            prepared_inputs.fresh(self.runner, state)

    def waiter(self, arrivals):
        """Fake clock/sleep for wait_dm; arrivals maps elapsed ms to a message ID to ingest."""
        start = self.clock
        pending = dict(arrivals)
        def sleep(seconds):
            self.clock += int(seconds * 1000)
            for at in sorted(list(pending)):
                if self.clock - start >= at:
                    self.ingest(pending.pop(at), text='typing burst')
        return (lambda: self.clock), sleep

    def test_wait_returns_after_the_burst_pauses(self):
        start = self.clock
        clock, sleep = self.waiter({2000: 'm2', 4000: 'm3'})
        self.ingest('m1')
        result = self.store.wait_dm('main', 300, clock, sleep)
        self.assertEqual(result['reason'], 'new_dm')
        self.assertEqual(result['new_message_ids'], ['m1', 'm2', 'm3'])
        self.assertGreaterEqual(self.clock - start, 4000 + store_module.INCOMING_SETTLE_MS)
        self.assertLess(self.clock - start, store_module.INCOMING_SETTLE_CAP_MS)

    def test_continuous_typing_is_answered_at_the_cap(self):
        start = self.clock
        clock, sleep = self.waiter({t: f'b{t}' for t in range(2000, 60000, 2000)})
        self.ingest('m1')
        self.assertEqual(self.store.wait_dm('main', 300, clock, sleep)['reason'], 'new_dm')
        self.assertLessEqual(self.clock - start, store_module.INCOMING_SETTLE_CAP_MS + 2000)

    def test_old_unanswered_message_does_not_wait(self):
        self.ingest('m1', observed_ms=self.clock - 600000)
        self.clock += 600000
        result = self.store.wait_dm('main', 300, lambda: self.clock, lambda s: None)
        self.assertEqual((result['reason'], result['settle_waited_ms']), ('new_dm', 0))


class GateSettleTests(unittest.TestCase):
    def settle(self, arrivals, cfg):
        clock = [1_000_000]
        start = clock[0]
        def tick(_cfg):
            seen = [t for t in arrivals if t <= clock[0] - start]
            return {'messages': [{'id': f'm{t}', 'status': 'received', 'observed_ms': start + t} for t in seen]}
        def sleep(seconds):
            clock[0] += int(seconds * 1000)
        with patch.object(gate, 'read_tick', tick):
            return gate.settle_incoming(cfg, clock=lambda: clock[0], sleep=sleep)

    def test_claim_waits_for_typing_burst_to_pause(self):
        waited = self.settle([0, 2000, 4000], {'incoming_settle_seconds': 6})
        self.assertGreaterEqual(waited, 10000)
        self.assertLess(waited, 20000)

    def test_claim_settle_is_capped_and_off_by_default(self):
        self.assertLessEqual(self.settle(list(range(0, 60000, 2000)), {'incoming_settle_seconds': 6}), 22000)
        self.assertEqual(self.settle([0, 2000], {}), 0)
        self.assertEqual(self.settle([], {'incoming_settle_seconds': 6}), 0)


if __name__ == '__main__':
    unittest.main()
