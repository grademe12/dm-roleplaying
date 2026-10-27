"""Pre-export checks and one-owner enforcement, with no network/model calls."""
import copy
import io
import json
from contextlib import redirect_stdout
from pathlib import Path
import unittest
from unittest.mock import patch

from common import ControlError
import driver_ownership
import gate
import model_adapter
import muse_executor
import role_executor
import role_preflight
import role_response
import test_role_executor as fixtures


class PreflightTests(unittest.TestCase):
    def setUp(self):
        self.r=fixtures.RoleExecutorTests('test_dm_routes_context_writer_review_and_commits_exact_text')
        self.r.setUp();self.addCleanup(self.r.doCleanups)
        self.c=self.r.c;self.r.start()

    def check(self, output, mutate=None):
        pending=self.r.worker.load()['pending']
        value=model_adapter.result(pending['request'], {'input_read':True,**output})
        if mutate:mutate(value)
        return role_preflight.check(pending['request'],value,self.c.clock)

    def test_writer_envelope_and_read_markers_rejected_before_export(self):
        self.r.answer(self.r.context())
        self.assertEqual(self.check({'parts':['hello']}, lambda v:v.update(schema='wrong'))['reason'],'model_output_invalid')
        self.assertEqual(self.check({'parts':['hello']}, lambda v:v['output'].pop('input_read'))['reason'],'role_input_not_read')
        self.assertEqual(self.c.snapshot()['jobs'],[])

    def test_context_document_must_be_present_not_merely_available(self):
        out=self.r.context();out['reply_briefs'][0]['documents']=['design/hobby-conversation.md']
        result=self.check(out)
        self.assertFalse(result['ok']);self.assertEqual(result['repair_kind'],'source')
        self.assertEqual(result['reason'],'role_writer_document_not_read')

    def test_life_context_scalar_collision_uses_existing_merge_rule(self):
        pending=self.r.worker.load()['pending']
        req=copy.deepcopy(pending['request']);req['input']['life_proposal']={'advance_to_ms':self.c.clock-1}
        value=model_adapter.result(req, {'input_read':True,**self.r.context()})
        self.assertEqual(role_preflight.check(req,value,self.c.clock)['reason'],'role_operation_collision')
        # Different proposed review times are merged by min, not rejected just for sharing a field.
        req['input']['life_proposal']={'next_decision_ms':self.c.clock+1000}
        self.assertTrue(role_preflight.check(req,value,self.c.clock)['ok'])

    def test_past_next_time_is_not_rejected_at_preflight(self):
        # Store.apply re-bases a stale next_decision_ms on the commit clock,
        # so preflight no longer rejects it or routes it back to context.
        out=self.r.context();out['decision']['next_decision_ms']=self.c.clock
        self.assertTrue(self.check(out)['ok'])

    def test_read_only_validator_does_not_consume_or_refresh(self):
        pending=self.r.worker.load()['pending'];folder=Path(self.c.tmp.name)
        request=folder/'request.json';response=folder/'response.json'
        request.write_text(json.dumps(pending['request']))
        response.write_text(json.dumps(model_adapter.result(pending['request'], {'input_read':True,**self.r.context()})))
        before=self.r.worker.path.read_bytes();ack=(self.c.state_dir/'wake_ack.json').read_bytes()
        _,_,result=role_response.validate(request,response,now=self.c.clock)
        self.assertTrue(result['ok']);self.assertEqual(self.r.worker.path.read_bytes(),before)
        self.assertEqual((self.c.state_dir/'wake_ack.json').read_bytes(),ack)

    def test_input_read_does_not_inherit_the_model_response_size_limit(self):
        pending=self.r.worker.load()['pending'];folder=Path(self.c.tmp.name)
        request=copy.deepcopy(pending['request'])
        request['input']['additional_source']='x'*(model_adapter.MAX_RESPONSE_BYTES+1)
        request_path=folder/'large-request.json';request_path.write_text(json.dumps(request))
        response_path=folder/'response.json'
        response_path.write_text(json.dumps(model_adapter.result(request,{'input_read':True,**self.r.context()})))
        _,_,result=role_response.validate(request_path,response_path,now=self.c.clock)
        self.assertTrue(result['ok'])

    def test_submit_only_stages_and_can_be_used_while_driver_owns_execution(self):
        self.c.cfg['decision_driver']={'enabled':True,'mode':'roles'}
        pending=self.r.worker.load()['pending'];folder=Path(self.c.tmp.name)
        config=folder/'automation.json';config.write_text(json.dumps(self.c.cfg))
        response=folder/'response.json'
        response.write_text(json.dumps(model_adapter.result(pending['request'],{'input_read':True,**self.r.context()})))
        request=self.r.worker.folder/('role-model-input-'+pending['id']+'.json')
        before=self.r.worker.path.read_bytes()
        result=role_response.submit(config,request,response,now=self.c.clock)
        self.assertEqual(result['next'],'driver');self.assertFalse(result['committed'])
        self.assertEqual(self.r.worker.path.read_bytes(),before)
        self.assertEqual(self.c.snapshot()['jobs'],[])
        # Same response is idempotent, while a competing different response cannot replace it.
        self.assertTrue(role_response.submit(config,request,response,now=self.c.clock)['ok'])
        changed=json.loads(response.read_text());changed['output']['decision']['next_decision_ms']+=1
        response.write_text(json.dumps(changed))
        with self.assertRaisesRegex(ControlError,'role_response_changed'):
            role_response.submit(config,request,response,now=self.c.clock)

    def test_format_repairs_do_not_use_semantic_revision_budget(self):
        self.r.worker.opts['max_revisions']=1;self.r.worker.opts['max_format_repairs']=3
        self.r.answer(self.r.context())
        generation=self.c.runner.state()['reply_source']['generation']
        for i in range(3):
            self.assertEqual(self.r.answer({'parts':[]})['role'],'writer')
            state=self.r.worker.load()
            self.assertEqual(state['revisions'],0);self.assertEqual(state['format_repairs'],i+1)
            self.assertEqual(self.c.runner.state()['reply_source']['generation'],generation)
        final=self.r.answer({'parts':[]})
        self.assertFalse(final['ok']);self.assertEqual(final['reason'],'role_format_budget')
        self.assertIn('role_abandoned',(self.r.worker.folder/'trace.jsonl').read_text())

    def test_real_rewrite_still_uses_semantic_budget(self):
        self.r.answer(self.r.context());self.r.answer({'parts':['안녕']})
        self.r.answer({'verdict':'revise','feedback':[{'index':0,'reason':'답을 더 구체적으로 써야 함'}]})
        state=self.r.worker.load()
        self.assertEqual(state['revisions'],1);self.assertEqual(state['format_repairs'],0)

    def test_failed_semantic_check_is_not_classified_as_format(self):
        self.r.answer(self.r.context());self.r.answer({'parts':['안녕']})
        output=self.r.review();output['reviews'][0]['conversation_review']['checks']['conflict']['passed']=False
        result=self.check(output)
        self.assertEqual(result['reason'],'conversation_review_failed')
        self.assertEqual(result['repair_kind'],'semantic')

    def test_duplicate_revision_indexes_rejected_without_mutating_drafts(self):
        self.r.answer(self.r.context());self.r.answer({'parts':['안녕']})
        output={'verdict':'revise','feedback':[{'index':0,'reason':'same'},{'index':0,'reason':'same'}]}
        self.assertEqual(self.check(output)['reason'],'role_invalid_revision')
        self.assertEqual(self.r.answer(output)['role'],'review')
        self.assertIn('0',self.r.worker.load()['drafts'])
        self.assertEqual(self.r.worker.load()['revisions'],0)

    def test_old_cli_claim_is_rejected_without_consuming_wake(self):
        self.c.cfg['decision_driver']={'enabled':True,'mode':'roles'}
        config=Path(self.c.tmp.name)/'config.json';config.write_text(json.dumps(self.c.cfg))
        before=(self.c.state_dir/'wake.json').read_bytes();out=io.StringIO()
        with redirect_stdout(out):code=gate.main(['--config',str(config),'--claim'])
        self.assertEqual(code,2);self.assertEqual(json.loads(out.getvalue())['reason'],'decision_driver_owned')
        self.assertEqual((self.c.state_dir/'wake.json').read_bytes(),before)

    def test_submit_rejects_response_after_wake_replaced(self):
        pending=self.r.worker.load()['pending'];folder=Path(self.c.tmp.name)
        config=folder/'config.json';config.write_text(json.dumps(self.c.cfg))
        response=folder/'out.json';response.write_text(json.dumps(model_adapter.result(pending['request'],
                         {'input_read':True,**self.r.context()})))
        request=self.r.worker.folder/('role-model-input-'+pending['id']+'.json')
        gate.write_json(self.c.state_dir/'wake.json',{'wake_id':'other'})
        with self.assertRaisesRegex(ControlError,'stale_claim'):
            role_response.submit(config,request,response,now=self.c.clock)
        self.assertFalse((self.r.worker.folder/('role-model-response-'+pending['id']+'.json')).exists())

    def test_accepted_response_replay_is_acknowledged_without_reexecuting_or_renewing(self):
        pending=self.r.worker.load()['pending'];folder=Path(self.c.tmp.name)
        config=folder/'config.json';config.write_text(json.dumps(self.c.cfg))
        request=folder/'request-copy.json';request.write_text(json.dumps(pending['request']))
        response=folder/'response.json';response.write_text(json.dumps(model_adapter.result(pending['request'],
                          {'input_read':True,**self.r.context()})))
        self.assertTrue(role_response.submit(config,request,response,now=self.c.clock)['ok'])
        self.r.worker.run();before=self.r.worker.path.read_bytes()
        ack=(self.c.state_dir/'wake_ack.json').read_bytes()
        result=role_response.submit(config,request,response,now=self.c.clock+2000000)
        self.assertEqual(result['reason'],'already_accepted');self.assertTrue(result['replayed'])
        self.assertEqual(self.r.worker.path.read_bytes(),before)
        self.assertEqual((self.c.state_dir/'wake_ack.json').read_bytes(),ack)

    def test_malformed_json_is_a_format_retry_not_session_death(self):
        pending=self.r.worker.load()['pending']
        path=self.r.worker.folder/('role-model-response-'+pending['id']+'.json')
        path.write_text('{bad json')
        result=self.r.worker.run()
        self.assertEqual(result['role'],'context');self.assertEqual(self.r.worker.load()['format_repairs'],1)

    def test_driver_mode_rejects_manual_claim_and_executor_calls(self):
        self.c.cfg['decision_driver']={'enabled':True,'mode':'roles'}
        with self.assertRaisesRegex(ControlError,'decision_driver_owned'):gate.claim_work(self.c.cfg)
        with self.assertRaisesRegex(ControlError,'decision_driver_owned'):self.r.worker.run()
        with self.assertRaisesRegex(ControlError,'decision_driver_owned'):role_executor.entry(self.c.cfg)
        with self.assertRaisesRegex(ControlError,'decision_driver_owned'):self.r.worker.executor.prepare()
        with self.assertRaisesRegex(ControlError,'decision_driver_owned'):self.r.worker.executor.recover()
        with self.assertRaisesRegex(ControlError,'decision_driver_owned'):
            with self.c.runner.owned():pass
        # The service driver can operate; matching an arbitrary wake string alone is not ownership.
        with driver_ownership.owned(self.c.cfg):
            self.assertEqual(self.r.worker.run()['role'],'context')


if __name__=='__main__':unittest.main()
