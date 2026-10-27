"""Conversation-only proactive_check_due: due matrix and timer advance on apply."""
import unittest

import test_controller as baseline
import persona
import proactive_check
import gate
import decision_routing


class ProactiveCheckTests(unittest.TestCase):
    tearDown = baseline.ControllerTests.tearDown
    snapshot = baseline.ControllerTests.snapshot
    decision = baseline.ControllerTests.decision
    review = baseline.ControllerTests.review
    activity = baseline.ControllerTests.activity
    ingest = baseline.ControllerTests.ingest

    def setUp(self):
        baseline.ControllerTests.setUp(self)
        self.store.apply('main', self.decision(activities=[
            self.activity('rest', attention='free', end=self.clock + 4 * 3600_000)]), self.clock)

    def check(self):
        return self.snapshot()['proactive_check']

    def set_after(self, after_ms):
        with self.store.connect(True) as db:
            db.execute('UPDATE sessions SET proactive_check_after_ms=? WHERE id=?',
                       (after_ms, 'main'))

    def test_interval_constant(self):
        self.assertEqual(persona.PROACTIVE_CHECK_INTERVAL_MS, 45 * 60_000)
        self.assertEqual(proactive_check.interval_ms(), 45 * 60_000)

    def test_null_without_outbound_is_not_due(self):
        snap = self.snapshot()
        self.assertIsNone(snap['state'].get('proactive_check_after_ms'))
        self.assertEqual(self.check()['status'], 'awaiting_outbound')
        self.assertFalse(self.check()['due'])
        self.assertNotIn('proactive_check_due', snap['needs_decision'])
        self.assertIn('proactive_check_due', gate.WAKE_NEEDS)
        self.assertIn('proactive_check_due', decision_routing.DM_REASONS)

    def test_not_due_with_incoming(self):
        self.set_after(self.clock - 1)
        self.ingest('in1')
        self.assertFalse(self.check()['due'])
        self.assertEqual(self.check()['status'], 'waiting_for_incoming')
        self.assertNotIn('proactive_check_due', self.snapshot()['needs_decision'])

    def test_not_due_with_active_proactive_group(self):
        dm = {'action': 'initiate', 'group_id': 'p1', 'due_ms': self.clock,
              'parts': ['뭐함'], 'reason': 'test', 'channel': 'instagram'}
        self.store.apply('main', self.decision(dm_decisions=[dm]), self.clock)
        self.assertTrue(self.snapshot()['proactive_group_ids'])
        self.assertFalse(self.check()['due'])
        self.assertEqual(self.check()['status'], 'proactive_active')

    def test_not_due_when_unavailable(self):
        self.set_after(self.clock - 1)
        self.store.apply('main', self.decision(activities=[
            {'id': 'rest', 'status': 'completed', 'actual_end_ms': self.clock, 'reason': 'done'},
            self.activity('sleep', category='sleep', label='수면', attention='unavailable',
                          end=self.clock + 8 * 3600_000)]), self.clock)
        self.assertFalse(self.check()['due'])
        self.assertEqual(self.check()['status'], 'waiting_for_availability')
        self.assertNotIn('proactive_check_due', self.snapshot()['needs_decision'])

    def test_not_due_before_interval(self):
        self.set_after(self.clock + 10 * 60_000)
        self.assertFalse(self.check()['due'])
        self.assertEqual(self.check()['status'], 'waiting_for_interval')
        self.assertEqual(self.check()['after_ms'], self.clock + 10 * 60_000)

    def test_due_after_interval(self):
        self.set_after(self.clock - 1)
        self.assertTrue(self.check()['due'])
        self.assertIn('proactive_check_due', self.snapshot()['needs_decision'])
        self.assertEqual(self.snapshot()['work_manifest']['primary'], 'dm')

    def test_apply_without_initiate_advances_timer(self):
        self.set_after(self.clock - 1)
        self.assertTrue(self.check()['due'])
        self.store.apply('main', self.decision(), self.clock)
        after = self.snapshot()['state']['proactive_check_after_ms']
        self.assertEqual(after, self.clock + persona.PROACTIVE_CHECK_INTERVAL_MS)
        self.assertFalse(self.check()['due'])
        self.assertEqual(self.snapshot()['state']['next_decision_ms'], self.clock + 3600000)

    def test_outbound_reply_advances_timer(self):
        self.set_after(self.clock + 60_000)
        self.ingest('m1')
        self.store.apply('main', self.decision(dm_decisions=[{
            'action': 'reply', 'message_ids': ['m1'], 'group_id': 'g1',
            'due_ms': self.clock, 'parts': ['ㅇㅋ'], 'reason': 'r',
            'channel': 'instagram'}], processed_event_ids=['dm:m1']), self.clock)
        after = self.snapshot()['state']['proactive_check_after_ms']
        self.assertEqual(after, self.clock + persona.PROACTIVE_CHECK_INTERVAL_MS)
        self.assertFalse(self.check()['due'])

    def test_null_falls_back_to_last_outbound_plus_interval(self):
        dm = {'action': 'initiate', 'group_id': 'p2', 'due_ms': self.clock,
              'parts': ['ㅎㅇ'], 'reason': 'test', 'channel': 'instagram'}
        self.store.apply('main', self.decision(dm_decisions=[dm]), self.clock)
        with self.store.connect(True) as db:
            db.execute("UPDATE reply_groups SET status='completed' WHERE id='p2'")
            db.execute("UPDATE dm_jobs SET status='confirmed' WHERE group_id='p2'")
            db.execute('UPDATE sessions SET proactive_check_after_ms=NULL WHERE id=?', ('main',))
        expected = self.clock + persona.PROACTIVE_CHECK_INTERVAL_MS
        self.assertEqual(self.check()['after_ms'], expected)
        self.assertFalse(self.check()['due'])
        self.clock = expected
        self.assertTrue(self.check()['due'])


if __name__ == '__main__':
    unittest.main()
