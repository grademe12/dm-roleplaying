"""Audit integrity checks on disposable synthetic trials, never original Muse data."""
from pathlib import Path
from contextlib import closing
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import audit_memory_chain_trial as verification
import run_memory_chain_eval as chain


class MemoryChainAuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='audit-test-', dir=Path(__file__).parent)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        corpus = [c for c in chain.memory_chain_cases.cases() if c['id'] == 'source-channel']
        fake = patch.object(chain.memory_chain_cases, 'cases', return_value=corpus)
        fake.start(); self.addCleanup(fake.stop)
        self.trial = self.root / 'trial'; self.run = self.trial / 'run'
        chain.prepare(self.run)
        self.case = 'source-channel'; self.path = self.run / self.case
        chain.next_input(self.run, self.case)
        candidate = self.root / 'candidate.json'; chain.write(candidate, {'memory_ops': []})
        chain.submit(self.run, self.case, candidate, 'no-model-test', {}, 'harness_test')
        chain.question(self.run, self.case)
        chain.query(self.run, self.case, {'operation': 'memory-recall', 'arguments': {'keywords': ['댓글']}})
        answer = self.root / 'answer.json'; chain.write(answer, {'answer': '확인 못 함', 'found_ids': []})
        chain.answer(self.run, self.case, answer, 'no-model-test', {}, 'harness_test')

    def test_replay_preserves_empty_candidates_and_original_files(self):
        result = verification.audit(self.trial)
        self.assertFalse(result['model_called'])
        self.assertTrue(result['original_files_unchanged'])
        self.assertEqual(result['cases'][0]['memory_states'], [])
        self.assertEqual(result['case_steps'], 1)

    def test_even_format_only_candidate_changes_break_submitted_hash(self):
        path = self.path / 'candidate-0.json'
        path.write_bytes(path.read_bytes() + b'\n')
        with self.assertRaises(AssertionError): verification.audit(self.trial)

    def test_forged_query_result_with_updated_hash_is_caught_by_replay(self):
        path = self.path / 'query-0.json'
        value = chain.read(path); value['returned_ids'] = ['invented']
        chain.write(path, value)
        check = chain.read(self.path / 'answer-check.json')
        check['query_trace_sha256'] = [chain.digest(path.read_bytes())]
        chain.write(self.path / 'answer-check.json', check)
        with self.assertRaises(AssertionError): verification.audit(self.trial)

    def test_changed_sqlite_row_is_caught_without_modifying_originals(self):
        with closing(sqlite3.connect(self.path / 'memory.db')) as db:
            with db:
                db.execute("UPDATE dm_jobs SET text='changed fixture'")
        with self.assertRaises(AssertionError): verification.audit(self.trial)

    def test_live_wal_snapshot_is_rejected_instead_of_ignored(self):
        with closing(sqlite3.connect(self.path / 'memory.db')) as db:
            with db:
                db.execute("UPDATE dm_jobs SET text='uncheckpointed fixture'")
            with self.assertRaisesRegex(ValueError, 'sidecar'):
                verification.audit(self.trial)


if __name__ == '__main__':
    unittest.main()
