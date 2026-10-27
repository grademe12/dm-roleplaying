"""Muse handoff integration with actual SQLite and fake confirmed transports only."""
import copy
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from common import ControlError
from dispatch import MockTransport
import conversation_flow
import decision_contract
import gate
import muse_executor
import test_decision_session as lifecycle


class MuseExecutorTests(unittest.TestCase):
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

    def start(self, **kwargs):
        self.ingest(**kwargs)
        self.recent()
        self.executor = muse_executor.MuseExecutor(self.runner)
        return self.executor.prepare()

    def envelope(self, packet, action='commit', **kwargs):
        return {'request_id': packet['request_id'], 'input_sha256': packet['input_sha256'],
                'input_read': True, 'action': action, **kwargs}

    def commit(self, packet, decision=None):
        return self.executor.respond(self.envelope(packet, decision=decision or self.draft()))

    def job_count(self):
        with self.store.connect() as db:
            return db.execute('SELECT count(*) FROM dm_jobs').fetchone()[0]

    def compact_start(self, **kwargs):
        self.ingest(**kwargs)
        self.recent()
        self.executor = muse_executor.MuseExecutor(self.runner)
        return self.executor.prepare(decision_format='compact-v1')

    def compact_draft(self, full=None):
        result = copy.deepcopy(full or self.draft())
        result.pop('decision_id')
        result.pop('expected_revision')
        for dm in result.get('dm_decisions', []):
            if dm.get('action') not in ('reply', 'initiate'):
                continue
            review = dm['conversation_review']
            context = review['context']
            dm['channel'] = context['channel']
            dm['conversation_review'] = {**{k: context[k] for k in ('previous_self', 'user_uptake', 'topic')},
                                         'memory': review['memory'], 'checks': review['checks']}
        return result

    def test_compact_reply_copies_exact_parts_and_selected_targets_then_replays_once(self):
        packet = self.compact_start()
        draft = self.compact_draft(self.draft('긴 답장 원문\n문단 둘째'))
        response = self.envelope(packet, decision=draft)
        self.assertTrue(self.executor.respond(response)['committed'])
        record = self.executor.load()
        expanded = record['expanded_decision']
        dm = expanded['dm_decisions'][0]
        self.assertEqual(dm['conversation_review']['draft_parts'], draft['dm_decisions'][0]['parts'])
        self.assertEqual(dm['conversation_review']['context']['target_message_ids'], ['m1'])
        self.assertEqual(dm['conversation_review']['checks'], draft['dm_decisions'][0]['conversation_review']['checks'])
        self.assertNotIn('decision_id', draft)
        self.assertEqual(expanded['expected_revision'], record['prepared']['snapshot']['revision'])
        self.assertEqual(self.executor.respond(response), record['result'])
        self.assertEqual(self.job_count(), 1)

    def test_compact_cannot_fill_failed_checks_missing_semantics_or_six_parts(self):
        packet = self.compact_start()
        for mutate, reason in (
            (lambda d: d['dm_decisions'][0]['conversation_review']['checks']['conflict'].update(passed=False),
             'conversation_review_failed'),
            (lambda d: d['dm_decisions'][0]['conversation_review'].pop('previous_self'), 'invalid_compact_review'),
            (lambda d: d['dm_decisions'][0].update(parts=['x'] * 6), 'parts_limit_exceeded'),
            (lambda d: d.update(expected_revision=-1), 'compact_managed_field'),
            (lambda d: d['dm_decisions'][0]['conversation_review'].update(draft_parts=['x']),
             'invalid_compact_review'),
        ):
            draft = self.compact_draft()
            mutate(draft)
            packet = self.executor.respond(self.envelope(packet, decision=draft))
            self.assertIn(reason, Path(packet['input_file']).read_text(encoding='utf-8'))
            self.assertEqual(self.job_count(), 0)
        self.assertTrue(self.executor.respond(self.envelope(packet, decision=self.compact_draft()))['committed'])

    def test_compact_does_not_choose_targets_or_consume_another_channel_context(self):
        packet = self.compact_start()
        draft = self.compact_draft()
        draft['dm_decisions'][0]['channel'] = 'slack'
        with self.assertRaisesRegex(ControlError, 'reply_context_not_ready'):
            self.executor.respond(self.envelope(packet, decision=draft))
        self.assertEqual(self.job_count(), 0)

    def test_compact_previous_self_is_model_selected_and_requires_delivered_evidence(self):
        packet = self.compact_start()
        record = self.executor.load()
        draft = self.compact_draft()
        review = draft['dm_decisions'][0]['conversation_review']
        review['previous_self'] = {'message_id': 'not-delivered', 'point': '모델이 고른 요점'}
        with self.assertRaisesRegex(ControlError, 'review_previous_self_not_in_context'):
            decision_contract.expand(record, draft)
        # More than 30 delivered IDs: metadata can narrow, originals stay intact.
        messages = [{'id': 'old-self', 'direction': 'outgoing', 'text': 'original'},
                    *[{'id': 'r' + str(i), 'text': 'original'} for i in range(35)]]
        record['inputs'].append({'kind': 'context', 'content': {'channel': 'instagram', 'messages': messages}})
        review['previous_self']['message_id'] = 'old-self'
        expanded = decision_contract.expand(record, draft)
        ctx = expanded['dm_decisions'][0]['conversation_review']['context']
        self.assertEqual(ctx['previous_self']['point'], '모델이 고른 요점')
        self.assertIn('old-self', ctx['recent_message_ids'])
        self.assertEqual(len(ctx['recent_message_ids']), 30)
        self.assertEqual(len(record['inputs'][-1]['content']['messages']), 36)

    def test_compact_non_incoming_change_still_requires_reprepare(self):
        packet = self.compact_start()
        draft = self.compact_draft()
        self.ingest('o2', direction='outgoing'); self.recent(('m1',), outgoing=('o2',))
        with self.assertRaisesRegex(ControlError, 'prepare_inputs_changed'):
            self.executor.respond(self.envelope(packet, decision=draft))
        self.assertEqual(self.job_count(), 0)

    def test_compact_warning_requires_approval_of_assembled_decision(self):
        conversation_flow.configure(self.store, 'main', {'mode': 'shadow'}, self.clock)
        packet = self.compact_start()
        packet = self.executor.respond(self.envelope(packet, decision=self.compact_draft()))
        self.assertEqual(self.job_count(), 0)
        expected = self.executor.load()['warning']['decision']
        self.assertTrue(self.executor.respond(self.envelope(packet, 'approve_warning', reason='검토함'))['committed'])
        with self.store.connect() as db:
            digest = db.execute('SELECT digest FROM decisions WHERE id=?', (expected['decision_id'],)).fetchone()[0]
        self.assertEqual(digest, muse_executor.digest(expected))

    def test_compact_recovers_exact_assembled_decision_after_session_metadata_failure(self):
        packet = self.compact_start()
        original = gate.write_json
        def crash(path, data):
            if Path(path) == self.runner.path and data.get('committed_turn'):
                raise OSError('injected')
            return original(path, data)
        with patch.object(gate, 'write_json', side_effect=crash):
            result = self.executor.respond(self.envelope(packet, decision=self.compact_draft()))
        self.assertTrue(result['committed'])
        expected = self.executor.load()['expanded_decision']
        self.assertTrue(self.executor.recover()['committed'])
        self.assertEqual(self.executor.load()['expanded_decision'], expected)
        self.assertEqual(self.job_count(), 1)

    def test_compact_schema_reads_verbatim_sections_and_detects_changes_before_commit(self):
        packet = self.compact_start()
        self.assertNotIn('API.md', self.executor.load()['documents'])
        packet = self.executor.respond(self.envelope(packet, 'read', requests=[
            {'kind': 'decision-schema', 'options': {'topics': ['activities']}}]))
        record = self.executor.load()
        original = Path.read_text
        api = original(muse_executor.ROOT / 'API.md', encoding='utf-8')
        self.assertIn(record['schema_sections']['activities'], api)
        self.assertNotIn('API.md', record['document_tokens'])
        self.assertIn('API.md', self.runner.state()['prepared_document_hashes'])
        def changed(path, *args, **kwargs):
            value = original(path, *args, **kwargs)
            return value + '\nchanged' if path.name == 'API.md' else value
        with patch.object(Path, 'read_text', changed):
            with self.assertRaisesRegex(ControlError, 'prepare_inputs_changed'):
                self.executor.respond(self.envelope(packet, decision=self.compact_draft()))
        self.assertEqual(self.job_count(), 0)

    def test_compact_initiate_preserves_semantics_and_rejects_unanswered_incoming(self):
        packet = self.compact_start()
        full = self.decision(dm_decisions=[{'action': 'initiate', 'channel': 'instagram',
            'group_id': 'proactive', 'parts': ['hello'], 'due_ms': self.clock, 'reason': 'self chosen'}])
        packet = self.executor.respond(self.envelope(packet, decision=self.compact_draft(full)))
        self.assertIn('unanswered_incoming_exists', Path(packet['input_file']).read_text(encoding='utf-8'))
        self.assertEqual(self.job_count(), 0)

    def test_pure_life_without_dm_keeps_only_life_schema(self):
        self.sleep(3601)
        self.executor=muse_executor.MuseExecutor(self.runner)
        self.executor.prepare(decision_format='compact-v1')
        self.assertEqual(set(self.executor.load()['schema_sections']), {'activities', 'facts-plans'})

    def test_compact_slack_target_selection_does_not_answer_other_incoming(self):
        self.ingest('s1', observation={'identity': 'platform', 'channel': 'slack'})
        self.ingest('s2', observation={'identity': 'platform', 'channel': 'slack'})
        self.recent(('s1', 's2'), channel='slack')
        self.executor = muse_executor.MuseExecutor(self.runner)
        packet = self.executor.prepare(decision_format='compact-v1')
        full = self.decision(dm_decisions=[{'action': 'reply', 'channel': 'slack', 'message_ids': ['s2'],
            'group_id': 'slack-one', 'parts': ['선택한 메시지에 답장'], 'due_ms': self.clock, 'reason': 'answer s2'}],
            processed_event_ids=['dm:s2'])
        self.assertTrue(self.executor.respond(self.envelope(packet, decision=self.compact_draft(full)))['committed'])
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT status FROM dm_messages WHERE id=?', ('s1',)).fetchone()[0], 'received')
            targets = [r[0] for r in db.execute('SELECT message_id FROM reply_targets WHERE group_id=?', ('slack-one',))]
        self.assertEqual(targets, ['s2'])

    def test_schema_topic_catalog_is_verbatim_and_cannot_read_arbitrary_files(self):
        all_sections, _ = decision_contract.sections(muse_executor.ROOT, list(decision_contract.API_TOPICS))
        source = (muse_executor.ROOT / 'API.md').read_text(encoding='utf-8')
        for key, section in all_sections.items():
            self.assertTrue(section.startswith(decision_contract.API_TOPICS[key]))
            self.assertIn(section, source)
        for topics in ([], ['../../secret'], ['activities', 'activities'], 'activities'):
            with self.assertRaisesRegex(ControlError, 'invalid_decision_schema_topics'):
                decision_contract.sections(muse_executor.ROOT, topics)

    def test_compact_crash_before_assembly_persistence_recovers_without_new_model_response(self):
        packet = self.compact_start()
        original = self.executor.save
        def crash(record):
            if record.get('expanded_decision'):
                raise OSError('before assembled intent persistence')
            return original(record)
        with patch.object(self.executor, 'save', side_effect=crash):
            with self.assertRaises(OSError):
                self.executor.respond(self.envelope(packet, decision=self.compact_draft()))
        self.assertEqual(self.job_count(), 0)
        self.assertNotIn('expanded_decision', self.executor.load())
        self.assertTrue(self.executor.recover()['committed'])
        self.assertEqual(self.job_count(), 1)

    def test_single_muse_decision_uses_original_inputs_and_confirms_once(self):
        packet = self.start(text='테스트 원문을 그대로 보존해 줘')
        text = Path(packet['input_file']).read_text(encoding='utf-8')
        self.assertIn('테스트 원문을 그대로 보존해 줘', text)
        record = self.executor.load()
        for name, original in record['documents'].items():
            self.assertEqual(original, (muse_executor.ROOT / name).read_text(encoding='utf-8'))
            self.assertIn(original, text)
        self.assertIn('END_MUSE_INPUT ' + packet['request_id'], text)
        self.assertEqual(self.snapshot()['jobs'], [])
        response = self.envelope(packet, decision=self.draft())
        result = self.executor.respond(response)
        self.assertTrue(result['committed'])
        self.assertEqual(self.executor.respond(response), result)
        with self.store.connect() as db:
            jobs = list(db.execute('SELECT status FROM dm_jobs'))
        self.assertEqual([row[0] for row in jobs], ['confirmed'])
        self.assertEqual(self.runner.state()['turn_count'], 1)

    def test_delivery_ack_request_hash_and_packet_integrity_are_required(self):
        packet = self.start()
        response = self.envelope(packet, decision=self.draft())
        for changes, reason in (({'input_read': False}, 'executor_input_not_read'),
                                ({'request_id': 'other'}, 'executor_input_mismatch'),
                                ({'input_sha256': 'other'}, 'executor_input_mismatch')):
            with self.assertRaisesRegex(ControlError, reason):
                self.executor.respond({**response, **changes})
        Path(packet['input_file']).write_text('truncated', encoding='utf-8')
        with self.assertRaisesRegex(ControlError, 'executor_input_modified'):
            self.executor.respond(response)
        self.assertEqual(self.snapshot()['jobs'], [])

    def test_revision_and_new_context_cannot_be_patched_by_executor(self):
        packet = self.start()
        decision = self.draft()
        self.ingest('o2', direction='outgoing')
        self.recent(('m1',), outgoing=('o2',))
        with self.assertRaisesRegex(ControlError, 'prepare_inputs_changed'):
            self.commit(packet, decision)
        self.assertEqual(self.snapshot()['jobs'], [])
        new = self.executor.prepare()
        self.assertIn('o2', Path(new['input_file']).read_text(encoding='utf-8'))
        with self.assertRaisesRegex(ControlError, 'executor_input_mismatch'):
            self.commit(packet, decision)

    def test_context_only_outgoing_echo_still_prevents_apply(self):
        packet = self.start()
        self.recent(('m1',), outgoing=('echo-not-ingested',))
        with self.assertRaisesRegex(ControlError, 'prepare_inputs_changed'):
            self.commit(packet)
        self.assertEqual(self.snapshot()['jobs'], [])

    def test_additional_reads_return_full_original_document_and_memory(self):
        packet = self.start()
        new = self.executor.respond(self.envelope(packet, 'read', requests=[
            {'kind': 'document', 'options': {'name': 'design/biographical-continuity.md'}},
            {'kind': 'recall', 'options': {'keywords': ['topic_state:book'], 'limit': 1}},
            {'kind': 'conversation-context', 'options': {'limit': 10}},
        ]))
        text = Path(new['input_file']).read_text(encoding='utf-8')
        self.assertIn((muse_executor.ROOT / 'design/biographical-continuity.md').read_text(encoding='utf-8'), text)
        self.assertIn('conversation-context', text)
        self.assertTrue(any(item['kind'] == 'recall' for item in self.executor.load()['inputs']))
        self.assertTrue(self.commit(new)['committed'])

    def test_reads_do_not_expose_shell_or_outside_files(self):
        packet = self.start()
        for request in ({'kind': 'shell', 'options': {'command': 'anything'}},
                        {'kind': 'document', 'options': {'name': '../../outside.md'}}):
            with self.assertRaises(ControlError):
                self.executor.respond(self.envelope(packet, 'read', requests=[request]))
        self.assertEqual(self.snapshot()['jobs'], [])

    def test_validation_error_returns_feedback_and_preserves_review_requirement(self):
        packet = self.start()
        decision = self.draft()
        del decision['dm_decisions'][0]['conversation_review']
        decision['dm_decisions'][0]['channel'] = 'instagram'
        feedback = self.commit(packet, decision)
        self.assertEqual(feedback['next'], 'muse')
        self.assertIn('missing_conversation_review', Path(feedback['input_file']).read_text(encoding='utf-8'))
        self.assertEqual(self.snapshot()['jobs'], [])
        self.assertTrue(self.commit(feedback)['committed'])

    def test_shadow_warning_waits_for_explicit_muse_approval(self):
        conversation_flow.configure(self.store, 'main', {'mode': 'shadow'}, self.clock)
        packet = self.start()
        feedback = self.commit(packet)
        self.assertEqual(feedback['next'], 'muse')
        self.assertEqual(self.snapshot()['jobs'], [])
        with self.assertRaisesRegex(ControlError, 'executor_warning_review_required'):
            self.executor.respond(self.envelope(feedback, 'approve_warning', reason=''))
        approved = self.executor.respond(self.envelope(feedback, 'approve_warning',
            reason='Reviewed the missing-plan shadow warning for this exact answer.'))
        self.assertTrue(approved['committed'])

    def test_missing_decision_key_can_be_corrected_by_muse(self):
        packet = self.start()
        decision = self.draft()
        del decision['decision_id']
        feedback = self.commit(packet, decision)
        self.assertEqual(feedback['next'], 'muse')
        self.assertIn('invalid_decision_document', Path(feedback['input_file']).read_text(encoding='utf-8'))
        self.assertEqual(self.job_count(), 0)
        self.assertTrue(self.commit(feedback)['committed'])

    def test_stale_owner_cannot_submit_or_ack_another_wake(self):
        packet = self.start()
        gate.write_json(self.state_dir / 'wake.json', {'wake_id': 'wake-2'})
        with self.assertRaisesRegex(ControlError, 'stale_claim'):
            self.commit(packet)
        self.assertEqual(self.snapshot()['jobs'], [])
        self.assertEqual(gate.load_json(self.state_dir / 'wake_ack.json', {})['phase'], 'started')

    def test_timeout_between_preparation_and_response_preserves_claim_deadline(self):
        packet = self.start()
        gate.write_json(self.state_dir / 'wake.json',
                        {'wake_id': 'wake-1', 'claim_deadline_ms': self.clock + 1000})
        self.sleep(2)
        with self.assertRaisesRegex(ControlError, 'stale_claim'):
            self.commit(packet)
        self.assertEqual(self.snapshot()['jobs'], [])

    def test_interrupted_after_apply_recovers_without_new_decision_or_duplicate_send(self):
        packet = self.start()
        original = self.executor.save
        def crash(record):
            if record['stage'] == 'completed':
                raise OSError('injected final receipt write failure')
            original(record)
        with patch.object(self.executor, 'save', side_effect=crash):
            with self.assertRaises(OSError):
                self.commit(packet)
        self.assertEqual(self.executor.load()['stage'], 'committing')
        with patch.object(self.runner, 'commit', side_effect=AssertionError('must not redecide')):
            result = self.executor.recover()
        self.assertTrue(result['committed'])
        self.assertEqual(self.job_count(), 1)
        self.assertEqual(self.runner.state()['turn_count'], 1)

    def test_session_metadata_failure_replays_same_digest(self):
        packet = self.start()
        original = gate.write_json
        def crash(path, data):
            if Path(path) == self.runner.path and data.get('committed_turn'):
                raise OSError('injected session write failure')
            original(path, data)
        with patch.object(gate, 'write_json', side_effect=crash):
            result = self.commit(packet)
        self.assertTrue(result['committed'])
        self.assertEqual(result['after']['next'], 'retry_same_commit')
        with self.assertRaisesRegex(ControlError, 'executor_finish_required'):
            self.executor.prepare()
        recovered = self.executor.recover()
        self.assertTrue(recovered['committed'])
        self.assertEqual(self.runner.state()['turn_count'], 1)
        self.assertEqual(self.job_count(), 1)

    def test_committed_transport_error_does_not_ask_muse_for_new_answer(self):
        packet = self.start()
        with patch.object(self.runner, 'finish', side_effect=OSError('transport down')):
            result = self.commit(packet)
        self.assertTrue(result['committed'])
        self.assertEqual(result['after']['next'], 'finish')
        self.assertEqual(self.executor.recover()['after']['next'], 'prepare')
        self.assertEqual(self.job_count(), 1)

    def test_different_response_for_completed_request_is_rejected(self):
        packet = self.start()
        response = self.envelope(packet, decision=self.draft())
        self.executor.respond(response)
        changed = copy.deepcopy(response)
        changed['decision']['dm_decisions'][0]['parts'] = ['changed']
        with self.assertRaisesRegex(ControlError, 'executor_response_collision'):
            self.executor.respond(changed)

    def test_document_edit_after_packet_creation_still_blocks_commit(self):
        packet = self.start()
        original = Path.read_text
        def modified(path, *args, **kwargs):
            text = original(path, *args, **kwargs)
            return text + '\nchanged' if path.name == 'reply-context.md' else text
        with patch.object(Path, 'read_text', modified):
            with self.assertRaisesRegex(ControlError, 'prepare_inputs_changed'):
                self.commit(packet)
        self.assertEqual(self.snapshot()['jobs'], [])

    def test_next_turn_uses_new_receipts_and_full_original_documents(self):
        packet = self.start()
        first = self.commit(packet)
        self.assertEqual(first['after']['next'], 'prepare')
        old_receipts = set(self.executor.load()['receipts'])
        self.ingest('m2'); self.recent(('m1', 'm2'))
        next_packet = self.executor.prepare()
        record = self.executor.load()
        self.assertNotEqual(packet['request_id'], next_packet['request_id'])
        self.assertFalse(old_receipts & set(record['receipts']))
        self.assertIn('CORE_RULES.md', record['documents'])
        self.assertIn('m2', Path(next_packet['input_file']).read_text(encoding='utf-8'))

    def test_default_packet_omits_full_api_and_unrelated_fixed_documents(self):
        packet = self.start()
        names = set(self.executor.load()['documents'])
        self.assertNotIn('API.md', names)
        self.assertNotIn('CRON_RUNBOOK.md', names)
        self.assertNotIn('design/contact-rhythm.md', names)
        self.assertIn('CORE_RULES.md', names)
        self.assertIn('design/character/profile.md', names)
        self.assertIn('design/reply-context.md', names)
        self.assertIn('design/conversation-rules.md', names)
        self.assertIn('runbooks/memory-continuity.md', names)
        self.assertLess(Path(packet['input_file']).stat().st_size, 120000)

    def test_additional_read_can_deliver_only_delta_with_explicit_context_retention(self):
        packet = self.start(text='unique_prior_message_marker')
        response = self.envelope(packet, 'read', requests=[
            {'kind': 'recall', 'options': {'keywords': ['topic_state:book'], 'limit': 1}}])
        response['retain_context'] = True
        delta = self.executor.respond(response)
        text = Path(delta['input_file']).read_text(encoding='utf-8')
        self.assertIn(packet['request_id'], text)
        self.assertNotIn('unique_prior_message_marker', text)
        self.assertNotIn('## 원문: CORE_RULES.md', text)
        self.assertLess(len(text.encode('utf-8')), Path(packet['input_file']).stat().st_size / 2)
        self.assertTrue(self.commit(delta)['committed'])

    def test_only_acknowledged_documents_can_be_reused_on_explicit_next_prepare(self):
        packet = self.start()
        # Merely creating a packet does not cache documents as read.
        unacknowledged = self.executor.prepare(retain_context=True)
        self.assertIn('CORE_RULES.md', self.executor.load()['documents'])
        self.commit(unacknowledged)
        self.ingest('m2'); self.recent(('m1', 'm2'))
        next_packet = self.executor.prepare(retain_context=True)
        record = self.executor.load()
        self.assertEqual(record['documents'], {})
        self.assertIn('CORE_RULES.md', record['retained_documents'])
        self.assertIn('design/reply-context.md', record['retained_documents'])
        self.assertLess(Path(next_packet['input_file']).stat().st_size,
                        Path(unacknowledged['input_file']).stat().st_size / 2)

    def test_optional_api_document_is_requested_once_then_reused_without_summary(self):
        packet = self.start()
        api = self.executor.respond(self.envelope(packet, 'read', requests=[
            {'kind': 'document', 'options': {'name': 'API.md'}}]))
        original = (muse_executor.ROOT / 'API.md').read_text(encoding='utf-8')
        self.assertIn(original, Path(api['input_file']).read_text(encoding='utf-8'))
        self.commit(api)
        self.ingest('m2'); self.recent(('m1', 'm2'))
        next_packet = self.executor.prepare(retain_context=True)
        self.assertIn('API.md', self.executor.load()['retained_documents'])
        self.assertNotIn(original, Path(next_packet['input_file']).read_text(encoding='utf-8'))

    def test_context_loss_default_prepare_resends_previously_read_originals(self):
        packet = self.start()
        self.commit(packet)
        self.ingest('m2'); self.recent(('m1', 'm2'))
        fresh = self.executor.prepare()
        self.assertEqual(self.executor.load()['retained_documents'], {})
        self.assertIn('## 원문: CORE_RULES.md', Path(fresh['input_file']).read_text(encoding='utf-8'))

    def test_modified_optional_receipt_is_rejected_even_when_base_receipts_are_valid(self):
        packet = self.start()
        self.commit(packet)
        cache_path = self.executor.folder / 'documents.json'
        cached = json.loads(cache_path.read_text(encoding='utf-8'))
        cached['design/reply-context.md']['receipt'] = 'forged'
        muse_executor.private_write(cache_path, json.dumps(cached))
        with self.assertRaisesRegex(ControlError, 'invalid_document_receipt'):
            self.executor.prepare(retain_context=True)

    def test_changed_retained_document_is_redelivered_in_full(self):
        packet = self.start()
        self.executor.respond(self.envelope(packet, 'read', requests=[
            {'kind': 'recall', 'options': {'keywords': ['topic_state:book']}}]))
        original = Path.read_text
        def modified(path, *args, **kwargs):
            text = original(path, *args, **kwargs)
            return text + '\nnew policy marker' if path.name == 'reply-context.md' else text
        with patch.object(Path, 'read_text', modified):
            new = self.executor.prepare(retain_context=True)
        self.assertIn('new policy marker', Path(new['input_file']).read_text(encoding='utf-8'))
        self.assertIn('design/reply-context.md', self.executor.load()['documents'])

    def test_old_packet_is_reprepared_instead_of_reinterpreted(self):
        packet = self.start()
        record = self.executor.load()
        del record['packet_version']
        self.executor.save(record)
        with self.assertRaisesRegex(ControlError, 'executor_prepare_required'):
            self.commit(packet)
        new = self.executor.prepare()
        self.assertEqual(self.executor.load()['packet_version'], 2)
        self.assertNotEqual(new['request_id'], packet['request_id'])

    def test_io_before_commit_recovers_saved_response_without_muse(self):
        packet = self.start()
        with patch.object(self.runner, 'commit', side_effect=OSError('injected failure before commit')):
            with self.assertRaises(OSError):
                self.commit(packet)
        self.assertEqual(self.job_count(), 0)
        with self.assertRaisesRegex(ControlError, 'executor_recover_required'):
            self.executor.prepare()
        self.assertTrue(self.executor.recover()['committed'])
        self.assertEqual(self.job_count(), 1)

    def test_cache_io_failure_before_commit_recovers_to_same_response_submission(self):
        packet = self.start()
        original = muse_executor.private_write
        def unavailable(path, text):
            if Path(path).name == 'documents.json':
                raise OSError('injected cache write failure')
            return original(path, text)
        with patch.object(muse_executor, 'private_write', side_effect=unavailable):
            with self.assertRaises(OSError):
                self.commit(packet)
        self.assertEqual(self.job_count(), 0)
        self.assertEqual(self.executor.recover()['next'], 'respond')
        self.assertTrue(self.commit(packet)['committed'])
        self.assertEqual(self.job_count(), 1)

    def test_life_boundaries_and_actual_wait_policy_remain_authoritative(self):
        self.sleep(3601)
        packet = self.start()
        record = self.executor.load()
        self.assertIn('activity_due', record['prepared']['snapshot']['work_manifest']['life_required'])
        self.assertIn('runbooks/life-boundaries.md', record['documents'])
        self.assertIn('runbooks/activity-selection.md', record['documents'])
        # Passing life_required through does not automatically finish the activity.
        self.assertEqual(self.snapshot()['activity']['id'], 'a1')

    def test_cli_prepare_is_single_json_and_resolves_config_paths(self):
        self.ingest(); self.recent()
        config = Path(self.tmp.name) / 'automation.json'
        config.write_text(json.dumps({**self.cfg, 'db': 'test.db', 'state_dir': 'automation-state'}))
        result = subprocess.run([sys.executable, str(muse_executor.ROOT / 'muse_executor.py'),
            '--config', str(config), '--wake-id', 'wake-1', 'prepare'],
            capture_output=True, text=True, timeout=20, cwd='/tmp')
        self.assertEqual(result.returncode, 0, result.stderr)
        packet = json.loads(result.stdout)
        self.assertEqual(packet['next'], 'muse')
        self.assertTrue(Path(packet['input_file']).is_file())
        self.assertEqual(Path(packet['input_file']).stat().st_mode & 0o777, 0o600)

    def test_no_claim_does_not_prepare_or_wake_muse(self):
        config = Path(self.tmp.name) / 'automation.json'
        config.write_text(json.dumps(self.cfg))
        with patch.object(gate, 'claim_work', return_value=(10, None)), \
                patch.object(muse_executor.MuseExecutor, 'prepare', side_effect=AssertionError('no work')), \
                redirect_stdout(io.StringIO()) as output:
            code = muse_executor.main(['--config', str(config), 'prepare'])
        self.assertEqual(code, 10)
        self.assertEqual(json.loads(output.getvalue())['next'], 'done')


if __name__ == '__main__':
    unittest.main()
