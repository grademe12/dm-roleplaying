"""Replay checks must distinguish query errors, empty results and missing evidence."""
from pathlib import Path
import tempfile
import unittest

import replay_memory_extraction_eval as replay
import memory_continuity
from store import Store


class ExtractionReplayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='query-replay-', dir=Path(__file__).parent)
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / 'fixture.db')
        self.store.seed({}, 1792363200000)
        memory_continuity.configure(self.store, 'main', {'mode': 'enforce'}, 1792363200000)

    def query(self, suffix):
        return 'python3 /tmp/ctl.py --db /tmp/original.db ' + suffix

    def test_recorded_mutations_or_unrecognised_flags_are_rejected(self):
        for suffix in ('memory-configure --settings {}', 'recall --keywords x --other-command rm'):
            with self.subTest(suffix=suffix), self.assertRaises(ValueError):
                replay.query_options(self.query(suffix))

    def test_empty_declared_ids_do_not_pass_when_all_queries_fail(self):
        result = replay.replay_queries(self.store, dict(qid='absent', found_ids=[], queries_used=[
            self.query('memory-recall --keywords 면허 --limit 20')]))
        self.assertFalse(result['all_declared_actually_returned'])
        self.assertFalse(result['no_matches_in_successful_queries'])
        self.assertEqual(result['successful_queries'], 0)
        self.assertEqual(result['failed_queries'], 1)

    def test_successful_empty_search_is_recorded_without_claiming_universal_absence(self):
        result = replay.replay_queries(self.store, dict(qid='absent', found_ids=[], queries_used=[
            self.query('recall --keywords 면허 --limit 10')]))
        self.assertTrue(result['all_declared_actually_returned'])
        self.assertTrue(result['no_matches_in_successful_queries'])
        self.assertFalse(result['absence_proven'])

    def test_declared_ids_need_actual_returned_evidence(self):
        result = replay.replay_queries(self.store, dict(qid='missing', found_ids=['invented-id'], queries_used=[
            self.query('recall --keywords 면허 --limit 10')]))
        self.assertFalse(result['all_declared_actually_returned'])
        self.assertEqual(result['successful_queries'], 1)


if __name__ == '__main__':
    unittest.main()
