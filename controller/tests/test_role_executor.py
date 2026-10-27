"""Role routing over real SQLite/claims/receipts and fake model transports only."""
import copy
import io
import json
from contextlib import redirect_stdout
from pathlib import Path
import sqlite3
import sys
import unittest
from unittest.mock import patch

from common import ControlError,encode
import gate
import model_adapter
import muse_executor
import reply_generator
import role_contract as contract
import role_executor as roles
import test_muse_executor as fixtures


class RoleExecutorTests(unittest.TestCase):
    def setUp(self):
        self.c=fixtures.MuseExecutorTests('test_next_turn_uses_new_receipts_and_full_original_documents')
        self.c.setUp();self.addCleanup(self.c.tearDown)
        self.c.cfg['role_execution']={'enabled':True,'adapters':{r:r for r in contract.ROLES}}
        self.c.cfg['model_adapters']={r:{'backend':'file','model':'current-writer' if r=='writer' else 'external-'+r} for r in contract.ROLES}
        self.worker=roles.RoleExecutor(self.c.cfg,self.c.runner,lambda:self.c.clock)
        self.serial=0

    def start(self):
        self.c.ingest();self.c.recent();return self.worker.run()

    def context(self,decision=None):
        d=copy.deepcopy(decision or self.c.draft())
        d.pop('decision_id');d.pop('expected_revision')
        briefs=[]
        for i,dm in enumerate(d.get('dm_decisions',[])):
            if dm.get('action') in ('reply','initiate'):
                dm['channel']=dm.get('channel','instagram');dm.pop('parts');dm.pop('conversation_review')
                briefs.append({'index':i,'situation':'집에서 쉬며 사용자에게 인사한다.',
                               'memory_fact_ids':[],'documents':[]})
        return {'decision':d,'reply_briefs':briefs}

    def review(self,parts=None):
        review=self.c.review({'channel':'instagram','message_ids':['m1'],'parts':parts or ['안녕']})
        compact={k:review['context'][k] for k in ('previous_self','user_uptake','topic')}
        compact.update(memory=review['memory'],checks=review['checks'])
        return {'verdict':'approve','reviews':[{'index':0,'conversation_review':compact}],
                'post_write':{},'conversation_ended':True}

    def answer(self,output):
        pending=self.worker.load()['pending'];self.serial+=1
        path=Path(self.c.tmp.name)/('response-'+str(self.serial)+'.json')
        value=model_adapter.result(pending['request'],{'input_read':True,**output})
        path.write_text(encode(value));return self.worker.complete(pending['id'],path)

    def to_review(self):
        self.start();r=self.answer(self.context());self.assertEqual(r['role'],'writer')
        r=self.answer({'parts':['안녕'],'note':''});self.assertEqual(r['role'],'review')
        return r

    def test_off_does_not_claim_read_or_create_state(self):
        self.c.cfg['role_execution']={'enabled':False,'adapters':None,'path':[]}
        with patch.object(gate,'claim_work',side_effect=AssertionError('claim')),patch.object(model_adapter,'options',side_effect=AssertionError('model')):
            self.assertEqual(roles.entry(self.c.cfg)['reason'],'off')

    def test_empty_gate_work_never_calls_a_model_or_prepares_documents(self):
        with patch.object(gate,'claim_work',return_value=(10,None)),patch.object(model_adapter,'options',side_effect=AssertionError('model')):
            self.assertEqual(roles.entry(self.c.cfg)['reason'],'no_claim')

    def test_dm_routes_context_writer_review_and_commits_exact_text(self):
        first=self.start();self.assertEqual(first['role'],'context')
        self.assertEqual(self.answer(self.context())['model'],'current-writer')
        self.assertEqual(self.answer({'parts':['안녕']})['role'],'review')
        result=self.answer(self.review());self.assertTrue(result['committed'])
        with self.c.store.connect() as db:
            self.assertEqual(db.execute('SELECT text FROM dm_jobs').fetchone()[0],'안녕')
            self.assertEqual(db.execute("SELECT count(*) FROM decisions WHERE id LIKE 'compact:%'").fetchone()[0],1)
        self.assertFalse(any(self.worker.folder.glob('role-model-input-*.json')))

    def test_writer_only_receives_scoped_writing_input_no_operation_api(self):
        self.start();self.answer(self.context())
        p=self.worker.load()['pending'];data=p['request']['input']
        self.assertEqual(p['request']['task'],'persona_writer')
        self.assertEqual(data['reply']['target_message_ids'],['m1'])
        self.assertIn('design/character/profile.md',data['documents'])
        self.assertNotIn('decision_contract',data);self.assertNotIn('prepared',data)
        self.assertNotIn('receipts',data);self.assertNotIn('API.md',data['documents'])
        self.assertNotIn('runbooks/muse-executor.md',data['documents'])

    def test_context_cannot_write_parts_or_fake_reviews(self):
        self.start();value=self.context();value['decision']['dm_decisions'][0]['parts']=['unreviewed']
        self.assertEqual(self.answer(value)['role'],'context')
        self.assertEqual(self.worker.load()['feedback']['reason'],'role_context_wrote_draft')
        self.assertEqual(self.c.snapshot()['jobs'],[])

    def test_life_required_runs_external_life_first_and_no_writer_when_no_message(self):
        c=self.c;c.sleep(3601)
        result=self.worker.run();self.assertEqual(result['role'],'life')
        activity=c.activity(aid='next',start=c.clock,end=c.clock+600000)
        self.assertEqual(self.answer({'operations':{'advance_to_ms':c.clock,'next_decision_ms':c.clock+600000,'activities':[{'id':'a1','status':'completed','actual_end_ms':c.clock,'reason':'기존 휴식 종료'},activity]}})['role'],'context')
        result=self.answer({'decision':{'advance_to_ms':c.clock,'next_decision_ms':c.clock+600000},'reply_briefs':[]})
        self.assertEqual(result['role'],'review')
        result=self.answer({'verdict':'approve','reviews':[],'post_write':{},'conversation_ended':False})
        self.assertTrue(result['committed']);self.assertEqual(c.snapshot()['activity']['id'],'next')
        log=(self.worker.folder/'trace.jsonl').read_text()
        self.assertNotIn('"role":"writer"',log)

    def test_life_cannot_create_dm_or_relationship_operations(self):
        with self.assertRaisesRegex(ControlError,'role_owned_fields'):contract.life_output({'operations':{'relationships':[]}})
        with self.assertRaisesRegex(ControlError,'role_owned_fields'):contract.life_output({'operations':{'dm_decisions':[]}})

    def test_state_proposals_wait_for_review_and_share_one_transaction(self):
        self.start();value=self.context()
        value['decision']['relationships']=[{'subject_id':'self','other_id':'user','details':{'evidence_ids':['new-fact'],'kind':'friend'}}]
        value['decision']['facts']=[{'id':'new-fact','source':'observed','summary':'검토할 사실','details':{'evidence':'verified incoming m1'}}]
        self.answer(value)
        with self.c.store.connect() as db:self.assertIsNone(db.execute("SELECT id FROM facts WHERE id='new-fact'").fetchone())
        self.answer({'parts':['안녕']});self.assertTrue(self.answer(self.review())['committed'])
        with self.c.store.connect() as db:self.assertIsNotNone(db.execute("SELECT id FROM facts WHERE id='new-fact'").fetchone())

    def test_failed_semantic_check_is_not_filled_in_or_sent(self):
        self.to_review();value=self.review();value['reviews'][0]['conversation_review']['checks']['missed_answer']['passed']=False
        result=self.answer(value)
        self.assertEqual(result['role'],'review');self.assertEqual(self.c.snapshot()['jobs'],[])
        feedback=self.worker.load()['pending']['request']['input']['feedback']
        self.assertEqual(feedback['kind'],'role_contract_error')
        self.assertEqual(feedback['reason'],'conversation_review_failed')
        self.assertEqual(self.worker.load()['revisions'],1)
        self.assertEqual(self.worker.load()['format_repairs'],0)
        self.assertTrue(self.answer(self.review())['committed'])

    def test_reviewer_cannot_rewrite_writer_text(self):
        self.to_review();value=self.review();value['reviews'][0]['parts']=['rewritten']
        self.assertEqual(self.answer(value)['role'],'review')
        self.assertEqual(self.worker.load()['feedback']['reason'],'generator_parts_changed')
        self.assertEqual(self.c.snapshot()['jobs'],[])

    def test_writer_revision_only_repeats_requested_draft(self):
        self.to_review();result=self.answer({'verdict':'revise','feedback':[{'index':0,'reason':'질문에 더 직접 답한다'}]})
        self.assertEqual(result['role'],'writer')
        self.assertIn('직접',self.worker.load()['pending']['request']['input']['revision_feedback'])
        self.answer({'parts':['응 안녕']})
        self.assertTrue(self.answer(self.review(['응 안녕']))['committed'])

    def test_exact_context_replan_reuses_the_paid_writing_result(self):
        self.to_review();result=self.answer({'verdict':'replan','target':'context','reason':'상태 필드만 재확인'})
        self.assertEqual(result['role'],'context')
        self.assertEqual(self.answer(self.context())['role'],'review')
        self.assertEqual(len(self.worker.load()['draft_cache']),1)

    def test_extra_read_is_collected_by_code_and_not_passed_to_writer_as_commands(self):
        self.start();r=self.answer({'read':[{'kind':'recall','options':{'keywords':['topic_state:book']}}]})
        self.assertEqual(r['role'],'context')
        self.assertTrue(any(i['kind']=='recall' for i in self.worker.executor.load()['inputs']))
        self.assertEqual(self.answer(self.context())['role'],'writer')

    def test_writer_cannot_request_tools(self):
        self.start();self.answer(self.context())
        result=self.answer({'read':[{'kind':'state','options':{}}]})
        self.assertEqual(result['role'],'writer')
        state=self.worker.load();self.assertEqual(state['feedback']['reason'],'role_writer_cannot_read_tools')
        self.assertEqual(state['revisions'],0);self.assertEqual(state['format_repairs'],1)

    def test_unread_memory_and_document_selectors_are_rejected(self):
        self.start();value=self.context();value['reply_briefs'][0]['memory_fact_ids']=['not-read']
        self.assertEqual(self.answer(value)['role'],'context')
        self.assertEqual(self.worker.load()['feedback']['reason'],'generator_memory_not_read')
        self.assertEqual(self.c.snapshot()['jobs'],[])

    def test_wrong_response_identity_is_rejected_without_consuming_request(self):
        result=self.start();p=self.worker.load()['pending'];value=model_adapter.result(p['request'],{'input_read':True,**self.context()})
        value['input_sha256']='wrong';path=Path(self.c.tmp.name)/'bad.json';path.write_text(encode(value))
        with self.assertRaisesRegex(ControlError,'model_input_mismatch'):self.worker.complete(p['id'],path)
        self.assertEqual(self.worker.load()['pending']['id'],result['request_id'])

    def test_completed_response_is_idempotent_and_changed_replay_is_rejected(self):
        self.start();p=self.worker.load()['pending'];value=model_adapter.result(p['request'],{'input_read':True,**self.context()})
        path=Path(self.c.tmp.name)/'again.json';path.write_text(encode(value));self.worker.complete(p['id'],path)
        self.assertTrue(self.worker.complete(p['id'],path)['replayed'])
        value['output']['reply_briefs'][0]['situation']='changed';path.write_text(encode(value))
        with self.assertRaisesRegex(ControlError,'role_response_changed'):self.worker.complete(p['id'],path)

    def test_paid_response_file_is_kept_when_acceptance_has_temporary_io_failure(self):
        self.start();p=self.worker.load()['pending'];value=model_adapter.result(p['request'],{'input_read':True,**self.context()})
        path=Path(self.c.tmp.name)/'paid.json';path.write_text(encode(value))
        with patch.object(self.worker,'accept',side_effect=OSError('disk temporarily busy')):
            with self.assertRaises(OSError):self.worker.complete(p['id'],path)
        self.assertTrue((self.worker.folder/('role-model-response-'+p['id']+'.json')).is_file())
        self.assertEqual(self.worker.run()['role'],'writer')

    def test_actual_input_change_reprepares_instead_of_attaching_old_writer(self):
        self.to_review();c=self.c
        with c.store.connect(True) as db:db.execute("UPDATE sessions SET revision=revision+1 WHERE id='main'")
        r=self.worker.run();self.assertEqual(r['reason'],'inputs_changed')
        self.assertEqual(self.worker.load()['phase'],'context')
        self.assertEqual(self.worker.load()['drafts'],{})

    def test_stale_claim_never_accepts_or_sends(self):
        self.start();gate.write_json(self.c.state_dir/'wake.json',{'wake_id':'replacement'})
        with self.assertRaisesRegex(ControlError,'stale_claim'):self.answer(self.context())
        self.assertEqual(self.c.snapshot()['jobs'],[])

    def test_same_wake_wait_and_next_turn_do_not_require_a_new_cron(self):
        self.to_review();c=self.c
        def incoming(seconds):c.sleep(seconds);c.ingest('m2');c.recent(('m1','m2'))
        c.runner.sleep=incoming;value=self.review();value['conversation_ended']=False
        r=self.answer(value);self.assertEqual(r['after']['reason'],'new_dm')
        again=self.worker.next_turn();self.assertEqual(again['wake_id'],'wake-1');self.assertEqual(again['role'],'context')
        self.assertIn('m2',Path(again['input_file']).read_text())

    def test_file_request_and_state_files_are_private_and_logs_have_no_text(self):
        self.start();self.answer(self.context())
        for path in self.worker.folder.iterdir():
            if path.is_file():self.assertEqual(path.stat().st_mode&0o777,0o600)
        log=(self.worker.folder/'trace.jsonl').read_text()
        self.assertNotIn('집에서',log);self.assertNotIn('hello',log);self.assertNotIn('documents',log)

    def test_http_roles_execute_without_frontend_or_gate_lock_during_network(self):
        for r in contract.ROLES:self.c.cfg['model_adapters'][r]={'backend':'http','endpoint':'http://localhost/fake','model':r}
        roles_seen=[]
        def transport(body,opts,timeout):
            role=opts['model'];roles_seen.append(role)
            with gate.gate_lock(self.c.state_dir),self.c.store.connect(True):pass
            data={'context':self.context(),'writer':{'parts':['안녕']},'review':self.review()}[role]
            return {'choices':[{'finish_reason':'stop','message':{'content':encode({'input_read':True,**data})}}],'usage':{'prompt_tokens':12}}
        self.worker.transport=transport
        self.assertTrue(self.start()['committed']);self.assertEqual(roles_seen,['context','writer','review'])

    def test_store_io_after_commit_recovers_without_recalling_models(self):
        self.to_review();original=self.worker.executor.save
        def crash(record):
            if record['stage']=='completed':raise OSError('final state write')
            original(record)
        with patch.object(self.worker.executor,'save',side_effect=crash):
            with self.assertRaises(OSError):self.answer(self.review())
        self.assertEqual(self.worker.load()['phase'],'commit')
        self.assertTrue(self.worker.run()['committed'])
        with self.c.store.connect() as db:self.assertEqual(db.execute('SELECT count(*) FROM dm_jobs').fetchone()[0],1)

    def test_missing_explicit_read_confirmation_never_acknowledges_inputs(self):
        self.start();pending=self.worker.load()['pending']
        path=Path(self.c.tmp.name)/'unread.json';path.write_text(encode(model_adapter.result(pending['request'],self.context())))
        self.assertEqual(self.worker.complete(pending['id'],path)['role'],'context')
        self.assertEqual(self.worker.load()['feedback']['reason'],'role_input_not_read')
        self.assertEqual(self.c.snapshot()['jobs'],[])

    def test_revision_budget_releases_claim_without_handling_incoming(self):
        self.c.cfg['role_execution']['max_revisions']=1;self.worker.opts=roles.settings(self.c.cfg)
        self.to_review();self.answer({'verdict':'revise','feedback':[{'index':0,'reason':'직접 답하기'}]})
        self.answer({'parts':['다시 안녕']})
        result=self.answer({'verdict':'revise','feedback':[{'index':0,'reason':'추가 수정'}]})
        self.assertEqual(result['reason'],'role_revision_budget');self.assertEqual(result['next'],'done')
        self.assertEqual(gate.load_json(self.c.state_dir/'wake_ack.json',{})['phase'],'failed')
        self.assertEqual(self.c.snapshot()['messages'][0]['status'],'received')
        self.assertFalse(any(self.worker.folder.glob('role-model-input-*.json')))

    def test_http_pending_tasks_never_appear_in_file_exchange(self):
        self.c.cfg['model_adapters']['context']={'backend':'http','endpoint':'http://localhost/fake','model':'context'}
        def failed(*args):
            self.assertFalse(any(self.worker.folder.glob('role-model-input-*.json')))
            raise ControlError('network_error')
        self.worker.transport=failed
        with self.assertRaisesRegex(ControlError,'network_error'):self.start()

    def test_transport_failures_are_bounded_and_leave_messages_for_gate_retry(self):
        self.c.cfg['model_adapters']['context']={'backend':'http','endpoint':'http://localhost/fake','model':'context'}
        calls=[]
        def failed(*args):calls.append(1);raise ControlError('network_error')
        self.worker.transport=failed
        with self.assertRaises(ControlError):self.start()
        with self.assertRaises(ControlError):self.worker.run()
        result=self.worker.run()
        self.assertEqual(len(calls),2);self.assertEqual(result['reason'],'role_transport_exhausted')
        self.assertEqual(self.c.snapshot()['messages'][0]['status'],'received')

    def test_paid_http_result_is_reused_after_io_error_without_calling_provider_again(self):
        self.c.cfg['model_adapters']['context']={'backend':'http','endpoint':'http://localhost/fake','model':'context'}
        calls=[]
        def fake(*args):
            calls.append(1)
            return {'choices':[{'finish_reason':'stop','message':{'content':encode({'input_read':True,**self.context()})}}]}
        self.worker.transport=fake
        with patch.object(self.worker,'accept',side_effect=OSError('temporary disk error')):
            with self.assertRaises(OSError):self.start()
        self.assertEqual(self.worker.run()['role'],'writer');self.assertEqual(len(calls),1)

    def test_shadow_warning_requires_external_review_reason(self):
        import conversation_flow
        conversation_flow.configure(self.c.store,'main',{'mode':'shadow'},self.c.clock)
        self.to_review();self.assertEqual(self.answer(self.review())['role'],'review')
        self.assertTrue(self.worker.executor.load().get('warning'))
        self.assertEqual(self.answer({'verdict':'approve_warning','reason':''})['role'],'review')
        result=self.answer({'verdict':'approve_warning','reason':'해당 초안의 누락된 shadow 계획 경고를 검토했다'})
        self.assertTrue(result['committed'])

    def test_model_selected_unknown_document_never_reaches_writer(self):
        self.start();value=self.context();value['reply_briefs'][0]['documents']=['design/not-read.md']
        self.assertEqual(self.answer(value)['role'],'context')
        self.assertEqual(self.worker.load()['feedback']['reason'],'role_writer_document_not_read')

    def test_same_writer_can_be_used_with_existing_generator_enforcement_enabled(self):
        rules=Path(self.c.tmp.name)/'writing-rules.md';rules.write_text('PINNED_WRITING_RULES')
        self.c.cfg['reply_generator']={'enabled':True,'endpoint':'https://llm.example.test/v1/chat/completions','rules_file':str(rules)}
        self.to_review();self.assertIn('PINNED_WRITING_RULES',encode(self.worker.load()['pending']['request']['input']))
        self.assertTrue(self.answer(self.review())['committed'])

    def test_role_exchange_only_uploads_its_own_file_tasks(self):
        import role_exchange_bridge as exchange
        import memory_exchange_bridge as bridge
        from test_memory_exchange_bridge import FakeDropbox,NOW
        first=self.start();folder=self.worker.folder
        (folder/('memory-model-input-'+'a'*32+'.json')).write_text('private-memory-file')
        (folder/('character-model-input-'+'b'*32+'.json')).write_text('private-character-file')
        client=FakeDropbox();state_file=folder/'bridge-state.json'
        result=bridge.run_pass(client,folder,state_file,now=NOW,profile=exchange.PROFILE)
        self.assertTrue(result['ok']);self.assertEqual(len(result['uploaded']),1)
        names=list(client.files)
        self.assertTrue(all('/role-exchange/' in n and 'role-model-input-' in n for n in names))
        self.assertEqual(exchange.PROFILE.complete_timeout,1860)

    def test_cli_off_resolves_paths_without_reading_adapters(self):
        cfg=Path(self.c.tmp.name)/'roles.json';cfg.write_text(encode({**self.c.cfg,'role_execution':{'enabled':False,'path':'roles'}}))
        stream=io.StringIO()
        with redirect_stdout(stream):code=roles.main(['--config',str(cfg),'run'])
        self.assertEqual(code,0);self.assertEqual(json.loads(stream.getvalue())['reason'],'off')
        self.assertEqual(roles.load_config(cfg)['role_execution']['path'],str(cfg.parent/'roles'))

    def test_legacy_writing_rules_are_pinned_for_writer_and_reviewer(self):
        rules=Path(self.c.tmp.name)/'rules.md';rules.write_text('FIRST_RULES')
        self.c.cfg['reply_generator']={'enabled':True,'endpoint':'https://llm.example.test/v1/chat/completions','rules_file':str(rules)}
        self.start();rules.write_text('CHANGED_RULES');self.answer(self.context())
        self.assertEqual(self.worker.load()['pending']['request']['input']['writing_rules'],'FIRST_RULES')
        self.answer({'parts':['안녕']})
        self.assertEqual(self.worker.load()['pending']['request']['input']['writing_rules'],'FIRST_RULES')

    def test_completed_turn_response_replay_never_requires_new_claim_or_resends(self):
        self.to_review();pending=self.worker.load()['pending']
        path=Path(self.c.tmp.name)/'last.json';path.write_text(encode(model_adapter.result(pending['request'],{'input_read':True,**self.review()})))
        self.assertTrue(self.worker.complete(pending['id'],path)['committed'])
        gate.write_json(self.c.state_dir/'wake.json',{'wake_id':'new-owner'})
        self.assertTrue(self.worker.complete(pending['id'],path)['replayed'])
        with self.c.store.connect() as db:self.assertEqual(db.execute('SELECT count(*) FROM dm_jobs').fetchone()[0],1)

    def test_database_constraint_error_is_external_feedback_not_an_endless_commit_retry(self):
        self.to_review();original=self.c.store.apply
        def rejected(*args,**kwargs):raise sqlite3.IntegrityError('constraint')
        with patch.object(self.c.runner.store,'apply',side_effect=rejected):
            result=self.answer(self.review())
        self.assertEqual(result['role'],'review')
        self.assertEqual(self.worker.load()['feedback']['recovery']['reason'],'controller_integrity_error')
        self.assertTrue(self.answer(self.review())['committed'])

    def test_expired_file_task_releases_claim_and_never_sends_late_output(self):
        self.start();state=self.worker.load();state['pending']['request']['deadline_ms']=self.c.clock
        self.worker.persist(state)
        result=self.worker.run();self.assertEqual(result['reason'],'role_request_expired')
        self.assertEqual(self.c.snapshot()['messages'][0]['status'],'received')
        self.assertEqual(gate.load_json(self.c.state_dir/'wake_ack.json',{})['phase'],'failed')

    def test_default_writer_input_is_smaller_than_whole_decision_input(self):
        first=self.start();context_bytes=Path(first['input_file']).stat().st_size
        second=self.answer(self.context());writer_bytes=Path(second['input_file']).stat().st_size
        self.assertLess(writer_bytes,context_bytes)

    def test_code_first_entry_preserves_hook_invocation_binding(self):
        with patch.object(gate,'claim_work',return_value=(10,None)) as claim:
            self.assertEqual(roles.entry(self.c.cfg,invocation_id='existing-hook-ticket')['reason'],'no_claim')
        claim.assert_called_once_with(self.c.cfg,'existing-hook-ticket')

    def test_code_driver_automatically_resumes_next_turn_inside_existing_wake(self):
        self.to_review();c=self.c
        def incoming(seconds):c.sleep(seconds);c.ingest('m2');c.recent(('m1','m2'))
        c.runner.sleep=incoming;value=self.review();value['conversation_ended']=False
        self.assertEqual(self.answer(value)['after']['next'],'prepare')
        roles.save(self.worker.folder/'active.json',{'wake':'wake-1'})
        with patch.object(gate,'claim_work',side_effect=AssertionError('second claim')):
            result=roles.entry(c.cfg,clock=lambda:c.clock)
        self.assertEqual(result['role'],'context');self.assertEqual(result['wake_id'],'wake-1')

    def test_independent_metric_contributions_keep_both_reasons_without_overwrite(self):
        value=contract.merge({'metric_deltas':{'mood':2},'metric_reason':'휴식 효과'},
                             {'metric_deltas':{'mood':1},'metric_reason':'확인된 대화의 반응'})
        self.assertEqual(value['metric_deltas']['mood'],3)
        self.assertIn('휴식 효과',value['metric_reason']);self.assertIn('대화의 반응',value['metric_reason'])

    def test_crash_after_validation_feedback_recovers_without_new_writing(self):
        self.to_review();value=self.review()
        # A DB-only factual-basis failure still reaches final validation after pure preflight.
        value['reviews'][0]['conversation_review']['checks']['self_consistency']['claims']=[
            {'excerpt':'안녕','fact_id':'missing-canon'}]
        original=self.worker.persist
        def crash(state):
            if state['phase']=='review' and state.get('feedback',{}).get('kind')=='controller_feedback':
                raise OSError('crash after executor feedback')
            original(state)
        with patch.object(self.worker,'persist',side_effect=crash):
            with self.assertRaises(OSError):self.answer(value)
        result=self.worker.run();self.assertEqual(result['role'],'review')
        feedback=self.worker.load()['feedback']['feedback']
        self.assertEqual(feedback['reason'],'self_claim_fact_missing')
        self.assertTrue(self.answer(self.review())['committed'])
        self.assertEqual(len(self.worker.load()['draft_cache']),1)

    def test_old_paths_and_prompt_settings_remain_unchanged_when_feature_is_off(self):
        baseline=self.c.runner.prepare(mode='bundled');self.assertNotIn('role_execution',baseline['snapshot'])
        self.assertNotIn('role_execution',baseline['documents'])


if __name__=='__main__':unittest.main()
