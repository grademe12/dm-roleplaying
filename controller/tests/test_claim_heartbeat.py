"""A bounded state keeper is deliberately not a model-liveness detector."""
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import claim_heartbeat as keeper
import gate
import test_gate as fixtures


class ClaimHeartbeatTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path, self.state = fixtures.GateTests().fake_cron(Path(self.tmp.name))
        self.cfg = json.loads(self.path.read_text())
        self.cfg['_config_path'] = str(self.path)

    def traces(self):
        return [json.loads(line) for path in self.state.glob('decision-trace-*.jsonl')
                for line in path.read_text().splitlines()]

    def seed_bound(self, duration=4000):
        result, wake = gate.claim_work(self.cfg)
        self.assertEqual(result, 0)
        now = gate.now_ms()
        wake.update(woken_ms=now, claim_deadline_ms=now + duration)
        gate.write_json(self.state / 'wake.json', wake)
        state = gate.load_json(self.state / 'gate_state.json', {})
        state['claim_deadline_ms'] = wake['claim_deadline_ms']
        gate.write_json(self.state / 'gate_state.json', state)
        return wake

    def test_terminal_ack_and_lost_ownership_stop_without_writing(self):
        wake = self.seed_bound()
        for phase in ('finished', 'failed'):
            ack = {'wake_id': wake['wake_id'], 'phase': phase, 'at_ms': gate.now_ms()}
            gate.write_json(self.state / 'wake_ack.json', ack)
            self.assertEqual(keeper.pulse(self.cfg, wake['wake_id']), 'ack_' + phase)
            self.assertEqual(gate.load_json(self.state / 'wake_ack.json', {}), ack)
        new_ack = {'wake_id': 'replacement', 'phase': 'started', 'at_ms': gate.now_ms()}
        gate.write_json(self.state / 'wake_ack.json', new_ack)
        self.assertEqual(keeper.pulse(self.cfg, wake['wake_id']), 'ownership_lost')
        self.assertEqual(gate.load_json(self.state / 'wake_ack.json', {}), new_ack)

    def test_deadline_blocks_apply_and_allows_takeover_even_with_a_fresh_ack(self):
        wake = self.seed_bound()
        current = wake['claim_deadline_ms']
        ack = {'wake_id': wake['wake_id'], 'phase': 'started', 'at_ms': current - 1}
        gate.write_json(self.state / 'wake_ack.json', ack)
        self.assertFalse(gate.claim_is_current(self.state, wake['wake_id'], current))
        state = gate.load_json(self.state / 'gate_state.json', {})
        detail = {'fingerprint_basis': {'new': 'input'}}
        self.assertEqual(gate.decide(['new_dm'], detail, state, current, ack), (True, 1))
        with patch.object(gate, 'now_ms', return_value=current):
            self.assertEqual(gate.main(['--config', str(self.path), '--wake-id', wake['wake_id'],
                                       '--apply-claimed', '{}']), 10)
        self.assertEqual(keeper.pulse(self.cfg, wake['wake_id'], now=current), 'claim_deadline')
        self.assertEqual(gate.load_json(self.state / 'wake_ack.json', {})['phase'], 'failed')
        self.assertFalse((Path(self.tmp.name) / 'applies').exists())

    def test_restart_does_not_extend_original_deadline(self):
        wake = self.seed_bound()
        remaining_start = wake['claim_deadline_ms'] - 2000
        elapsed = [0.0]

        def advance(seconds):
            elapsed[0] += seconds

        reason = keeper.run(self.cfg, wake['wake_id'], lambda: False,
                            clock=lambda: remaining_start + int(elapsed[0] * 1000),
                            monotonic=lambda: elapsed[0], sleep=advance)
        self.assertEqual(reason, 'claim_deadline')
        self.assertEqual(elapsed[0], 2)
        self.assertEqual(gate.load_json(self.state / 'wake.json', {})['claim_deadline_ms'],
                         wake['claim_deadline_ms'])
        self.assertFalse(keeper.marker_path(self.state, wake['wake_id']).exists())

    def test_monotonic_bound_still_expires_if_wall_clock_stops(self):
        wake = self.seed_bound(duration=2000)
        elapsed = [0.0]

        def advance(seconds):
            elapsed[0] += seconds

        self.assertEqual(keeper.run(self.cfg, wake['wake_id'], lambda: False,
                         clock=lambda: wake['woken_ms'], monotonic=lambda: elapsed[0], sleep=advance),
                         'claim_deadline')
        self.assertEqual(elapsed[0], 2)

    def test_a_second_daemon_cannot_run_for_the_same_wake(self):
        wake = self.seed_bound()
        marker = keeper.marker_path(self.state, wake['wake_id'])
        with marker.with_suffix('.lock').open('a+b') as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            before = (self.state / 'wake_ack.json').read_bytes()
            self.assertEqual(keeper.run(self.cfg, wake['wake_id'], lambda: False), 'already_running')
            self.assertEqual((self.state / 'wake_ack.json').read_bytes(), before)

    def test_unbounded_legacy_claim_cannot_start_a_keeper(self):
        result, wake = gate.claim_work(self.cfg)
        self.assertEqual(result, 0)
        with self.assertRaisesRegex(Exception, 'unbounded_claim'):
            keeper.run(self.cfg, wake['wake_id'], lambda: False)

    def configure_auto(self, maximum=4):
        self.cfg['decision_claim'] = {'auto_heartbeat': True, 'stale_after_seconds': 30,
                                     'heartbeat_interval_seconds': 1, 'max_run_seconds': maximum}
        self.path.write_text(json.dumps(self.cfg))

    def wait_for(self, condition, timeout=6):
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            if condition():
                return
            time.sleep(0.02)
        self.fail('bounded daemon did not reach expected state')

    def stop_daemons(self):
        for marker in self.state.glob('heartbeat-*.json'):
            value = gate.load_json(marker, {})
            try:
                os.kill(value['pid'], signal.SIGTERM)
            except (ProcessLookupError, KeyError):
                pass
        self.wait_for(lambda: not list(self.state.glob('heartbeat-*.json')))

    def auto_claim(self):
        self.addCleanup(self.stop_daemons)
        result = subprocess.run([sys.executable, str(Path(gate.__file__)), '--config', str(self.path), '--claim'],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        return gate.load_json(self.state / 'wake.json', {})

    def test_automatic_daemon_survives_claim_process_and_stops_on_finish(self):
        self.configure_auto()
        wake = self.auto_claim()
        marker = keeper.marker_path(self.state, wake['wake_id'])
        ready = gate.load_json(marker, {})
        self.assertEqual(os.getsid(ready['pid']), ready['pid'])
        self.wait_for(lambda: gate.load_json(self.state / 'wake_ack.json', {}).get('source') == 'daemon')
        first = gate.load_json(self.state / 'wake_ack.json', {})['at_ms']
        self.wait_for(lambda: gate.load_json(self.state / 'wake_ack.json', {}).get('at_ms', 0) > first)
        tick = gate.read_tick(self.cfg)
        tick['messages'].append({'id': 'new-during-reasoning', 'status': 'received',
                                 'observed_ms': gate.now_ms(), 'observation': {'channel': 'slack'}})
        with patch.object(gate, 'read_tick', return_value=tick):
            self.assertEqual(gate.main(['--config', str(self.path), '--claim']), 10)
        self.assertEqual(gate.load_json(self.state / 'wake.json', {})['wake_id'], wake['wake_id'])
        self.assertEqual(gate.main(['--config', str(self.path), '--wake-id', wake['wake_id'],
                                   '--ack-phase', 'finished']), 0)
        self.wait_for(lambda: not marker.exists())
        self.assertTrue(any(e['phase'] == 'heartbeat_stopped' and e['reason'] == 'ack_finished'
                            for e in self.traces()))

    def test_daemon_caps_abandoned_claim_without_waiting_an_extra_stale_period(self):
        self.configure_auto(maximum=3)
        wake = self.auto_claim()
        self.wait_for(lambda: gate.load_json(self.state / 'wake_ack.json', {}).get('phase') == 'failed')
        self.wait_for(lambda: not keeper.marker_path(self.state, wake['wake_id']).exists())
        self.assertEqual(gate.load_json(self.state / 'wake_ack.json', {})['reason'], 'claim_deadline')
        self.assertEqual(gate.main(['--config', str(self.path), '--wake-id', wake['wake_id'], '--heartbeat']), 10)
        self.assertTrue(any(e['phase'] == 'heartbeat_stopped' and e['reason'] == 'claim_deadline'
                            for e in self.traces()))

    def test_startup_failure_does_not_return_a_successful_claim(self):
        self.configure_auto()
        with patch.object(keeper, 'start', side_effect=OSError('failed')):
            self.assertEqual(gate.main(['--config', str(self.path), '--claim']), 1)
        self.assertEqual(gate.load_json(self.state / 'wake_ack.json', {})['phase'], 'failed')
        self.assertEqual(self.traces()[-1]['reason'], 'heartbeat_start_failed')
