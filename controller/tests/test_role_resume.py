"""New-wake content reuse with fresh authority, source comparisons and review."""
import copy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from common import ControlError
import gate
from decision_session import DecisionSession
import model_adapter
import reply_generator
import role_executor
import role_resume
import test_role_executor as fixtures


class ResumeTests(unittest.TestCase):
    def setUp(self):
        self.r=fixtures.RoleExecutorTests('test_dm_routes_context_writer_review_and_commits_exact_text')
        self.r.setUp();self.addCleanup(self.r.doCleanups)
        self.c=self.r.c
        self.r.start();self.r.answer(self.r.context());self.r.answer({'parts':['안녕']})
        self.old=self.r.worker.load()
        self.old_generator=self.old['drafts']['0']['generator_id']

    def new_wake(self, change=None):
        if change:change()
        for name in ('wake','gate_state'):
            gate.write_json(self.c.state_dir/(name+'.json'),{'wake_id':'wake-2','woken_ms':self.c.clock,
                            'claim_deadline_ms':self.c.clock+1800000})
        gate.write_json(self.c.state_dir/'wake_ack.json',{'wake_id':'wake-2','phase':'started','at_ms':self.c.clock})
        runner=DecisionSession(self.c.cfg,'wake-2',lambda:self.c.clock,self.c.sleep)
        runner.transport=self.c.runner.transport
        self.r.worker=role_executor.RoleExecutor(self.c.cfg,runner,lambda:self.c.clock)
        self.r.worker.run()
        return self.r.worker

    def test_expired_old_wake_reuses_draft_under_new_generation_and_reviews_again(self):
        old_deadline=self.c.clock-1
        # Old claim expires; the old request is never made current again.
        gate.write_json(self.c.state_dir/'wake.json',{'wake_id':'wake-1','claim_deadline_ms':old_deadline})
        worker=self.new_wake()
        state=worker.load();self.assertEqual(state['phase'],'context')
        self.assertEqual(state['resume_candidate']['source_wake'],'wake-1')
        result=self.r.answer(self.r.context())
        self.assertEqual(result['role'],'review')  # No new writer/model request.
        draft=worker.load()['drafts']['0']
        self.assertEqual(draft['output']['parts'],['안녕'])
        self.assertNotEqual(draft['generator_id'],self.old_generator)
        record=gate.load_json(reply_generator.record_path(worker.session,draft['generator_id']),{})
        self.assertEqual(record['binding']['turn_id'],worker.session.state()['turn_id'])
        self.assertEqual(record['binding']['source_generation'],worker.session.state()['reply_source']['generation'])
        self.assertTrue(self.r.answer(self.r.review())['committed'])
        with self.c.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM dm_jobs').fetchone()[0],1)
        self.assertIn('writer_draft_reused',(worker.folder/'trace.jsonl').read_text())

    def test_old_decision_clocks_can_be_replanned_without_rewriting_same_content(self):
        self.c.sleep(10);self.c.recent()
        self.new_wake();out=self.r.context();out['decision']['next_decision_ms']+=60000
        self.assertEqual(self.r.answer(out)['role'],'review')

    def test_changed_direction_requires_new_writer(self):
        self.new_wake();out=self.r.context();out['reply_briefs'][0]['situation']='다른 내용을 설명한다.'
        self.assertEqual(self.r.answer(out)['role'],'writer')

    def test_changed_message_text_requires_new_writer(self):
        self.c.recent()
        path=self.c.state_dir/'recent_thread.json';value=json.loads(path.read_text())
        value['messages'][0]['text']='different actual message';path.write_text(json.dumps(value))
        self.new_wake()
        self.assertEqual(self.r.answer(self.r.context())['role'],'writer')

    def test_expired_activity_does_not_reuse_old_writer(self):
        files=list((self.r.worker.folder/'draft-recovery').glob('*.json'))
        for path in files:
            value=json.loads(path.read_text());value['expires_ms']=self.c.clock-1;path.write_text(json.dumps(value))
        self.new_wake()
        self.assertEqual(self.r.answer(self.r.context())['role'],'writer')

    def test_different_model_does_not_reuse_previous_model_output(self):
        self.c.cfg['model_adapters']['writer']['model']='new-writer'
        self.new_wake()
        self.assertEqual(self.r.answer(self.r.context())['role'],'writer')

    def test_corrupt_cache_falls_back_to_normal_writing(self):
        for path in (self.r.worker.folder/'draft-recovery').glob('*.json'):path.write_text('{bad')
        self.new_wake()
        self.assertEqual(self.r.answer(self.r.context())['role'],'writer')

    def test_old_wake_cannot_submit_after_new_claim(self):
        previous=self.r.worker;pending=self.old['pending'];self.new_wake()
        path=Path(self.c.tmp.name)/'late.json'
        path.write_text(json.dumps(model_adapter.result(pending['request'],{'input_read':True,**self.r.review()})))
        with self.assertRaisesRegex(ControlError,'stale_claim'):previous.complete(pending['id'],path)
        self.assertEqual(self.c.snapshot()['jobs'],[])

    def test_resume_does_not_copy_previous_reviews_or_receipts(self):
        old_record=self.r.worker.executor.load();worker=self.new_wake()
        self.assertNotEqual(worker.executor.load()['receipts'],old_record['receipts'])
        self.r.answer(self.r.context())
        state=worker.load()
        self.assertNotIn('conversation_review',state['decision']['dm_decisions'][0])
        self.assertEqual(state['phase'],'review')

    def test_cache_storage_failure_never_blocks_normal_writer_acceptance(self):
        self.new_wake();out=self.r.context();out['reply_briefs'][0]['situation']='새 방향'
        self.assertEqual(self.r.answer(out)['role'],'writer')
        with patch.object(role_resume,'remember',side_effect=OSError('cache disk unavailable')):
            result=self.r.answer({'parts':['새 답장']})
        self.assertEqual(result['role'],'review')
        self.assertIn('draft_cache_unavailable',(self.r.worker.folder/'trace.jsonl').read_text())

    def test_changed_writing_rules_prevent_old_draft_reuse(self):
        rules=Path(self.c.tmp.name)/'writing-rules.md';rules.write_text('새로 지정한 작성 기준')
        self.c.cfg['role_execution']['rules_file']=str(rules)
        self.new_wake()
        self.assertEqual(self.r.answer(self.r.context())['role'],'writer')

    def test_warning_approved_apply_is_not_offered_again_after_crash(self):
        # Simulate an apply followed by a crash before the role result was saved.
        previous=self.r.worker
        with patch.object(previous.session,'finish',side_effect=OSError('after apply')):
            self.assertTrue(self.r.answer(self.r.review())['committed'])
        saved=previous.executor.load();expanded=saved['expanded_decision']
        saved.pop('expanded_decision');saved['response']={'action':'approve_warning'}
        saved['warning']={'decision':expanded};previous.executor.save(saved)
        state=previous.load();state.pop('result',None);state['phase']='commit';previous.persist(state)
        worker=self.new_wake()
        self.assertIsNone(worker.load()['resume_candidate'])

    def test_same_review_feedback_does_not_reuse_known_rejected_draft(self):
        feedback={'verdict':'revise','feedback':[{'index':0,'reason':'답을 구체적으로 써야 함'}]}
        self.assertEqual(self.r.answer(feedback)['role'],'writer')
        self.r.answer({'parts':['두 번째 초안']})
        second=self.r.worker.load()['drafts']['0']['generator_id']
        self.assertEqual(self.r.answer(feedback)['role'],'writer')
        self.assertEqual(self.r.answer({'parts':['세 번째 초안']})['role'],'review')
        self.assertNotEqual(self.r.worker.load()['drafts']['0']['generator_id'],second)

    def test_review_rejected_draft_is_not_reused_in_a_new_wake(self):
        self.r.answer({'verdict':'revise','feedback':[{'index':0,'reason':'답변 방향을 다시 보라'}]})
        self.new_wake()
        self.assertEqual(self.r.answer(self.r.context())['role'],'writer')

    def test_rebind_state_write_crash_preserves_cache_identity_for_later_rejection(self):
        self.new_wake();original=self.r.worker.persist
        def crash(state):
            if state['phase']=='writer' and state.get('drafts'):
                raise OSError('crash before state journal')
            return original(state)
        with patch.object(self.r.worker,'persist',crash):
            with self.assertRaises(OSError):self.r.answer(self.r.context())
        self.assertEqual(self.r.worker.run()['role'],'review')
        draft=self.r.worker.load()['drafts']['0'];self.assertIn('recovery_key',draft)
        self.assertEqual(self.r.answer({'verdict':'revise','feedback':[{'index':0,'reason':'새로 작성'}]})['role'],'writer')
        self.assertFalse((self.r.worker.folder/'draft-recovery'/(draft['recovery_key']+'.json')).exists())


if __name__=='__main__':unittest.main()
