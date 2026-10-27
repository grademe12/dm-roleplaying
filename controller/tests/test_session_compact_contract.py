"""Active DecisionSession path with synthetic DB/transport; no MuseExecutor calls."""
import copy
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from common import ControlError
import conversation_flow
import decision_session
import gate
import instrumentation
import prepared_inputs
import test_decision_session as lifecycle


class SessionCompactTests(unittest.TestCase):
    setUp = lifecycle.DecisionSessionTests.setUp
    tearDown = lifecycle.DecisionSessionTests.tearDown
    snapshot = lifecycle.DecisionSessionTests.snapshot
    decision = lifecycle.DecisionSessionTests.decision
    review = lifecycle.DecisionSessionTests.review
    ingest = lifecycle.DecisionSessionTests.ingest
    activity = lifecycle.DecisionSessionTests.activity
    free = lifecycle.DecisionSessionTests.free
    sleep = lifecycle.DecisionSessionTests.sleep
    recent = lifecycle.DecisionSessionTests.recent
    draft = lifecycle.DecisionSessionTests.draft

    def prepare(self, mode='bundled'):
        self.ingest(); self.recent()
        return self.runner.prepare(mode=mode, decision_format='compact-v1')

    def compact(self, full=None):
        result = copy.deepcopy(full or self.draft())
        result.pop('decision_id'); result.pop('expected_revision')
        for dm in result.get('dm_decisions', []):
            if dm.get('action') in ('reply', 'initiate'):
                review = dm['conversation_review']
                ctx = review['context']
                dm['channel'] = ctx['channel']
                dm['conversation_review'] = {**{k: ctx[k] for k in ('previous_self', 'user_uptake', 'topic')},
                                             'memory': review['memory'], 'checks': review['checks']}
        return result

    def commit(self, prepared, decision=None, **kwargs):
        return self.runner.commit(prepared['turn_id'], decision or self.compact(),
            decision_format='compact-v1', decision_generation=prepared['decision_generation'], **kwargs)

    def receipts(self, prepared):
        return prepared_inputs.read_bundle(self.runner, prepared['turn_id'], prepared['input_id'])['input_receipts']

    def jobs(self):
        with self.store.connect() as db:
            return [dict(r) for r in db.execute('SELECT * FROM dm_jobs')]

    def test_bundled_direct_commit_and_replay_without_executor(self):
        prepared = self.prepare()
        draft = self.compact()
        self.assertEqual(prepared['input_mode'], 'bundled')
        self.assertEqual(prepared['decision_contract']['schema_read']['command'],
                         'decision_session.py read-decision-schema')
        # Direct sessions reject an already finished turn. Replay is a recovery
        # path while finish is still pending, not a new turn's fresh decision.
        with patch.object(self.runner, 'finish', side_effect=OSError('pending finish')):
            result = self.commit(prepared, draft)
        self.assertTrue(result['committed'])
        self.assertFalse((self.state_dir / 'muse-executor').exists())
        self.assertTrue(self.commit(prepared, draft)['committed'])
        self.assertEqual(self.jobs()[0]['status'], 'confirmed')
        self.assertEqual(len(self.jobs()), 1)
        self.assertEqual(self.runner.state()['turn_count'], 1)
        self.assertNotIn('decision_id', draft)
        files = list((self.state_dir / 'compact-decisions' / 'wake-1').glob('*.json'))
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].stat().st_mode & 0o777, 0o600)

    def test_split_input_direct_commit_uses_submitted_receipts(self):
        prepared = self.prepare('compact')
        self.assertEqual(prepared['input_mode'], 'compact')
        self.assertTrue(self.commit(prepared, input_receipts=self.receipts(prepared))['committed'])
        self.assertFalse((self.state_dir / 'muse-executor').exists())

    def test_more_receipts_can_fix_uncommitted_metadata_without_rewriting_answer(self):
        prepared = self.prepare('compact')
        draft = self.compact()
        with self.assertRaises(ControlError):
            self.commit(prepared, draft, input_receipts=[])
        self.assertEqual(self.jobs(), [])
        self.assertTrue(self.commit(prepared, draft, input_receipts=self.receipts(prepared))['committed'])

    def test_unsubmitted_context_receipt_does_not_become_consumed_metadata(self):
        prepared = self.prepare('compact')
        bundle = prepared_inputs.read_bundle(self.runner, prepared['turn_id'], prepared['input_id'])
        state = self.runner.state()
        receipts = [token for token in bundle['input_receipts']
                    if state['input_offers'][instrumentation.digest(token)]['kind'] != 'context']
        with self.assertRaises(ControlError):
            self.commit(prepared, input_receipts=receipts)
        self.assertEqual(self.jobs(), [])

    def test_missing_or_wrong_generation_never_gets_current_revision(self):
        prepared = self.prepare()
        for generation, reason in ((None, 'decision_generation_required'), ('0' * 32, 'decision_generation_mismatch')):
            with self.assertRaisesRegex(ControlError, reason):
                self.runner.commit(prepared['turn_id'], self.compact(), decision_format='compact-v1',
                                   decision_generation=generation)
        self.assertEqual(self.jobs(), [])

    def test_reprepare_same_revision_rejects_old_generation(self):
        first = self.prepare()
        draft = self.compact()
        second = self.runner.prepare(mode='bundled', decision_format='compact-v1')
        self.assertEqual(first['turn_id'], second['turn_id'])
        self.assertEqual(first['snapshot']['revision'], second['snapshot']['revision'])
        with self.assertRaisesRegex(ControlError, 'decision_generation_mismatch'):
            self.commit(first, draft)
        self.assertTrue(self.commit(second, draft)['committed'])

    def test_new_database_input_rejects_compact_without_revision_rewrite(self):
        prepared = self.prepare()
        draft = self.compact()
        self.ingest('o2', direction='outgoing'); self.recent(('m1',), outgoing=('o2',))
        with self.assertRaisesRegex(ControlError, 'prepare_inputs_changed'):
            self.commit(prepared, draft)
        self.assertEqual(self.jobs(), [])

    def test_new_cache_echo_without_ingest_also_invalidates_bundled_output(self):
        prepared = self.prepare()
        self.recent(('m1',), outgoing=('echo-not-ingested',))
        with self.assertRaisesRegex(ControlError, 'prepare_inputs_changed'):
            self.commit(prepared)
        self.assertEqual(self.jobs(), [])

    def test_semantic_and_part_guards_still_rollback_whole_decision(self):
        prepared = self.prepare()
        for modify, reason in (
            (lambda d: d['dm_decisions'][0]['conversation_review']['checks']['conflict'].update(passed=False),
             'conversation_review_failed'),
            (lambda d: d['dm_decisions'][0]['conversation_review'].pop('previous_self'), 'invalid_compact_review'),
            (lambda d: d['dm_decisions'][0].update(parts=['x'] * 6), 'parts_limit_exceeded'),
            (lambda d: d.update(expected_revision=-1), 'compact_managed_field'),
        ):
            draft = self.compact(); modify(draft)
            with self.assertRaisesRegex(ControlError, reason):
                self.commit(prepared, draft)
            self.assertEqual(self.jobs(), [])

    def test_warning_approval_stays_bound_to_same_raw_decision(self):
        conversation_flow.configure(self.store, 'main', {'mode': 'shadow'}, self.clock)
        prepared = self.prepare()
        draft = self.compact()
        result = self.commit(prepared, draft)
        self.assertFalse(result['committed'])
        self.assertEqual(self.jobs(), [])
        self.assertTrue(self.commit(prepared, draft, warning_review=result['commit']['warning_review'])['committed'])

    def test_saved_decision_recovers_after_metadata_failure_even_after_new_prepare(self):
        prepared = self.prepare()
        draft = self.compact()
        original = gate.write_json
        def crash(path, data):
            if Path(path) == self.runner.path and data.get('committed_turn'):
                raise OSError('injected metadata failure')
            return original(path, data)
        with patch.object(gate, 'write_json', side_effect=crash):
            result = self.commit(prepared, draft)
        self.assertTrue(result['committed'])
        self.assertEqual(result['after']['next'], 'retry_same_commit')
        self.runner.prepare(mode='bundled', decision_format='compact-v1')
        self.assertTrue(self.commit(prepared, draft)['committed'])
        self.assertEqual(len(self.jobs()), 1)
        self.assertEqual(self.runner.state()['turn_count'], 1)

    def test_io_before_apply_retries_saved_compact_intent(self):
        prepared = self.prepare()
        draft = self.compact()
        with patch.object(self.runner, '_commit', side_effect=OSError('before DB apply')):
            with self.assertRaises(OSError):
                self.commit(prepared, draft)
        self.assertEqual(self.jobs(), [])
        self.assertTrue(self.commit(prepared, draft)['committed'])
        self.assertEqual(len(self.jobs()), 1)

    def test_reprepare_between_assembly_and_apply_is_checked_under_apply_lock(self):
        prepared = self.prepare()
        original = self.runner._commit
        def change(*args, **kwargs):
            self.runner.prepare(mode='bundled', decision_format='compact-v1')
            return original(*args, **kwargs)
        with patch.object(self.runner, '_commit', side_effect=change):
            with self.assertRaisesRegex(ControlError, 'decision_generation_mismatch'):
                self.commit(prepared)
        self.assertEqual(self.jobs(), [])

    def test_cache_echo_between_assembly_and_apply_is_checked_under_apply_lock(self):
        prepared = self.prepare()
        original = self.runner._commit
        def change(*args, **kwargs):
            self.recent(('m1',), outgoing=('echo-not-ingested',))
            return original(*args, **kwargs)
        with patch.object(self.runner, '_commit', side_effect=change):
            with self.assertRaisesRegex(ControlError, 'prepare_inputs_changed'):
                self.commit(prepared)
        self.assertEqual(self.jobs(), [])

    def test_schema_sections_work_in_both_input_modes_and_register_freshness(self):
        for mode in ('bundled', 'compact'):
            prepared = self.prepare(mode)
            result = self.runner.read_decision_schema(prepared['turn_id'], prepared['decision_generation'], ['activities'])
            self.assertIn(result['sections']['activities'],
                          (decision_session.ROOT / 'API.md').read_text(encoding='utf-8'))
            self.assertIn('API.md', self.runner.state()['prepared_document_hashes'])
            original = Path.read_text
            def changed(path, *args, **kwargs):
                content = original(path, *args, **kwargs)
                return content + '\nchanged' if path.name == 'API.md' else content
            with patch.object(Path, 'read_text', changed):
                with self.assertRaisesRegex(ControlError, 'prepare_inputs_changed'):
                    self.commit(prepared, input_receipts=[] if mode == 'compact' else None)

    def test_failed_prepare_preserves_previous_output_generation(self):
        prepared = self.prepare()
        with patch.object(prepared_inputs, 'configure', side_effect=ControlError('synthetic_prepare_failure')):
            with self.assertRaisesRegex(ControlError, 'synthetic_prepare_failure'):
                self.runner.prepare(decision_format='compact-v1')
        self.assertEqual(self.runner.state()['compact_contract']['generation'], prepared['decision_generation'])
        self.assertTrue(self.commit(prepared)['committed'])

    def test_cli_routes_prepare_and_commit_directly_without_executor_files(self):
        self.ingest(); self.recent()
        config = Path(self.tmp.name) / 'automation.json'
        config.write_text(json.dumps(self.cfg))
        prefix = ['--config', str(config), '--wake-id', 'wake-1']
        with patch.object(decision_session, 'DecisionSession', return_value=self.runner), \
                redirect_stdout(io.StringIO()) as output:
            code = decision_session.main(prefix + ['prepare', '--decision-format', 'compact-v1', '--mode', 'bundled'])
        self.assertEqual(code, 0)
        prepared = json.loads(output.getvalue())
        draft_file = Path(self.tmp.name) / 'compact.json'
        draft_file.write_text(json.dumps(self.compact()))
        original = draft_file.read_bytes()
        with patch.object(decision_session, 'DecisionSession', return_value=self.runner), \
                redirect_stdout(io.StringIO()) as output:
            code = decision_session.main(prefix + ['commit', '--turn-id', prepared['turn_id'],
                '--decision-format', 'compact-v1', '--decision-generation', prepared['decision_generation'],
                '--decision', '@' + str(draft_file)])
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(output.getvalue())['committed'])
        self.assertEqual(draft_file.read_bytes(), original)
        self.assertFalse((self.state_dir / 'muse-executor').exists())


if __name__ == '__main__':
    unittest.main()
