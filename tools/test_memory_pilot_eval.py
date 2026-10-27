"""Check pilot comparison provenance and scoring without making model calls."""
import copy
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

import run_memory_eval as evaluation
import run_memory_pilot_eval as pilot


class MemoryPilotEvaluationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='pilot-eval-', dir=Path(__file__).parent)
        cls.root = Path(cls.temp.name)
        cls.folder = cls.root / 'run'
        cls.result = pilot.prepare(cls.folder)
        cls.jobs = [json.loads(line) for line in (cls.folder / 'inputs.jsonl').read_text(encoding='utf-8').splitlines()]
        cls.refs = evaluation.read(cls.folder / 'reference.json')

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_real_pipeline_activates_only_pilot_rows_and_all_model_trials_are_pending(self):
        self.assertFalse(self.result['model_executed'])
        self.assertEqual(len(self.jobs), 12)
        report = evaluation.report(self.folder)
        for row in report['rows']:
            self.assertEqual(row['new_trial'], {'current': 'pending', 'pilot': 'pending'})
        for case in self.refs:
            diagnostic = case['condition_diagnostics']
            self.assertEqual(diagnostic['current']['memory_statuses'], [])
            statuses = diagnostic['pilot']['memory_statuses']
            if case['id'] in ('nickname', 'self_statement', 'scoped_preference'):
                self.assertEqual(len(statuses), 1)
                self.assertEqual(statuses[0]['status'], 'active')
                self.assertEqual(bool(statuses[0]['job_id']), case['id'] != 'scoped_preference')
            else:
                self.assertEqual(statuses, [])
        self.assertFalse(any(path.suffix == '.db' for path in self.folder.rglob('*')))

    def test_controls_and_recent_inputs_are_matched_without_future_or_grading_leaks(self):
        by_case = {}
        for job in self.jobs:
            by_case.setdefault(job['case_id'], {})[job['condition']] = job
            context = job['model_input']['context']
            self.assertEqual(set(context), {'now', 'recent_messages', 'state', 'memories'})
            cutoff = evaluation.fixtures.milliseconds(context['now'])
            for message in context['recent_messages']:
                self.assertLessEqual(evaluation.fixtures.milliseconds(message['at']), cutoff)
            for fact in context['memories']:
                self.assertLessEqual(fact.get('activated_ms') or fact['recorded_ms'], cutoff)
        for case_id, pair in by_case.items():
            current = pair['current']['model_input']['context']
            changed = pair['pilot']['model_input']['context']
            for field in ('now', 'recent_messages', 'state'):
                self.assertEqual(current[field], changed[field])
            if case_id in ('history_count', 'intent_control'):
                self.assertEqual(pair['current']['input_sha256'], pair['pilot']['input_sha256'])
        preference = by_case['scoped_preference']['current']['model_input']['context']
        self.assertIn('JavaScript의 map', preference['recent_messages'][0]['text'])
        nickname = by_case['nickname']['current']['model_input']['context']
        self.assertNotIn('오늘도', nickname['recent_messages'][-1]['text'])

    def test_history_plan_recovers_delay_and_completion_and_declares_authored_query(self):
        case = next(c for c in self.refs if c['id'] == 'past_behavior')
        diagnostic = case['condition_diagnostics']
        expected = {'photo-hesitated', 'photo-resolved'}
        self.assertFalse(expected.intersection(diagnostic['current']['returned_ids']))
        self.assertTrue(expected.issubset(diagnostic['pilot']['returned_ids']))
        self.assertIn('authored', diagnostic['pilot']['strategy'])

    def test_honest_uncertainty_can_fail_recall_while_passing_grounding(self):
        target = self.root / 'scored'
        shutil.copytree(self.folder, target)
        reviews = evaluation.read(target / 'reviews.json')
        reviews.update(model='test-only-no-model-call', settings={'purpose': 'scorer test'})
        row = next(r for r in reviews['reviews'] if r['case_id'] == 'self_statement' and r['condition'] == 'current')
        row['response'] = '테스트용 불확실성 응답: 전에 뭐라고 했는지는 지금 확인이 안 돼.'
        for check, passed in zip(row['checks'], (False, True, True)):
            check.update(passed=passed, note='합성 채점기 검사; 실제 모델 시행 결과가 아님')
        (target / 'reviews.json').write_text(evaluation.dump(reviews), encoding='utf-8')
        result = next(r for r in evaluation.report(target)['rows'] if r['case_id'] == 'self_statement')
        self.assertEqual(result['rubric_axes']['recall']['current'], 'fail')
        self.assertEqual(result['rubric_axes']['grounding']['current'], 'pass')
        self.assertEqual(result['new_trial']['pilot'], 'pending')

    def test_future_authored_history_is_rejected(self):
        original = pilot.history_plan
        def future(identifier):
            plan = copy.deepcopy(original(identifier))
            if plan:
                plan['at'] = '2099-01-01T00:00:00+09:00'
            return plan
        with patch.object(pilot, 'history_plan', future), self.assertRaisesRegex(ValueError, 'Future authored'):
            pilot.prepare(self.root / 'future')

if __name__ == '__main__':
    unittest.main()
