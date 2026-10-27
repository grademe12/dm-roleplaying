"""Evaluation integrity checks; no production configuration or model calls."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

import run_memory_eval as evaluation


class MemoryEvaluationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='memory-test-', dir=Path(__file__).parent)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def prepare(self):
        folder = self.root / 'run'
        evaluation.prepare(folder)
        return folder

    def test_rejects_future_context_and_evidence(self):
        cases = evaluation.fixtures.build_cases()
        for component, field in (('recent', 'at'), ('evidence', 'recorded_at'), ('stored', 'recorded_at')):
            changed = copy.deepcopy(cases)
            changed[0][component][0][field] = '2026-10-19T00:00:00+09:00'
            with self.subTest(component=component), self.assertRaisesRegex(ValueError, 'Future'):
                evaluation.validate(changed)

    def test_current_store_does_not_search_preference_in_details(self):
        case = next(c for c in evaluation.fixtures.build_cases() if c['id'] == 'scoped_preference')
        # A sufficient episode exists, but the fixed summary-only query misses it.
        self.assertEqual(evaluation.current_recall(case, self.root), [])
        self.assertIn('correction', case['stored'][0]['details'])

    def test_old_counterexample_falls_outside_current_top_ten(self):
        case = next(c for c in evaluation.fixtures.build_cases() if c['id'] == 'past_behavior')
        found = evaluation.current_recall(case, self.root)
        self.assertEqual(len(found), 10)
        self.assertNotIn('photo-hesitated', {f['id'] for f in found})
        self.assertNotIn('photo-resolved', {f['id'] for f in found})

    def test_model_inputs_exclude_historical_outcomes_and_rubrics(self):
        folder = self.prepare()
        rows = [json.loads(line) for line in (folder / 'inputs.jsonl').read_text(encoding='utf-8').splitlines()]
        self.assertEqual(len(rows), 12)
        for row in rows:
            model_input = row['model_input']
            self.assertEqual(set(model_input), {'instruction', 'context'})
            self.assertEqual(set(model_input['context']), {'now', 'recent_messages', 'state', 'memories'})
            if row['case_id'] == 'nickname':
                ids = {f['id'] for f in model_input['context']['memories']}
                self.assertEqual('nickname-accepted' in ids, row['condition'] == 'evidence')

    def test_unrun_trials_are_pending_and_missing_storage_is_not_retrieval_failure(self):
        result = evaluation.report(self.prepare())
        rows = {r['case_id']: r for r in result['rows']}
        self.assertEqual(rows['nickname']['synthetic_stored_coverage'], 0)
        self.assertIsNone(rows['nickname']['synthetic_retrieved_coverage'])
        self.assertIsNone(rows['intent_control']['synthetic_stored_coverage'])
        self.assertEqual(rows['scoped_preference']['synthetic_stored_coverage'], 1)
        for row in rows.values():
            self.assertEqual(row['new_trial'], {'current': 'pending', 'evidence': 'pending'})

    def test_rejects_modified_frozen_input(self):
        folder = self.prepare()
        with (folder / 'inputs.jsonl').open('a', encoding='utf-8') as handle:
            handle.write('\n')
        with self.assertRaisesRegex(ValueError, 'Frozen'):
            evaluation.report(folder)

    def test_scored_review_needs_output_model_settings_and_evidence(self):
        folder = self.prepare()
        review = evaluation.read(folder / 'reviews.json')
        row = review['reviews'][0]
        row['checks'][0].update(passed=True, note='first response preserves the established nickname')
        (folder / 'reviews.json').write_text(evaluation.dump(review), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'actual output'):
            evaluation.report(folder)
        review.update(model='test-model-not-a-real-model-run', settings={'temperature': 0})
        row['response'] = 'synthetic output used only for scorer validation'
        row['checks'][1].update(passed=False, note='test-only failed criterion')
        (folder / 'reviews.json').write_text(evaluation.dump(review), encoding='utf-8')
        self.assertEqual(evaluation.report(folder)['rows'][0]['new_trial']['current'], 'fail')

    def test_rejects_duplicate_or_wrong_run_reviews(self):
        folder = self.prepare()
        original = evaluation.read(folder / 'reviews.json')
        changed = copy.deepcopy(original)
        changed['run_id'] = 'another-run'
        (folder / 'reviews.json').write_text(evaluation.dump(changed), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'different run'):
            evaluation.report(folder)
        original['reviews'].append(copy.deepcopy(original['reviews'][0]))
        (folder / 'reviews.json').write_text(evaluation.dump(original), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'Duplicate'):
            evaluation.report(folder)

    def test_existing_run_is_not_overwritten(self):
        folder = self.prepare()
        with self.assertRaises(FileExistsError):
            evaluation.prepare(folder)


if __name__ == '__main__':
    unittest.main()
