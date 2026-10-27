"""Claim replacement, diagnostics, and recovery copies without external services."""
import json
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from common import ControlError
from decision_session import DecisionSession
import gate
import test_gate as gate_fixtures
import test_decision_session as session_fixtures
import decision_drafts


class ClaimPolicyTests(unittest.TestCase):
    def traces(self, folder):
        return [json.loads(line) for path in folder.glob('decision-trace-*.jsonl')
                for line in path.read_text().splitlines()]

    def test_default_preserves_an_active_worker_after_206_seconds_with_new_input(self):
        state = {'fingerprint': 'old', 'wake_id': 'worker', 'attempts': 1}
        ack = {'wake_id': 'worker', 'phase': 'started', 'at_ms': 1000}
        detail = {'fingerprint_basis': {'messages': ['new-message']}}
        self.assertEqual(gate.decide(['new_dm'], detail, state, 207000, ack), (False, 1))
        self.assertEqual(gate.decide(['new_dm'], detail, state, 207000, ack, 180000), (True, 1))

    def test_recorded_life_run_needs_both_ack_expiry_and_retry_delay(self):
        reasons = ['decision_due', 'activity_due']
        detail = {'fingerprint_basis': {'reasons': reasons}}
        claimed = 1792505426365
        gate.decide(reasons, detail, {}, claimed)
        state = {'wake_id': 'recorded-worker', 'fingerprint': detail['fingerprint'],
                 'attempts': 1, 'last_wake_ms': claimed}
        ack = {'wake_id': state['wake_id'], 'phase': 'started', 'at_ms': 1792505590778}
        # The backstop sees an old ACK, but the ten-minute retry delay still holds.
        self.assertEqual(gate.decide(reasons, detail, state, 1792505890275, ack, 180000), (False, 1))
        ack['at_ms'] = 1792505967440  # Latest input read renews ownership before its trace.
        before_expiry = ack['at_ms'] + 180000 - 1
        self.assertEqual(gate.decide(reasons, detail, state, before_expiry, ack, 180000), (False, 1))
        self.assertEqual(gate.decide(reasons, detail, state, before_expiry + 1, ack, 180000), (True, 2))
        replacement = 1792506191414
        self.assertEqual(gate.decide(reasons, detail, state, replacement, ack, 180000), (True, 2))
        self.assertEqual(gate.decide(reasons, detail, state, replacement, ack), (False, 1))

    def test_invalid_timing_and_types_are_rejected(self):
        for policy in ({'stale_after_seconds': True}, {'stale_after_seconds': 0},
                       {'heartbeat_interval_seconds': 0}, {'stale_after_seconds': 30},
                       {'max_run_seconds': 30}, {'max_run_seconds': 1801},
                       {'auto_heartbeat': 'yes'}, [], None):
            with self.subTest(policy=policy), self.assertRaises(ControlError):
                gate.claim_policy({'decision_claim': policy})

    def test_expiry_replacement_and_late_heartbeat_have_diagnostic_evidence(self):
        with tempfile.TemporaryDirectory() as folder:
            path, state_dir = gate_fixtures.GateTests().fake_cron(Path(folder))
            cfg = json.loads(path.read_text())
            cfg['decision_claim'] = {'stale_after_seconds': 30, 'heartbeat_interval_seconds': 5}
            path.write_text(json.dumps(cfg))
            self.assertEqual(gate.main(['--config', str(path), '--claim']), 0)
            old = gate.load_json(state_dir / 'wake.json', {})['wake_id']
            ack_at = gate.now_ms() - 31000
            gate.write_json(state_dir / 'wake_ack.json', {'wake_id': old, 'phase': 'started', 'at_ms': ack_at})
            self.assertEqual(gate.main(['--config', str(path), '--claim']), 0)
            current = gate.load_json(state_dir / 'wake_ack.json', {})
            self.assertNotEqual(current['wake_id'], old)
            self.assertEqual(gate.main(['--config', str(path), '--heartbeat', '--wake-id', old]), 10)
            self.assertEqual(gate.load_json(state_dir / 'wake_ack.json', {}), current)
            replacement = next(e for e in self.traces(state_dir) if e['phase'] == 'claim_replaced')
            self.assertEqual(replacement['previous_wake_id'], old)
            self.assertEqual(replacement['reason'], 'worker_stalled')
            self.assertEqual(replacement['ack_at_ms'], ack_at)
            self.assertGreaterEqual(replacement['heartbeat_age_ms'], 31000)
            self.assertEqual(replacement['stale_after_ms'], 30000)
            rejection = self.traces(state_dir)[-1]
            self.assertEqual(rejection['phase'], 'claim_rejected')
            self.assertEqual(rejection['current_wake_id'], current['wake_id'])

    def test_empty_work_does_not_erase_a_live_claim(self):
        with tempfile.TemporaryDirectory() as folder:
            path, state = gate_fixtures.GateTests().fake_cron(Path(folder))
            cfg = json.loads(path.read_text())
            status, wake = gate.claim_work(cfg)
            self.assertEqual(status, 0)
            tick = gate.read_tick(cfg)
            tick.update(messages=[], needs_decision=[])
            before = gate.load_json(state / 'gate_state.json', {})
            with patch.object(gate, 'read_tick', return_value=tick):
                self.assertEqual(gate.main(['--config', str(path), '--claim']), 10)
            self.assertEqual(gate.load_json(state / 'gate_state.json', {}), before)
            self.assertTrue(gate.claim_is_current(state, wake['wake_id']))

    def test_legacy_apply_rejection_is_traced_before_subprocess(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            cfg, state = gate_fixtures.GateTests().fake_cron(root)
            gate.main(['--config', str(cfg), '--claim'])
            current = gate.load_json(state / 'wake.json', {})['wake_id']
            result = gate.main(['--config', str(cfg), '--wake-id', 'old', '--apply-claimed', '{}'])
            self.assertEqual(result, 10)
            self.assertFalse((root / 'applies').exists())
            event = self.traces(state)[-1]
            self.assertEqual((event['operation'], event['reason'], event['current_wake_id']),
                             ('apply', 'stale_claim', current))

    def test_legacy_exit_one_records_reason_without_error_details(self):
        with tempfile.TemporaryDirectory() as folder:
            path, state = gate_fixtures.GateTests().fake_cron(Path(folder))
            cfg = json.loads(path.read_text())
            ctl = Path(cfg['ctl'])
            ctl.write_text(ctl.read_text().replace('    print(json.dumps({"ok": True}))',
                '    print(json.dumps({"ok": False, "reason": "activity_unavailable", '
                '"detail": "private_error_detail"}))\n    sys.exit(1)'))
            self.assertEqual(gate.main(['--config', str(path), '--claim']), 0)
            wake = gate.load_json(state / 'wake.json', {})['wake_id']
            with patch('sys.stdout'):
                self.assertEqual(gate.main(['--config', str(path), '--wake-id', wake,
                                           '--apply-claimed', '{}']), 1)
            event = self.traces(state)[-1]
            self.assertEqual((event['phase'], event['reason'], event['exit_code']),
                             ('apply_finished', 'activity_unavailable', 1))
            self.assertNotIn('private_error_detail', json.dumps(self.traces(state)))


class DraftRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.case = session_fixtures.DecisionSessionTests()
        self.case.setUp()
        self.addCleanup(self.case.tearDown)

    def test_conflict_saves_private_draft_and_prepare_offers_it_without_applying(self):
        c = self.case
        turn = c.prepared()
        draft = c.draft('private_recovery_body')
        c.ingest('o2', direction='outgoing'); c.recent(('m1',), outgoing=('o2',))
        revision = c.snapshot()['revision']
        with self.assertRaisesRegex(ControlError, 'revision_conflict'):
            c.runner.commit(turn, draft)
        saved = c.runner.last_recovery
        self.assertTrue(saved['saved'])
        path = c.runner.drafts.path(saved['draft_id'])
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
        prepared = c.runner.prepare()
        self.assertEqual(prepared['turn_id'], turn)
        self.assertEqual(prepared['recovery_candidates'][0]['draft_id'], saved['draft_id'])
        recovered = c.runner.recovery_drafts(saved['draft_id'])['draft']
        self.assertEqual(recovered['decision'], draft)
        self.assertFalse(recovered['can_apply_unchanged'])
        self.assertFalse(recovered['warning_review_reusable'])
        self.assertNotEqual(recovered['decision']['expected_revision'], prepared['snapshot']['revision'])
        self.assertEqual(c.snapshot()['revision'], revision)
        self.assertEqual(c.snapshot()['jobs'], [])
        self.assertNotIn('private_recovery_body', json.dumps(c.traces()))

    def test_lost_claim_can_save_its_draft_but_cannot_modify_new_claim_or_read_archives(self):
        c = self.case
        turn = c.prepared(); draft = c.draft()
        for name in ('wake', 'gate_state'):
            gate.write_json(c.state_dir / (name + '.json'), {'wake_id': 'replacement'})
        ack = {'wake_id': 'replacement', 'phase': 'started', 'at_ms': c.clock}
        gate.write_json(c.state_dir / 'wake_ack.json', ack)
        with self.assertRaisesRegex(ControlError, 'stale_claim'):
            c.runner.commit(turn, draft)
        draft_id = c.runner.last_recovery['draft_id']
        self.assertEqual(gate.load_json(c.state_dir / 'wake_ack.json', {}), ack)
        event = next(e for e in c.traces() if e['phase'] == 'apply_rejected')
        self.assertEqual((event['reason'], event['exit_code'], event['current_wake_id']),
                         ('stale_claim', 10, 'replacement'))
        with self.assertRaisesRegex(ControlError, 'stale_claim'):
            c.runner.recovery_drafts(draft_id)
        replacement = DecisionSession(c.cfg, 'replacement', lambda: c.clock, c.sleep)
        self.assertEqual(replacement.recovery_drafts(draft_id)['draft']['decision'], draft)
        self.assertEqual(c.snapshot()['jobs'], [])

    def test_archive_failure_preserves_original_error(self):
        c = self.case
        turn = c.prepared(); draft = c.draft()
        c.ingest('o2', direction='outgoing')
        with patch.object(c.runner.drafts, 'save', side_effect=OSError('unavailable')):
            with self.assertRaisesRegex(ControlError, 'revision_conflict'):
                c.runner.commit(turn, draft)
        self.assertFalse(c.runner.last_recovery['saved'])
        self.assertEqual(c.snapshot()['jobs'], [])

    def test_session_cannot_commit_after_fixed_deadline_even_without_a_successor(self):
        c = self.case
        turn = c.prepared(); draft = c.draft()
        for name in ('wake', 'gate_state'):
            record = gate.load_json(c.state_dir / (name + '.json'), {})
            record.update(woken_ms=c.clock - 1000, claim_deadline_ms=c.clock + 1)
            gate.write_json(c.state_dir / (name + '.json'), record)
        c.clock += 1
        before = c.snapshot()['revision']
        with self.assertRaisesRegex(ControlError, 'stale_claim'):
            c.runner.commit(turn, draft)
        self.assertTrue(c.runner.last_recovery['saved'])
        self.assertEqual(c.snapshot()['revision'], before)
        self.assertEqual(c.snapshot()['jobs'], [])

    def test_successful_exact_retry_removes_its_recovery_copy(self):
        c = self.case
        turn = c.prepared(); draft = c.draft()
        draft_id = c.runner.drafts.save(c.runner.wake, turn, draft, 'session_input_or_io_error',
                                        draft['expected_revision'])
        with patch.object(c.runner, 'finish', return_value={'next': 'done'}):
            self.assertTrue(c.runner.commit(turn, draft)['committed'])
        self.assertFalse(c.runner.drafts.path(draft_id).exists())

    def test_archive_cannot_be_read_with_path_traversal_or_tampered_payload(self):
        c = self.case
        turn = c.prepared(); draft = c.draft()
        with self.assertRaisesRegex(ControlError, 'invalid_draft_id'):
            c.runner.recovery_drafts('../wake_ack')
        draft_id = c.runner.drafts.save('wake-1', turn, draft, 'revision_conflict', draft['expected_revision'])
        path = c.runner.drafts.path(draft_id)
        record = json.loads(path.read_text()); record['decision']['next_decision_ms'] = 1
        path.write_text(json.dumps(record))
        with self.assertRaisesRegex(ControlError, 'draft_digest_mismatch'):
            c.runner.recovery_drafts(draft_id)

    def test_cli_conflict_reports_recovery_and_records_failure_reason(self):
        c = self.case
        turn = c.prepared(); draft = c.draft()
        draft['dm_decisions'] = []
        c.ingest('o2', direction='outgoing')
        root = Path(c.tmp.name)
        cfg = root / 'automation.json'; cfg.write_text(json.dumps(c.cfg))
        decision = root / 'decision.json'; decision.write_text(json.dumps(draft))
        result = subprocess.run([sys.executable, str(Path(gate.__file__).with_name('decision_session.py')),
            '--config', str(cfg), '--wake-id', 'wake-1', 'commit', '--turn-id', turn,
            '--decision', '@' + str(decision)], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 2, result.stderr)
        error = json.loads(result.stdout)
        self.assertEqual(error['reason'], 'revision_conflict')
        self.assertTrue(error['recovery']['saved'])
        self.assertEqual(c.traces()[-1]['phase'], 'session_action_failed')
        self.assertEqual(c.traces()[-1]['reason'], 'revision_conflict')

    def test_archive_expiry_and_file_count_are_bounded(self):
        c = self.case
        turn = c.prepared(); draft = c.draft()
        archive = c.runner.drafts
        old_id = archive.save('wake-1', turn, draft, 'revision_conflict', draft['expected_revision'])
        c.clock += decision_drafts.RETENTION_MS + 1
        self.assertEqual(archive.candidates(), [])
        with self.assertRaisesRegex(ControlError, 'draft_expired'):
            archive.read(old_id)
        with patch.object(decision_drafts, 'MAX_DRAFTS', 3):
            for index in range(5):
                changed = {**draft, 'decision_id': 'draft-' + str(index)}
                archive.save('wake-1', turn, changed, 'revision_conflict', draft['expected_revision'])
            self.assertLessEqual(len(archive.files()), 3)
            self.assertFalse(archive.path(old_id).exists())
