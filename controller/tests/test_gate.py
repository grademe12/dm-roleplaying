import hashlib
import importlib.util
import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
GATE_PATH = ROOT / 'gate.py'
spec = importlib.util.spec_from_file_location('controller_gate', GATE_PATH)
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


class GateTests(unittest.TestCase):
    def test_claimed_apply_forwards_turn_correlation(self):
        with tempfile.TemporaryDirectory() as folder:
            cfg,state=self.fake_cron(Path(folder))
            self.assertEqual(subprocess.run(self.gate_cmd(cfg,'--claim'),capture_output=True).returncode,0)
            wake=json.loads((state/'wake.json').read_text())['wake_id']
            result=subprocess.run(self.gate_cmd(cfg,'--apply-claimed','{}','--wake-id',wake,
                                               '--turn-id','turn-1'),capture_output=True)
            self.assertEqual(result.returncode,0)
            records=[json.loads(s) for p in state.glob('decision-trace-*.jsonl') for s in p.read_text().splitlines()]
            applies=[r for r in records if r['phase'].startswith('apply_')]
            self.assertEqual([r['turn_id'] for r in applies],['turn-1','turn-1'])

    def test_empty_cron_exits_without_wake(self):
        with tempfile.TemporaryDirectory() as folder:
            cfg, state = self.fake_cron(Path(folder))
            current = int(time.time() * 1000)
            tick = {'revision': 1, 'conversation_lease_until_ms': None, 'messages': [],
                    'needs_decision': [], 'state': {'next_decision_ms': current + 600_000},
                    'activity': {'id': 'free', 'attention': 'normal',
                                 'planned_end_ms': current + 600_000}, 'media': [], 'jobs': []}
            with patch.object(gate, 'read_tick', return_value=tick):
                self.assertEqual(gate.main(['--config', str(cfg), '--claim']), gate.SILENT)
            self.assertFalse((state / 'wake.json').exists())
            self.assertEqual(gate.load_json(state / 'gate_check.json', {})['reason'], 'no_work')

    def test_read_only_check_does_not_claim_pending_dm(self):
        with tempfile.TemporaryDirectory() as folder:
            cfg, state = self.fake_cron(Path(folder))
            result = subprocess.run(self.gate_cmd(cfg, '--check'), capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            self.assertTrue(report['has_work'])
            self.assertTrue(report['wake_eligible'])
            self.assertIn('new_dm', report['reasons'])
            self.assertEqual(report['received_count'], 1)
            for name in ('wake.json', 'wake_ack.json', 'gate_state.json', 'gate_check.json', 'gate.lock'):
                self.assertFalse((state / name).exists(), name)

    def test_read_only_check_suppresses_active_worker_but_preserves_work(self):
        with tempfile.TemporaryDirectory() as folder:
            cfg, state = self.fake_cron(Path(folder))
            self.assertEqual(gate.main(['--config',str(cfg),'--claim']),gate.WAKE)
            before = {p.name:p.read_bytes() for p in state.glob('*.json')}
            with patch('sys.stdout') as output:
                gate.main(['--config',str(cfg),'--check'])
            report = json.loads(output.write.call_args_list[0].args[0])
            self.assertTrue(report['has_work'])
            self.assertFalse(report['wake_eligible'])
            self.assertEqual(before,{p.name:p.read_bytes() for p in state.glob('*.json')})

    def test_live_conversation_lease_explains_silent_gate_with_received_dm(self):
        with tempfile.TemporaryDirectory() as folder:
            cfg, state = self.fake_cron(Path(folder))
            tick = gate.read_tick(json.loads(cfg.read_text()))
            tick['conversation_lease_until_ms'] = int(time.time() * 1000) + 30000
            with patch.object(gate, 'read_tick', return_value=tick):
                self.assertEqual(gate.main(['--config', str(cfg), '--claim']), gate.SILENT)
            check = gate.load_json(state / 'gate_check.json', {})
            self.assertEqual((check['reason'], check['received_count']), ('conversation_lease', 1))

    def fake_cron(self, root):
        state = root / 'state'
        state.mkdir()
        clock = int(time.time() * 1000)
        tick = {'ok': True, 'revision': 1, 'conversation_lease_until_ms': None,
                'messages': [{'id': 'slack:m1', 'status': 'received', 'observed_ms': clock,
                              'observation': {'channel': 'slack'}}],
                'needs_decision': [], 'state': {'next_decision_ms': clock + 600_000},
                'activity': {'id': 'free', 'attention': 'normal', 'planned_end_ms': clock + 600_000},
                'media': [], 'jobs': []}
        ctl = root / 'fake_ctl.py'
        ctl.write_text('import json, sys\n'
                       'from pathlib import Path\n'
                       f'tick = {tick!r}\n'
                       'if "tick" in sys.argv: print(json.dumps(tick))\n'
                       'elif "release-lease" in sys.argv:\n'
                       '    print(json.dumps({"ok": True, "reason": "no_lease", "released": False,'
                       ' "was_live": False, "received_count": 0}))\n'
                       'elif "apply" in sys.argv:\n'
                       f'    Path({str(root / "applies")!r}).open("a").write("applied\\n")\n'
                       '    print(json.dumps({"ok": True}))\n')
        cfg = root / 'automation.json'
        cfg.write_text(json.dumps({'state_dir': str(state), 'python': sys.executable,
                                   'ctl': str(ctl), 'db': str(root / 'unused.db'), 'session': 'main'}))
        (state / 'poller_health.json').write_text(json.dumps({'last_run_ms': clock}))
        return cfg, state

    def gate_cmd(self, cfg, *args):
        return [sys.executable, str(GATE_PATH), '--config', str(cfg), *args]

    def test_concurrent_cron_claims_have_one_winner(self):
        with tempfile.TemporaryDirectory() as folder:
            cfg, state = self.fake_cron(Path(folder))
            p1 = subprocess.Popen(self.gate_cmd(cfg, '--claim'), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            p2 = subprocess.Popen(self.gate_cmd(cfg, '--claim'), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            p1.communicate(timeout=10)
            p2.communicate(timeout=10)
            self.assertEqual(sorted([p1.returncode, p2.returncode]), [gate.WAKE, gate.SILENT])
            wake = json.loads((state / 'wake.json').read_text())
            ack = json.loads((state / 'wake_ack.json').read_text())
            self.assertEqual((ack['wake_id'], ack['phase']), (wake['wake_id'], 'started'))

    def test_superseded_cron_cannot_apply(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            cfg, state = self.fake_cron(root)
            self.assertEqual(subprocess.run(self.gate_cmd(cfg, '--claim'), capture_output=True).returncode, 0)
            old_id = json.loads((state / 'wake.json').read_text())['wake_id']
            gate.write_json(state / 'wake.json', {'wake_id': 'new-id'})
            decision = root / 'decision.json'
            decision.write_text('{}')
            result = subprocess.run(self.gate_cmd(cfg, '--apply-claimed', '@' + str(decision), '--wake-id', old_id),
                                    capture_output=True)
            self.assertEqual(result.returncode, gate.SILENT)
            self.assertFalse((root / 'applies').exists())

    def test_current_cron_applies_and_finishes_once(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            cfg, state = self.fake_cron(root)
            self.assertEqual(subprocess.run(self.gate_cmd(cfg, '--claim'), capture_output=True).returncode, 0)
            wake_id = json.loads((state / 'wake.json').read_text())['wake_id']
            decision = root / 'decision.json'
            decision.write_text('{}')
            self.assertEqual(subprocess.run(self.gate_cmd(cfg, '--apply-claimed', '@' + str(decision),
                                                           '--wake-id', wake_id), capture_output=True).returncode, 0)
            self.assertEqual((root / 'applies').read_text(), 'applied\n')
            self.assertEqual(subprocess.run(self.gate_cmd(cfg, '--ack-phase', 'finished',
                                                           '--wake-id', wake_id), capture_output=True).returncode, 0)
            self.assertEqual(subprocess.run(self.gate_cmd(cfg, '--ack-phase', 'started',
                                                           '--wake-id', wake_id), capture_output=True).returncode, gate.SILENT)
            self.assertEqual(json.loads((state / 'wake_ack.json').read_text())['phase'], 'finished')
            trace_path = next(state.glob('decision-trace-*.jsonl'))
            trace = [json.loads(line) for line in trace_path.read_text().splitlines()]
            self.assertEqual([entry['phase'] for entry in trace],
                             ['claimed', 'apply_started', 'apply_finished', 'ack_finished', 'claim_rejected'])
            self.assertTrue(all(entry['wake_id'] == wake_id for entry in trace))
            self.assertEqual(trace[2]['exit_code'], 0)
            self.assertGreaterEqual(trace[2]['elapsed_ms'], 0)

    def test_recovery_retry_after_fast_attempts_exhausted(self):
        with tempfile.TemporaryDirectory() as folder:
            cfg, state = self.fake_cron(Path(folder))
            self.assertEqual(subprocess.run(self.gate_cmd(cfg, '--claim'), capture_output=True).returncode, 0)
            first = json.loads((state / 'wake.json').read_text())
            gate_state = json.loads((state / 'gate_state.json').read_text())
            gate_state['attempts'] = len(gate.DM_RETRY_DELAYS_MS)
            gate_state['last_wake_ms'] -= gate.DM_RECOVERY_RETRY_MS + 1
            gate.write_json(state / 'gate_state.json', gate_state)
            gate.write_json(state / 'wake_ack.json', {'wake_id': first['wake_id'], 'phase': 'started',
                                                      'at_ms': 0})
            self.assertEqual(subprocess.run(self.gate_cmd(cfg, '--claim'), capture_output=True).returncode, 0)
            recovered = json.loads((state / 'wake.json').read_text())
            self.assertNotEqual(first['wake_id'], recovered['wake_id'])
            self.assertEqual(recovered['attempt'], len(gate.DM_RETRY_DELAYS_MS))
            self.assertTrue(recovered['recovery_retry'])
            self.assertFalse(recovered['last_attempt'])
            self.assertEqual(subprocess.run(self.gate_cmd(cfg, '--claim'), capture_output=True).returncode,
                             gate.SILENT)
            self.assertNotEqual(gate.load_json(state / 'gate_health.json', {}).get('alert'), 'retry_exhausted')

    def test_sleep_interrupt_is_stable_and_escalates_once_per_cluster(self):
        now = 1_000_000
        activity = {'id': 'sleep-1', 'category': 'sleep', 'label': '수면', 'attention': 'unavailable',
                    'start_ms': now - 60_000, 'planned_end_ms': now + 3_600_000}
        first_id = next(f'slack:{n}' for n in range(10000)
                        if 0.10 <= int.from_bytes(hashlib.sha256(f'sleep-1:slack:{n}'.encode()).digest()[:8], 'big') / 2**64 < 0.30)
        first = {'id': first_id, 'status': 'received', 'observed_ms': now - 10_000,
                 'observation': {'channel': 'slack'}}
        tick = {'activity': activity, 'messages': [first]}
        self.assertFalse(gate.sleep_interrupt(tick, now)['wake_allowed'])
        self.assertEqual(gate.sleep_interrupt(tick, now), gate.sleep_interrupt(tick, now + 1000))
        tick['messages'].append({'id': 'slack:second', 'status': 'received', 'observed_ms': now,
                                 'observation': {'channel': 'slack'}})
        result = gate.sleep_interrupt(tick, now)
        self.assertTrue(result['wake_allowed'])
        self.assertEqual(result['chance_percent'], 30)
        self.assertEqual(result['message_ids'], [first_id, 'slack:second'])

    def test_new_dm_retry_is_fast_but_started_worker_is_not_duplicated(self):
        now = 1_000_000
        detail = {'fingerprint_basis': {'reasons': ['new_dm'], 'ids': [['m1']]}}
        self.assertEqual(gate.decide(['new_dm'], detail, {}, now), (True, 1))
        state = {'fingerprint': detail['fingerprint'], 'attempts': 1, 'last_wake_ms': now,
                 'wake_id': 'wake-1'}
        self.assertEqual(gate.decide(['new_dm'], detail, state, now + 1), (True, 2))
        self.assertEqual(gate.decide(['new_dm'], detail, state, now + 60_000), (True, 2))
        ack = {'wake_id': 'wake-1', 'phase': 'started', 'at_ms': now + 30_000}
        self.assertEqual(gate.decide(['new_dm'], detail, state, now + 60_000, ack), (False, 1))

    def test_instagram_message_does_not_use_old_slack_sleep_draw(self):
        now = 1_000_000
        tick = {'activity': {'id': 'sleep-1', 'category': 'sleep', 'label': '수면',
                             'attention': 'unavailable', 'start_ms': now-1000,
                             'planned_end_ms': now+100000},
                'messages': [
                    {'id': 'slack:old', 'status': 'deferred', 'observed_ms': now-500,
                     'observation': {'channel': 'slack'}},
                    {'id': 'instagram:new', 'status': 'received', 'observed_ms': now,
                     'observation': {'channel': 'instagram'}}],
                'needs_decision': [], 'state': {'next_decision_ms': now+100000}, 'media': [], 'jobs': []}
        reasons, detail = gate.evaluate(tick, {'last_run_ms': now}, now)
        self.assertIn('new_dm', reasons)
        self.assertNotIn('sleep_interrupt', detail)

    def test_ack_rejects_superseded_wake(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            state = root / 'state'
            state.mkdir()
            (state / 'wake.json').write_text(json.dumps({'wake_id': 'current'}))
            cfg = root / 'automation.json'
            cfg.write_text(json.dumps({'state_dir': str(state)}))
            def ack(wake_id):
                return subprocess.run([sys.executable, str(GATE_PATH), '--config', str(cfg),
                                       '--ack-phase', 'started', '--wake-id', wake_id], capture_output=True).returncode
            self.assertEqual(ack('old'), gate.SILENT)
            self.assertFalse((state / 'wake_ack.json').exists())
            self.assertEqual(ack('current'), gate.WAKE)
            self.assertEqual(json.loads((state / 'wake_ack.json').read_text())['phase'], 'started')


if __name__ == '__main__':
    unittest.main()
