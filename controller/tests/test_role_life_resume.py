"""Passed life proposals require a new model's explicit current-state revalidation."""
import copy
import unittest

import gate
from decision_session import DecisionSession
import role_executor
import test_role_executor as fixtures


class LifeResumeTests(unittest.TestCase):
    def setUp(self):
        self.r=fixtures.RoleExecutorTests('test_dm_routes_context_writer_review_and_commits_exact_text')
        self.r.setUp();self.addCleanup(self.r.doCleanups)
        self.c=self.r.c;self.c.sleep(3601)
        self.assertEqual(self.r.start()['role'],'life')
        self.life={'advance_to_ms':self.c.clock,'next_decision_ms':self.c.clock+600000,
                   'activities':[{'id':'a1','status':'completed','actual_end_ms':self.c.clock,'reason':'휴식 종료'},
                                 self.c.activity(aid='next',start=self.c.clock,end=self.c.clock+600000)]}
        self.r.answer({'operations':self.life});self.r.answer(self.r.context())
        self.r.answer({'parts':['안녕']})

    def fresh(self):
        self.c.sleep(10);self.c.recent()
        for name in ('wake','gate_state'):
            gate.write_json(self.c.state_dir/(name+'.json'),{'wake_id':'wake-2','woken_ms':self.c.clock,
                            'claim_deadline_ms':self.c.clock+1800000})
        gate.write_json(self.c.state_dir/'wake_ack.json',{'wake_id':'wake-2','phase':'started','at_ms':self.c.clock})
        runner=DecisionSession(self.c.cfg,'wake-2',lambda:self.c.clock,self.c.sleep)
        runner.transport=self.c.runner.transport
        self.r.worker=role_executor.RoleExecutor(self.c.cfg,runner,lambda:self.c.clock)
        return self.r.worker.run()

    def approval(self):
        out=self.r.context();out['resume_life']=copy.deepcopy(self.life)
        out['resume_life'].update(advance_to_ms=self.c.clock,next_decision_ms=self.c.clock+600000)
        return out

    def test_new_context_can_revalidate_life_adjust_clocks_and_reuse_writer(self):
        self.assertEqual(self.fresh()['role'],'context')
        state=self.r.worker.load();self.assertTrue(state['life_revalidation_required'])
        result=self.r.answer(self.approval())
        self.assertEqual(result['role'],'review')
        self.assertTrue(self.r.answer(self.r.review())['committed'])
        self.assertEqual(self.c.snapshot()['activity']['id'],'next')

    def test_life_cannot_be_reused_without_explicit_confirmation(self):
        self.fresh();result=self.r.answer(self.r.context())
        self.assertEqual(result['role'],'context')
        self.assertEqual(self.r.worker.load()['feedback']['reason'],'role_resume_confirmation_required')

    def test_model_can_decline_and_run_life_again(self):
        self.fresh();result=self.r.answer({'resume_life':None})
        self.assertEqual(result['role'],'life')
        self.assertEqual(self.r.worker.load()['revisions'],0)

    def test_declined_candidate_cannot_override_new_life_result(self):
        self.fresh();self.r.answer({'resume_life':None})
        self.r.answer({'operations':self.life})
        result=self.r.answer(self.approval())
        self.assertEqual(result['role'],'context')
        self.assertEqual(self.r.worker.load()['feedback']['reason'],'role_resume_unavailable')

    def test_nonclock_changes_require_real_life_replanning(self):
        self.fresh();out=self.approval();out['resume_life']['activities'][1]['location']='elsewhere'
        result=self.r.answer(out)
        self.assertEqual(result['role'],'context')
        self.assertEqual(self.r.worker.load()['feedback']['reason'],'role_resume_life_changed')

    def test_changed_authoritative_revision_does_not_skip_life(self):
        self.c.ingest('other',direction='outgoing')
        self.assertEqual(self.fresh()['role'],'life')


if __name__=='__main__':unittest.main()
