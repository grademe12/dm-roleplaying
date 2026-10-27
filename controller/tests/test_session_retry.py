"""Resume partial reads and rejected commits without losing input/generator bindings."""
from contextlib import redirect_stdout
import copy
import io
import json
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch

from common import ControlError, encode
import decision_session
import gate
import prepared_inputs as inputs
import test_decision_session as lifecycle
import test_reply_generator as generator_tests
import test_character_brief as character_tests


class SessionRetryTests(unittest.TestCase):
    def setUp(self):
        self.c = lifecycle.DecisionSessionTests()
        self.c.setUp()
        self.addCleanup(self.c.tearDown)
        self.c.ingest(); self.c.recent()

    def prepare(self, **kwargs):
        return self.c.runner.prepare(mode='compact', **kwargs)

    def bundle(self, p, receipts=None):
        return inputs.read_bundle(self.c.runner, p['turn_id'], p['input_id'], input_receipts=receipts)

    def test_retained_bundle_returns_no_bodies_and_still_commits(self):
        p = self.prepare(queries=[['hello']])
        first = self.bundle(p)
        before = self.c.runner.state()
        again = self.bundle(p, first['input_receipts'])
        self.assertEqual(again['components'], [])
        self.assertEqual(again['input_receipts'], first['input_receipts'])
        self.assertEqual(self.c.runner.state()['input_offers'], before['input_offers'])
        self.assertLess(len(encode(again)), len(encode(first)))
        with patch.object(self.c.runner, 'finish', return_value={'next': 'done'}):
            result = self.c.runner.commit(p['turn_id'], self.c.draft(), input_receipts=again['input_receipts'])
        self.assertTrue(result['committed'])

    def test_only_explicitly_retained_components_are_skipped(self):
        p = self.prepare(queries=[['hello']])
        first = self.bundle(p)
        # A server-issued receipt alone is never proof that the caller read it.
        reread = self.bundle(p)
        self.assertEqual(len(reread['components']), len(first['components']))
        docs = list(first['document_receipts'].values())
        partial = self.bundle(p, docs)
        self.assertEqual({c['kind'] for c in partial['components']}, {'state', 'context', 'recall'})

    def test_missing_receipt_recovery_reads_only_missing_context(self):
        p = self.prepare()
        first = self.bundle(p)
        receipts = [t for t in first['input_receipts'] if
                    self.c.runner.state()['input_offers'][inputs.instrumentation.digest(t)]['kind'] != 'context']
        with self.assertRaisesRegex(ControlError, 'message_input_required'):
            self.c.runner.commit(p['turn_id'], self.c.draft(), input_receipts=receipts)
        self.assertEqual(self.c.runner.last_recovery['next'], 'complete_inputs_and_retry_commit')
        generation = self.c.runner.state()['reply_source']['generation']
        rest = self.bundle(p, receipts)
        self.assertEqual([c['kind'] for c in rest['components']], ['context'])
        self.assertEqual(self.c.runner.state()['reply_source']['generation'], generation)
        with patch.object(self.c.runner, 'finish', return_value={'next': 'done'}):
            self.assertTrue(self.c.runner.commit(p['turn_id'], self.c.draft(),
                                                input_receipts=rest['input_receipts'])['committed'])

    def test_partial_context_is_supplemented_by_exact_missing_ids(self):
        self.c.ingest('m2'); self.c.recent(('m1', 'm2'))
        p = self.prepare()
        read = self.c.runner.read_input(p['turn_id'], p['input_id'], 'context',
                                       channel='instagram', message_ids=['m1'])
        rest = self.bundle(p, [read['receipt']])
        messages = [m['id'] for c in rest['components'] if c['kind'] == 'context'
                    for m in c['content']['messages']]
        self.assertEqual(messages, ['m2'])

    def test_partial_state_does_not_count_as_complete_section(self):
        self.c.ingest('m2'); self.c.recent(('m1', 'm2'))
        p = self.prepare()
        read = self.c.runner.read_input(p['turn_id'], p['input_id'], 'state',
                                       sections=['messages'], offset=0, limit=1)
        rest = self.bundle(p, [read['receipt']])
        state = next(c['content'] for c in rest['components'] if c['kind'] == 'state')
        self.assertEqual(len(state['messages']), 2)

    def test_narrow_recall_does_not_replace_prepared_recall(self):
        p = self.prepare(queries=[['hello']])
        read = self.c.runner.read_input(p['turn_id'], p['input_id'], 'recall', index=0, limit=1)
        rest = self.bundle(p, [read['receipt']])
        self.assertEqual(sum(c['kind'] == 'recall' for c in rest['components']), 1)

    def test_reuse_still_detects_changed_context(self):
        p = self.prepare(); first = self.bundle(p)
        path = self.c.state_dir / 'recent_thread.json'
        data = gate.load_json(path, {}); data['messages'][0]['text'] = 'edited'
        gate.write_json(path, data)
        with self.assertRaisesRegex(ControlError, 'prepare_inputs_changed'):
            self.bundle(p, first['input_receipts'])

    def test_reuse_still_detects_changed_revision(self):
        p = self.prepare(); first = self.bundle(p)
        self.c.ingest('outgoing', direction='outgoing')
        with self.assertRaisesRegex(ControlError, 'prepare_inputs_changed'):
            self.bundle(p, first['input_receipts'])

    def test_reuse_still_detects_changed_optional_document(self):
        p = self.prepare()
        name = 'design/character/profile.md'
        read = self.c.runner.read_input(p['turn_id'], p['input_id'], 'document', name=name)
        original = Path.read_text
        def changed(path, *args, **kwargs):
            value = original(path, *args, **kwargs)
            return value + '\nchanged' if path.name == 'profile.md' else value
        with patch.object(Path, 'read_text', changed):
            with self.assertRaisesRegex(ControlError, 'prepare_inputs_changed'):
                self.bundle(p, [read['receipt']])

    def test_reprepare_rejects_old_state_receipts_and_forged_tokens(self):
        p = self.prepare(); first = self.bundle(p)
        new = self.prepare()
        for tokens in (first['input_receipts'], ['invented'], 'not-an-array'):
            with self.subTest(tokens=tokens):
                with self.assertRaisesRegex(ControlError, 'invalid_input_receipt'):
                    self.bundle(new, tokens)

    def test_post_commit_failure_requires_finish_not_prepare(self):
        p = self.c.runner.prepare()
        with self.c.store.connect() as db:
            decisions_before = db.execute('SELECT count(*) FROM decisions').fetchone()[0]
        with patch.object(self.c.runner, 'finish', side_effect=OSError('post-commit')):
            result = self.c.runner.commit(p['turn_id'], self.c.draft(), conversation_ended=True)
        self.assertTrue(result['committed']); self.assertEqual(result['after']['next'], 'finish')
        before = self.c.runner.path.read_bytes()
        with self.assertRaisesRegex(ControlError, 'finish_required'):
            self.c.runner.prepare()
        self.assertEqual(self.c.runner.last_recovery, {'next': 'finish', 'turn_id': p['turn_id'], 'committed': True})
        self.assertEqual(self.c.runner.path.read_bytes(), before)
        self.c.runner.finish(p['turn_id'])
        with self.c.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM decisions').fetchone()[0], decisions_before + 1)
            self.assertEqual(db.execute('SELECT count(*) FROM dm_jobs').fetchone()[0], 1)

    def test_io_retry_keeps_decision_and_real_conflict_requires_prepare(self):
        p = self.c.runner.prepare(); draft = self.c.draft()
        with patch.object(self.c.runner.store, 'apply', side_effect=sqlite3.OperationalError('busy')):
            with self.assertRaises(sqlite3.OperationalError):
                self.c.runner.commit(p['turn_id'], draft)
        self.assertEqual(self.c.runner.last_recovery['next'], 'retry_same_commit')
        self.c.ingest('outgoing', direction='outgoing')
        with self.assertRaisesRegex(ControlError, 'revision_conflict'):
            self.c.runner.commit(p['turn_id'], draft)
        self.assertEqual(self.c.runner.last_recovery['next'], 'prepare_and_review_changes')

    def test_lost_claim_stops_instead_of_reprepare(self):
        p = self.c.runner.prepare(); draft = self.c.draft()
        gate.write_json(self.c.state_dir / 'wake.json', {'wake_id': 'replacement'})
        with self.assertRaisesRegex(ControlError, 'stale_claim'):
            self.c.runner.commit(p['turn_id'], draft)
        self.assertEqual(self.c.runner.last_recovery['next'], 'stop')

    def test_cli_accepts_retained_input_file(self):
        p = self.prepare(); first = self.bundle(p)
        folder = Path(self.c.tmp.name)
        config = folder / 'config.json'; config.write_text(encode(self.c.cfg))
        receipt_file = folder / 'receipts.json'; receipt_file.write_text(encode(first['input_receipts']))
        output = io.StringIO()
        with patch('decision_session.DecisionSession', return_value=self.c.runner), redirect_stdout(output):
            code = decision_session.main(['--config', str(config), '--wake-id', 'wake-1',
                'read-bundle', '--turn-id', p['turn_id'], '--input-id', p['input_id'],
                '--input-receipts', '@' + str(receipt_file)])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())['components'], [])

    def test_cli_malformed_decision_keeps_existing_preparation(self):
        p = self.c.runner.prepare(decision_format='compact-v1')
        before = self.c.runner.path.read_bytes()
        folder = Path(self.c.tmp.name)
        config = folder / 'config.json'; config.write_text(encode(self.c.cfg))
        decision = folder / 'decision.json'; decision.write_text('{broken')
        output = io.StringIO()
        with patch('decision_session.DecisionSession', return_value=self.c.runner), redirect_stdout(output):
            code = decision_session.main(['--config', str(config), '--wake-id', 'wake-1',
                'commit', '--turn-id', p['turn_id'], '--decision', '@' + str(decision)])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(output.getvalue())['recovery']['next'], 'correct_and_retry_commit')
        self.assertEqual(self.c.runner.path.read_bytes(), before)

    def test_changed_document_during_supplement_rejects_entire_bundle(self):
        p = self.prepare()
        read = self.c.runner.read_input(p['turn_id'], p['input_id'], 'document',
                                       name='design/character/profile.md')
        original = Path.read_text
        count = 0
        def changed(path, *args, **kwargs):
            nonlocal count
            value = original(path, *args, **kwargs)
            if path.name == 'profile.md':
                count += 1
                if count > 1:return value + '\nchanged during supplementation'
            return value
        with patch.object(Path, 'read_text', changed):
            with self.assertRaisesRegex(ControlError, 'prepare_inputs_changed'):
                self.bundle(p, [read['receipt']])


class CharacterRetryTests(unittest.TestCase):
    def setUp(self):
        self.character = character_tests.CharacterBriefTests()
        self.character.setUp()
        self.addCleanup(self.character.doCleanups)
        self.character.activate()
        self.c = self.character.c

    def test_retained_rendered_documents_and_brief_still_commit(self):
        p = self.character.prepare('compact')
        first = inputs.read_bundle(self.c.runner, p['turn_id'], p['input_id'])
        again = inputs.read_bundle(self.c.runner, p['turn_id'], p['input_id'],
                                   input_receipts=first['input_receipts'])
        self.assertEqual(again['components'], [])
        self.assertEqual(again['input_receipts'], first['input_receipts'])
        with patch.object(self.c.runner, 'finish', return_value={'next': 'done'}):
            self.assertTrue(self.c.runner.commit(p['turn_id'], self.c.draft(),
                input_receipts=again['input_receipts'])['committed'])

    def test_source_correction_rejects_retained_bundle_then_reprepares(self):
        p = self.character.prepare('compact')
        first = inputs.read_bundle(self.c.runner, p['turn_id'], p['input_id'])
        self.character.correct_source()
        with self.assertRaisesRegex(ControlError, 'character_reprepare_required'):
            inputs.read_bundle(self.c.runner, p['turn_id'], p['input_id'],
                               input_receipts=first['input_receipts'])
        again = self.c.runner.prepare(mode='compact')
        self.assertEqual(again['turn_id'], p['turn_id'])
        self.assertNotEqual(again['input_id'], p['input_id'])
        with self.assertRaisesRegex(ControlError, 'invalid_input_receipt'):
            inputs.read_bundle(self.c.runner, again['turn_id'], again['input_id'],
                               input_receipts=first['input_receipts'])
        fresh = inputs.read_bundle(self.c.runner, again['turn_id'], again['input_id'])
        with patch.object(self.c.runner, 'finish', return_value={'next': 'done'}):
            self.assertTrue(self.c.runner.commit(again['turn_id'], self.c.draft(),
                input_receipts=fresh['input_receipts'])['committed'])

    def test_cli_source_correction_advice_agrees_and_retry_commits(self):
        p = self.character.prepare()
        folder = Path(self.c.tmp.name)
        config = folder / 'config.json'; config.write_text(encode(self.c.cfg))
        decision = folder / 'decision.json'; decision.write_text(encode(self.c.draft()))
        self.character.correct_source()
        output = io.StringIO()
        with patch('decision_session.DecisionSession', return_value=self.c.runner), redirect_stdout(output):
            code = decision_session.main(['--config', str(config), '--wake-id', self.c.runner.wake,
                'commit', '--turn-id', p['turn_id'], '--decision', '@' + str(decision)])
        error = json.loads(output.getvalue())
        self.assertEqual(code, 1)
        self.assertEqual(error['reason'], 'character_reprepare_required')
        self.assertEqual(error['next'], 'prepare')
        self.assertTrue(error['same_wake'])
        self.assertEqual(error['recovery']['next'], 'prepare_and_review_changes')
        again = self.c.runner.prepare(mode='bundled')
        self.assertEqual(again['turn_id'], p['turn_id'])
        self.assertNotIn('character_brief', again['snapshot'])
        with patch.object(self.c.runner, 'finish', return_value={'next': 'done'}):
            self.assertTrue(self.c.runner.commit(again['turn_id'], self.c.draft())['committed'])


class GeneratorRetryTests(unittest.TestCase):
    def test_schema_correction_preserves_successful_generation(self):
        c = generator_tests.ReplyGeneratorTests(); c.setUp()
        self.addCleanup(c.tearDown)
        generated = c.generate()
        prepared = c.runner.state()
        draft = c.draft('안녕')
        draft['dm_decisions'][0]['generator_id'] = generated['generator_id']
        broken = copy.deepcopy(draft); del broken['advance_to_ms']
        with self.assertRaises(KeyError):
            c.runner.commit(c.prepared['turn_id'], broken)
        self.assertEqual(c.runner.last_recovery['next'], 'correct_and_retry_commit')
        self.assertEqual(c.runner.state()['reply_source'], prepared['reply_source'])
        with patch.object(c.runner, 'finish', return_value={'next': 'done'}):
            self.assertTrue(c.runner.commit(c.prepared['turn_id'], draft)['committed'])
        self.assertEqual(len(c.calls), 1)
