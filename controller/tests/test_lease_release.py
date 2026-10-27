"""2026-10-22 incident: a wake hit its claim deadline while its conversation lease was still held,
so the gate stayed silent (conversation_lease) and a new DM waited ~8 minutes. Offline only."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import claim_heartbeat as keeper
import gate
from store import Store
import wake_nudge


class LeaseReleaseTests(unittest.TestCase):
    """Real controller DB and real ctl subprocesses, wall clock (the gate uses it)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=ROOT / 'tests')
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.db = root / 'test.db'
        self.state = root / 'automation-state'
        self.state.mkdir()
        self.store = Store(self.db)
        now = gate.now_ms()
        self.store.seed({}, now)
        self.store.ingest('main', {'read_ok': True, 'messages': [{
            'id': 'slack:m1', 'text': 'hello', 'observed_ms': now - 60_000, 'direction': 'incoming',
            'observation': {'identity': 'platform', 'channel': 'slack'}}]}, now - 60_000)
        gate.write_json(self.state / 'poller_health.json', {'last_run_ms': now})
        self.cfg_path = root / 'automation.json'
        self.cfg = {'state_dir': str(self.state), 'python': sys.executable, 'ctl': str(ROOT / 'ctl.py'),
                    'db': str(self.db), 'session': 'main',
                    'decision_claim': {'auto_heartbeat': False, 'max_run_seconds': 600}}
        self.cfg_path.write_text(json.dumps(self.cfg))
        self.cfg['_config_path'] = str(self.cfg_path)

    def traces(self):
        return [json.loads(line) for path in self.state.glob('decision-trace-*.jsonl')
                for line in path.read_text().splitlines()]

    def lease(self):
        snap = self.store.tick('main', gate.now_ms(), view='gate')
        return snap['conversation_lease_until_ms'], snap['conversation_lease_owner']

    def claim(self, deadline_in_ms=600_000):
        result, wake = gate.claim_work(self.cfg)
        self.assertEqual(result, gate.WAKE)
        now = gate.now_ms()
        wake.update(woken_ms=now - 1, claim_deadline_ms=now + deadline_in_ms)
        gate.write_json(self.state / 'wake.json', wake)
        state = gate.load_json(self.state / 'gate_state.json', {})
        state['claim_deadline_ms'] = wake['claim_deadline_ms']
        gate.write_json(self.state / 'gate_state.json', state)
        return wake['wake_id']

    def hold_lease(self, owner, ms=180_000):
        """What wait_dm does after new_dm: a 3-minute composing grace owned by the wake."""
        now = gate.now_ms()
        self.store._set_lease('main', now + ms, now, owner)

    def test_wait_dm_records_owner_and_release_only_matches_it(self):
        clock = [gate.now_ms()]
        def sleep(seconds):
            clock[0] += int(seconds * 1000)
            if clock[0] > gate.now_ms() + 3000:
                self.store.ingest('main', {'read_ok': True, 'messages': [{
                    'id': 'slack:m2', 'text': 'again', 'observed_ms': clock[0] - 10_000, 'direction': 'incoming',
                    'observation': {'identity': 'platform', 'channel': 'slack'}}]}, clock[0] - 10_000)
        self.store.end_conversation('main', clock[0])
        self.store._set_lease('main', clock[0] - 1, clock[0])
        result = self.store.wait_dm('main', 60, lambda: clock[0], sleep, lease_owner='wake-a')
        self.assertEqual(result['reason'], 'new_dm')
        self.assertEqual(self.store.tick('main', clock[0], view='gate')['conversation_lease_owner'], 'wake-a')
        other = self.store.release_lease('main', 'wake-b', clock[0])
        self.assertEqual((other['released'], other['reason']), (False, 'lease_not_owned'))
        own = self.store.release_lease('main', 'wake-a', clock[0])
        self.assertTrue(own['released'] and own['was_live'])
        self.assertGreaterEqual(own['received_count'], 1)
        self.assertEqual(self.store.release_lease('main', 'wake-a', clock[0])['reason'], 'no_lease')

    def test_incident_claim_deadline_releases_lease_nudges_and_next_claim_succeeds(self):
        wake = self.claim(deadline_in_ms=2000)
        self.hold_lease(wake)
        # Before the fix the gate was silent here until the lease expired.
        self.assertEqual(self.lease()[1], wake)
        sent = []
        with patch.object(wake_nudge, 'send', side_effect=lambda cfg, w, r: sent.append((w, r)) or 'sent'):
            elapsed = [0.0]
            reason = keeper.run(self.cfg, wake, lambda: False,
                                clock=lambda: gate.now_ms() + 5000 + int(elapsed[0] * 1000),
                                monotonic=lambda: elapsed[0],
                                sleep=lambda s: elapsed.__setitem__(0, elapsed[0] + s))
        self.assertEqual(reason, 'claim_deadline')
        self.assertEqual(self.lease(), (None, None))
        self.assertEqual(sent, [(wake, 'pending_dm_after_claim_deadline')])
        released = [t for t in self.traces() if t['phase'] == 'lease_released']
        self.assertEqual([(t['wake_id'], t['reason'], t['was_live']) for t in released],
                         [(wake, 'claim_deadline', True)])
        result, successor = gate.claim_work(self.cfg)
        self.assertEqual(result, gate.WAKE)
        self.assertNotEqual(successor['wake_id'], wake)
        self.assertIn('new_dm', successor['reasons'])

    def test_gate_releases_orphaned_lease_when_no_keeper_ran(self):
        wake = self.claim()
        self.hold_lease(wake)
        gate.write_json(self.state / 'wake_ack.json', {'wake_id': wake, 'phase': 'failed', 'at_ms': gate.now_ms(),
                                                      'source': 'daemon', 'reason': 'claim_deadline'})
        with patch('sys.stdout') as output:
            self.assertEqual(gate.main(['--config', str(self.cfg_path), '--check']), gate.WAKE)
        report = json.loads(output.write.call_args_list[0].args[0])
        self.assertTrue(report['wake_eligible'])
        self.assertEqual(self.lease()[1], wake)   # --check stays read-only
        result, successor = gate.claim_work(self.cfg)
        self.assertEqual(result, gate.WAKE)
        self.assertNotEqual(successor['wake_id'], wake)
        self.assertEqual(self.lease(), (None, None))
        self.assertIn('orphaned_lease', [t['reason'] for t in self.traces() if t['phase'] == 'lease_released'])

    def test_live_session_lease_is_respected(self):
        wake = self.claim()
        self.hold_lease(wake)
        result, _ = gate.claim_work(self.cfg)
        self.assertEqual(result, gate.SILENT)
        self.assertEqual(gate.load_json(self.state / 'gate_check.json', {})['reason'], 'conversation_lease')
        self.assertEqual(self.lease()[1], wake)
        # A previous, ended wake cannot release the live owner's lease, nor can its keeper.
        self.assertFalse(gate.release_wake_lease(self.cfg, self.state, 'old-wake', 'test')['released'])
        self.assertIsNone(keeper.wake_ended(self.cfg, 'old-wake', 'ownership_lost',
                                            nudge=lambda *a: self.fail('must not nudge')))
        self.assertEqual(self.lease()[1], wake)
        # The keeper being stopped (SIGTERM) is not the wake ending.
        self.assertIsNone(keeper.wake_ended(self.cfg, wake, 'daemon_stopped'))
        self.assertEqual(self.lease()[1], wake)

    def test_owner_less_legacy_lease_is_never_released(self):
        wake = self.claim()
        now = gate.now_ms()
        self.store._set_lease('main', now + 60_000, now)
        gate.write_json(self.state / 'wake_ack.json', {'wake_id': wake, 'phase': 'failed', 'at_ms': now})
        result, _ = gate.claim_work(self.cfg)
        self.assertEqual(result, gate.SILENT)
        self.assertIsNotNone(self.lease()[0])

    def test_ack_failed_releases_own_lease(self):
        wake = self.claim()
        self.hold_lease(wake)
        self.assertEqual(gate.main(['--config', str(self.cfg_path), '--ack-phase', 'failed',
                                    '--wake-id', wake]), gate.WAKE)
        self.assertEqual(self.lease(), (None, None))

    def test_migration_adds_owner_to_existing_lease_table(self):
        import sqlite3
        from contextlib import closing
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute('DROP TABLE conversation_leases')
            db.execute('''CREATE TABLE conversation_leases (session_id TEXT PRIMARY KEY REFERENCES sessions(id),
                until_ms INTEGER NOT NULL, started_ms INTEGER NOT NULL, waits INTEGER NOT NULL DEFAULT 0)''')
            db.execute("INSERT INTO conversation_leases VALUES ('main', ?, 0, 1)", (gate.now_ms() + 60_000,))
        self.assertEqual(self.lease()[1], None)
        self.assertIsNotNone(self.lease()[0])
        with closing(sqlite3.connect(self.db)) as db:
            self.assertIn('owner', [r[1] for r in db.execute('PRAGMA table_info(conversation_leases)')])


class WakeNudgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        (root / 'url').write_text('http://127.0.0.1:9/never\n')
        (root / 'key').write_text('secret-key\n')
        self.cfg = {'state_dir': str(root / 'state'), 'session': 'main',
                    'nudge': {'enabled': True, 'url_path': str(root / 'url'), 'key_path': str(root / 'key'),
                              'key_header': 'Authorization', 'min_interval_seconds': 20}}
        self.posts = []

    def post(self, nudge, body):
        self.posts.append(json.loads(body))

    def traces(self):
        return [json.loads(line) for path in Path(self.cfg['state_dir']).glob('decision-trace-*.jsonl')
                for line in path.read_text().splitlines()]

    def test_debounce_once_per_wake_and_minimum_interval(self):
        t = 1_000_000
        self.assertEqual(wake_nudge.send(self.cfg, 'w1', 'r', now=t, post=self.post), 'sent')
        self.assertEqual(wake_nudge.send(self.cfg, 'w1', 'r', now=t + 120_000, post=self.post), 'duplicate_wake')
        self.assertEqual(wake_nudge.send(self.cfg, 'w2', 'r', now=t + 30_000, post=self.post), 'debounced')
        self.assertEqual(wake_nudge.send(self.cfg, 'w2', 'r', now=t + 60_000, post=self.post), 'sent')
        self.assertEqual([p['reason'] for p in self.posts], ['r', 'r'])
        self.assertEqual(set(self.posts[0]), {'session', 'reason', 'ts'})

    def test_failures_are_traced_without_secrets_and_disabled_is_a_noop(self):
        self.assertEqual(wake_nudge.send(self.cfg, 'w1', 'r', now=1), 'failed')   # real POST to a closed port
        text = json.dumps(self.traces())
        self.assertIn('wake_nudge_failed', text)
        self.assertNotIn('secret-key', text)
        self.assertNotIn('127.0.0.1', text)
        self.cfg['nudge']['enabled'] = False
        self.assertEqual(wake_nudge.send(self.cfg, 'w2', 'r', now=10**9, post=self.post), 'disabled')
        self.assertEqual(self.posts, [])

    def test_real_request_shape_matches_listener(self):
        import http.server
        import threading
        seen = []
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                seen.append((self.headers.get('Authorization'),
                             json.loads(self.rfile.read(int(self.headers['Content-Length'])))))
                self.send_response(204); self.end_headers()
            def log_message(self, *args): pass
        server = http.server.HTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        Path(self.cfg['nudge']['url_path']).write_text(f'http://127.0.0.1:{server.server_port}/nudge')
        self.assertEqual(wake_nudge.send(self.cfg, 'w1', 'pending_dm_after_claim_deadline', now=5), 'sent')
        self.assertEqual(seen[0][0], 'Bearer secret-key')
        self.assertEqual(seen[0][1]['session'], 'main')


if __name__ == '__main__':
    unittest.main()
