"""Pickup deadline after a live wait: a session that dies at handover loses the
claim in PICKUP_GRACE_MS, while a live session keeps the 15-minute protection.

The bug: finish() waits in wait_dm with the conversation lease held, and the
wait's last refresh leaves a fresh ack. If the model session is already dead
when the wait returns new_dm, nobody calls prepare, yet gate.active_claim()
stays true for the full WORKER_STALE_MS window, so a new DM waits 15 minutes.
The fix leaves a short pickup deadline on the ack at handover: past it the
claim no longer counts as active, even while the ack is fresh (the auto
heartbeat daemon preserves the deadline instead of extending it).
"""
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import ControlError
import claim_heartbeat as keeper
import gate
import test_lease_release as lease_fixtures
import test_decision_session as session_fixtures


class PickupClaimFunctionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name) / 'state'
        self.state.mkdir()
        for name in ('wake', 'gate_state'):
            gate.write_json(self.state / (name + '.json'), {'wake_id': 'w1'})
        gate.write_json(self.state / 'wake_ack.json',
                        {'wake_id': 'w1', 'phase': 'started', 'at_ms': 0})

    def refresh(self, source, now, pickup=None):
        with gate.gate_lock(self.state):
            return gate.refresh_claim(self.state, 'w1', source, now, pickup_deadline_ms=pickup)

    def ack(self):
        return gate.load_json(self.state / 'wake_ack.json', {})

    def test_session_refresh_sets_pickup_and_plain_refresh_clears_it(self):
        self.assertTrue(self.refresh('session', 1000, 1000 + gate.PICKUP_GRACE_MS))
        self.assertEqual(self.ack()['pickup_deadline_ms'], 1000 + gate.PICKUP_GRACE_MS)
        self.assertTrue(self.refresh('session', 2000))
        self.assertNotIn('pickup_deadline_ms', self.ack())

    def test_daemon_refresh_preserves_pickup_deadline(self):
        self.assertTrue(self.refresh('session', 1000, 1000 + gate.PICKUP_GRACE_MS))
        # A daemon refresh must not extend (or clear) a dead session's deadline,
        # even when the caller passes a later value.
        self.assertTrue(self.refresh('daemon', 2000, 999999))
        self.assertEqual((self.ack()['at_ms'], self.ack()['pickup_deadline_ms']),
                         (2000, 1000 + gate.PICKUP_GRACE_MS))
        self.assertTrue(self.refresh('daemon', 3000))
        self.assertEqual(self.ack()['pickup_deadline_ms'], 1000 + gate.PICKUP_GRACE_MS)

    def test_active_claim_ends_at_pickup_deadline_even_with_fresh_ack(self):
        state = {'wake_id': 'w1'}
        ack = {'wake_id': 'w1', 'phase': 'started', 'at_ms': 1000,
               'pickup_deadline_ms': 1000 + gate.PICKUP_GRACE_MS}
        self.assertTrue(gate.active_claim(state, ack, 1000 + gate.PICKUP_GRACE_MS - 1,
                                          gate.WORKER_STALE_MS))
        self.assertFalse(gate.active_claim(state, ack, 1000 + gate.PICKUP_GRACE_MS,
                                           gate.WORKER_STALE_MS))
        plain = {k: v for k, v in ack.items() if k != 'pickup_deadline_ms'}
        self.assertTrue(gate.active_claim(state, plain, 1000 + gate.PICKUP_GRACE_MS,
                                          gate.WORKER_STALE_MS))

    def test_decide_allows_replacement_after_pickup_despite_fresh_ack(self):
        state = {'fingerprint': 'old', 'wake_id': 'w1', 'attempts': 1, 'last_wake_ms': 0}
        detail = {'fingerprint_basis': {'messages': ['m2']}}
        ack = {'wake_id': 'w1', 'phase': 'started', 'at_ms': 1000,
               'pickup_deadline_ms': 1000 + gate.PICKUP_GRACE_MS}
        self.assertEqual(gate.decide(['new_dm'], detail, state,
                                     1000 + gate.PICKUP_GRACE_MS - 1, ack), (False, 1))
        self.assertEqual(gate.decide(['new_dm'], detail, state,
                                     1000 + gate.PICKUP_GRACE_MS, ack), (True, 1))

    def test_keeper_pulse_preserves_pickup_and_claim_expires_on_time(self):
        gate.write_json(self.state / 'wake.json',
                        {'wake_id': 'w1', 'woken_ms': 900_000, 'claim_deadline_ms': 1_500_000})
        gate.write_json(self.state / 'wake_ack.json',
                        {'wake_id': 'w1', 'phase': 'started', 'at_ms': 900_000,
                         'pickup_deadline_ms': 950_000})
        self.assertIsNone(keeper.pulse({'state_dir': str(self.state)}, 'w1', now=940_000))
        ack = self.ack()
        self.assertEqual((ack['at_ms'], ack['pickup_deadline_ms']), (940_000, 950_000))
        state = gate.load_json(self.state / 'gate_state.json', {})
        self.assertTrue(gate.active_claim(state, ack, 949_999, gate.WORKER_STALE_MS))
        self.assertFalse(gate.active_claim(state, ack, 950_000, gate.WORKER_STALE_MS))


class PickupHandoverGateTests(unittest.TestCase):
    """Real DB + real gate subprocess controller, wall clock (as the gate uses it)."""

    def setUp(self):
        self.case = lease_fixtures.LeaseReleaseTests()
        self.case.setUp()
        self.addCleanup(self.case.tmp.cleanup)

    def post_wait(self, wake, pickup_deadline_ms):
        """The state finish() leaves behind: a composing lease owned by the wake
        and a fresh ack carrying the pickup deadline, plus the DM that ended the wait."""
        c = self.case
        c.hold_lease(wake)
        now = gate.now_ms()
        c.store.ingest('main', {'read_ok': True, 'messages': [{
            'id': 'slack:m2', 'text': 'again', 'observed_ms': now, 'direction': 'incoming',
            'observation': {'identity': 'platform', 'channel': 'slack'}}]}, now)
        gate.write_json(c.state / 'wake_ack.json', {'wake_id': wake, 'phase': 'started',
            'at_ms': now, 'pickup_deadline_ms': pickup_deadline_ms})

    def test_before_pickup_deadline_gate_stays_silent(self):
        c = self.case
        wake = c.claim()
        self.post_wait(wake, gate.now_ms() + 120_000)
        result, _ = gate.claim_work(c.cfg)
        self.assertEqual(result, gate.SILENT)
        self.assertEqual(c.lease()[1], wake)
        self.assertEqual(gate.load_json(c.state / 'gate_state.json', {})['wake_id'], wake)

    def test_after_pickup_deadline_new_claim_releases_lease_as_orphan(self):
        c = self.case
        wake = c.claim()
        self.post_wait(wake, gate.now_ms() - 1)
        result, successor = gate.claim_work(c.cfg)
        self.assertEqual(result, gate.WAKE)
        self.assertNotEqual(successor['wake_id'], wake)
        self.assertEqual(c.lease(), (None, None))
        released = [t for t in c.traces() if t['phase'] == 'lease_released']
        self.assertIn((wake, 'orphaned_lease', True),
                      [(t['wake_id'], t['reason'], t['was_live']) for t in released])
        replaced = next(t for t in c.traces() if t['phase'] == 'claim_replaced')
        self.assertEqual((replaced['previous_wake_id'], replaced['reason']),
                         (wake, 'pickup_expired'))


class PickupSessionTests(unittest.TestCase):
    def setUp(self):
        self.case = session_fixtures.DecisionSessionTests()
        self.case.setUp()
        self.addCleanup(self.case.tearDown)

    def test_wait_guard_leaves_rolling_pickup_deadline(self):
        c = self.case
        start = c.clock
        pending = {4000: 'm2'}

        def sleep(seconds):
            c.clock += int(seconds * 1000)
            for at in sorted(list(pending)):
                if c.clock - start >= at:
                    c.ingest(pending.pop(at), text='typing burst')

        result = c.store.wait_dm('main', 30, lambda: c.clock, sleep,
                                 owner_guard=c.runner.wait_guard, lease_owner='wake-1')
        self.assertEqual(result['reason'], 'new_dm')
        # finish() hands the wait back with the same pickup refresh:
        with c.runner.owned(pickup=True):
            pass
        ack = gate.load_json(c.state_dir / 'wake_ack.json', {})
        self.assertEqual(ack['pickup_deadline_ms'], c.clock + gate.PICKUP_GRACE_MS)
        self.assertEqual(c.store.tick('main', c.clock, view='gate')['conversation_lease_owner'],
                         'wake-1')

    def test_prepare_within_grace_clears_pickup_and_keeps_stale_protection(self):
        c = self.case
        with c.runner.owned(pickup=True):
            pass
        before = gate.load_json(c.state_dir / 'wake_ack.json', {})
        self.assertEqual(before['pickup_deadline_ms'], c.clock + gate.PICKUP_GRACE_MS)
        c.prepared()
        after = gate.load_json(c.state_dir / 'wake_ack.json', {})
        self.assertNotIn('pickup_deadline_ms', after)
        state = gate.load_json(c.state_dir / 'gate_state.json', {})
        past_pickup = before['pickup_deadline_ms'] + 1
        self.assertTrue(gate.active_claim(state, after, past_pickup, gate.WORKER_STALE_MS))
        self.assertFalse(gate.active_claim(state, after, after['at_ms'] + gate.WORKER_STALE_MS,
                                           gate.WORKER_STALE_MS))

    def test_late_prepare_after_replacement_gets_stale_claim(self):
        c = self.case
        with c.runner.owned(pickup=True):
            pass
        c.clock += gate.PICKUP_GRACE_MS + 1
        for name in ('wake', 'gate_state'):
            gate.write_json(c.state_dir / (name + '.json'), {'wake_id': 'replacement'})
        gate.write_json(c.state_dir / 'wake_ack.json',
                        {'wake_id': 'replacement', 'phase': 'started', 'at_ms': c.clock})
        c.ingest(); c.recent()
        with self.assertRaisesRegex(ControlError, 'stale_claim'):
            c.runner.prepare(mode='bundled')


if __name__ == '__main__':
    unittest.main()
