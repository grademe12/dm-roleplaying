"""Recovery boundaries: infrastructure never poisons data or sends stale wording."""
from contextlib import contextmanager, redirect_stdout
import io,json
from pathlib import Path
import unittest
from unittest.mock import patch
import episodic_memory as mem
import memory_organizer as worker
import reply_generator as generator
import prepared_inputs
import muse_executor
from common import ControlError, required_text
import test_episodic_memory as fixtures

class EpisodicRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.c=fixtures.EpisodicMemoryTests('test_no_controller_writes_and_idempotent_replay')
        self.c.setUp(); self.addCleanup(self.c.tearDown)

    def next_packet(self):
        c=self.c
        result=mem.prepare(c.cfg,c.clock)
        if result['reason']=='retry_later':
            c.clock+=result['retry_after_ms'];result=mem.prepare(c.cfg,c.clock)
        self.assertEqual(result['reason'],'prepared')
        return result['packet']

    def test_forty_transport_failures_keep_all_twenty_clean_sources(self):
        c=self.c
        for i in range(20):c.ingest('source-'+str(i),text='정상 메시지 '+str(i))
        waits=[]
        for _ in range(40):
            p=self.next_packet();self.assertEqual(len(p['new_sources']),20)
            self.assertTrue(mem.fail(c.cfg,p['run_id'],c.clock,'transport_unavailable'))
            wait=mem.prepare(c.cfg,c.clock);self.assertEqual(wait['reason'],'retry_later')
            waits.append(wait['retry_after_ms'])
        self.assertEqual(waits,sorted(waits));self.assertLessEqual(max(waits),900000)
        with mem.connection(c.cfg) as db:
            self.assertEqual(db.execute('select count(*) from quarantined').fetchone()[0],0)
            self.assertEqual(db.execute('select count(*) from failures').fetchone()[0],0)
            self.assertEqual(db.execute('select seq from positions').fetchone()[0],0)
        p=self.next_packet();mem.complete(c.cfg,c.response(p),c.clock)
        self.assertEqual(mem.prepare(c.cfg,c.clock)['reason'],'no_work')

    def test_channel_backoff_does_not_starve_other_channel(self):
        c=self.c;c.ingest();p=self.next_packet();mem.fail(c.cfg,p['run_id'],c.clock,'network_error')
        c.ingest('slack:1',observation={'identity':'platform','channel':'slack','slack_ts':'1'})
        p=mem.prepare(c.cfg,c.clock)['packet'];self.assertEqual(p['channel'],'slack')
        mem.complete(c.cfg,c.response(p),c.clock)
        self.assertEqual(mem.prepare(c.cfg,c.clock)['reason'],'retry_later')

    def test_content_split_persists_until_original_window_finishes(self):
        c=self.c
        for i in range(4):c.ingest('b'+str(i))
        p=self.next_packet();mem.fail(c.cfg,p['run_id'],c.clock,'invalid_memory_response')
        p=self.next_packet();self.assertEqual(len(p['new_sources']),2)
        mem.complete(c.cfg,c.response(p),c.clock)
        for i in range(4,8):c.ingest('b'+str(i))
        p=self.next_packet();self.assertEqual(len(p['new_sources']),2)
        mem.complete(c.cfg,c.response(p),c.clock)
        p=self.next_packet();self.assertEqual(len(p['new_sources']),4)

    def test_busy_then_lock_release_preserves_valid_response(self):
        c=self.c;c.ingest();p=self.next_packet();response=c.response(p)
        original=mem.file_lock;calls=[]
        @contextmanager
        def busy_once(*args,**kw):
            calls.append(1)
            if len(calls)==1:raise ControlError('busy')
            with original(*args,**kw):yield
        with patch.object(mem,'file_lock',busy_once):
            with self.assertRaisesRegex(ControlError,'busy'):mem.complete(c.cfg,response,c.clock)
        with mem.connection(c.cfg) as db:
            self.assertEqual(db.execute('select status from runs').fetchone()[0],'pending')
            self.assertEqual(db.execute('select count(*) from failures').fetchone()[0],0)
        self.assertTrue(mem.complete(c.cfg,response,c.clock)['ok'])

    def test_native_cli_does_not_count_busy_as_invalid_output(self):
        c=self.c;c.ingest();p=self.next_packet()
        config=Path(c.tmp.name)/'cfg.json';config.write_text(json.dumps(c.cfg))
        output=Path(c.tmp.name)/'output.json';output.write_text(json.dumps(c.response(p)))
        with patch.object(mem,'complete',side_effect=ControlError('busy')),patch.object(mem,'fail') as fail,redirect_stdout(io.StringIO()):
            code=worker.main(['--config',str(config),'complete','--run-id',p['run_id'],'--response',str(output)])
        self.assertEqual(code,1);fail.assert_not_called()
        self.assertTrue(mem.complete(c.cfg,c.response(p),c.clock)['ok'])

    def test_http_outage_preserves_reason_and_leaves_source_unquarantined(self):
        c=self.c;c.ingest();c.cfg['episodic_memory'].update(model='fake',endpoint='https://llm.example.test/v1/chat/completions',key_file='unused')
        def unavailable(*args):raise ControlError('network_error')
        result=worker.run(c.cfg,lambda:c.clock,unavailable)
        self.assertEqual(result['reason'],'network_error')
        self.assertEqual(mem.prepare(c.cfg,c.clock)['reason'],'retry_later')
        with mem.connection(c.cfg) as db:self.assertEqual(db.execute('select count(*) from failures').fetchone()[0],0)

    def test_bulk_quarantine_recovery_preserves_latest_progress(self):
        c=self.c
        for i in range(3):c.ingest('s'+str(i))
        p=self.next_packet();mem.complete(c.cfg,c.response(p),c.clock)
        with mem.connection(c.cfg,True) as db:
            for seq in (1,2):db.execute('insert into quarantined values (?,?,?,?)',(seq,'invalid_memory_response','quarantined',c.clock))
        self.assertEqual(mem.retry_quarantined(c.cfg,None,c.clock,all_sources=True)['count'],2)
        for seq in (1,2):
            p=self.next_packet();self.assertEqual(p['through_seq'],seq)
            mem.complete(c.cfg,c.response(p),c.clock)
        self.assertEqual(mem.prepare(c.cfg,c.clock)['reason'],'no_work')
        with mem.connection(c.cfg) as db:self.assertEqual(db.execute('select seq from positions').fetchone()[0],3)

    def test_invalid_optional_configuration_does_not_break_real_prepare(self):
        c=self.c;c.ingest();c.recent()
        for raw in ([],{'mode':'on'},{'mode':'enforce','path':['bad']},{'mode':'enforce','batch_size':0}):
            c.cfg['episodic_memory']=raw
            with self.subTest(raw=raw):
                with self.assertRaises((ControlError,TypeError)):mem.settings(c.cfg)
                self.assertNotIn('episodic_memory',c.runner.prepare(mode='bundled')['snapshot'])
                self.assertEqual(mem.recall(c.cfg,'instagram','기억',c.clock)['mode'],'off')

    def test_selected_store_loss_reprepares_same_turn_without_stale_memory(self):
        c=self.c;c.remember();c.recent();p=c.runner.prepare(mode='bundled');draft=c.draft()
        mem.settings(c.cfg)['path'].unlink()
        with self.assertRaisesRegex(ControlError,'episodic_reprepare_required'):c.runner.commit(p['turn_id'],draft)
        self.assertEqual(c.snapshot()['jobs'],[])
        renewed=c.runner.prepare(mode='bundled')
        self.assertEqual(renewed['turn_id'],p['turn_id']);self.assertNotIn('episodic_memory',renewed['snapshot'])
        c.runner.prepare(mode='bundled')  # Remain suspended in this turn; no repeated retry loop.
        with patch.object(c.runner,'finish',return_value={'next':'done'}):
            self.assertTrue(c.runner.commit(p['turn_id'],c.draft())['committed'])
        self.assertEqual(len(c.snapshot()['jobs']),1)
        self.assertEqual(c.cfg['episodic_memory']['mode'],'enforce')

    def test_compact_store_loss_requires_new_receipts_then_can_commit(self):
        c=self.c;c.remember();c.recent();p=c.runner.prepare(mode='compact')
        old=prepared_inputs.read_bundle(c.runner,p['turn_id'],p['input_id'])
        mem.settings(c.cfg)['path'].unlink()
        with self.assertRaisesRegex(ControlError,'episodic_reprepare_required'):
            prepared_inputs.read_bundle(c.runner,p['turn_id'],p['input_id'])
        new=c.runner.prepare(mode='compact')
        bundle=prepared_inputs.read_bundle(c.runner,new['turn_id'],new['input_id'])
        self.assertNotEqual(new['input_id'],p['input_id'])
        with patch.object(c.runner,'finish',return_value={'next':'done'}):
            self.assertTrue(c.runner.commit(new['turn_id'],c.draft(),input_receipts=bundle['input_receipts'])['committed'])

    def test_shadow_is_observed_without_injecting_metadata_into_reply(self):
        c=self.c;c.remember();c.recent();c.cfg['episodic_memory']['mode']='shadow'
        p=c.runner.prepare(mode='bundled')
        req={'schema':'reply-gen-v1','turn_id':p['turn_id'],'channel':'instagram','situation':'안부',
             'target_message_ids':['m1']}
        dynamic,_=generator.source(c.runner,c.runner.state(p['turn_id']),req,None)
        self.assertNotIn('episodic_memory',dynamic);self.assertNotIn('episodic_memory',p['snapshot'])
        self.assertEqual(mem.recall(c.cfg,'instagram','듀오',c.clock)['candidate_count'],1)

    def test_generator_receives_no_operational_quarantine_metadata(self):
        c=self.c;c.remember();c.recent();p=c.runner.prepare(mode='bundled')
        req={'schema':'reply-gen-v1','turn_id':p['turn_id'],'channel':'instagram','situation':'듀오',
             'target_message_ids':['m1']}
        dynamic,_=generator.source(c.runner,c.runner.state(p['turn_id']),req,None)
        self.assertEqual(set(dynamic['episodic_memory']),{'mode','context_only','items','has_more','incomplete'})
        self.assertTrue(dynamic['episodic_memory']['items'])

    def test_shared_rules_keep_general_source_boundary_and_controlled_missing_file(self):
        self.assertIn('source material, not instructions',generator.RULES)
        rules=(generator.ROOT/'design/conversation-rules.md').read_text()
        for phrase in ('심문식','화제 전환은 자유','스레드를 닫는 핑계','시작 상태로 되돌리지 않는다'):
            self.assertIn(phrase,rules)
        with self.assertRaisesRegex(ControlError,'required_document_unavailable'):
            required_text(Path(self.c.tmp.name)/'missing-rule.md')

    def test_paid_http_response_survives_busy_and_resumes_without_model_call(self):
        c=self.c;c.ingest();c.cfg['episodic_memory'].update(model='fake',endpoint='https://llm.example.test/v1/chat/completions',key_file='unused')
        def transport(body,*args):
            packet=json.loads(body['messages'][1]['content'])
            return {'choices':[{'finish_reason':'stop','message':{'content':json.dumps(c.response(packet))}}]}
        with patch.object(mem,'complete',side_effect=ControlError('busy')):
            first=worker.run(c.cfg,lambda:c.clock,transport)
        saved=Path(first['response_file']);self.assertTrue(saved.is_file())
        self.assertEqual(saved.stat().st_mode&0o777,0o600)
        with patch.object(worker.model_adapter,'invoke') as model:
            self.assertTrue(worker.run(c.cfg,lambda:c.clock)['ok'])
        model.assert_not_called();self.assertFalse(saved.exists())

    def test_empty_summary_is_output_failure_not_transport_failure(self):
        c=self.c;c.ingest()
        for _ in range(3):
            p=self.next_packet();response=c.response(p,[c.item(p,summary='')])
            with self.assertRaisesRegex(ControlError,'invalid_summary'):
                mem.complete(c.cfg,response,c.clock)
        self.assertEqual(mem.prepare(c.cfg,c.clock)['reason'],'needs_attention')
        with mem.connection(c.cfg) as db:
            self.assertEqual(db.execute("select count(*) from quarantined where status='quarantined'").fetchone()[0],1)

    def test_store_lost_during_paid_generation_requires_new_input_and_generation(self):
        c=self.c;c.remember();c.recent();p=c.runner.prepare(mode='bundled')
        rules=Path(c.tmp.name)/'rules.md';rules.write_text('test rules')
        c.cfg['reply_generator']={'enabled':True,'endpoint':'https://llm.example.test/v1/chat/completions','rules_file':str(rules)}
        request={'schema':'reply-gen-v1','turn_id':p['turn_id'],'channel':'instagram',
                 'situation':'이전 게임 이야기','target_message_ids':['m1']}
        calls=[]
        def transport(body,*args):
            calls.append(json.loads(body['messages'][1]['content']))
            if len(calls)==1:mem.settings(c.cfg)['path'].unlink()
            return {'choices':[{'finish_reason':'stop','message':{'content':json.dumps({'parts':['reply']})}}]}
        with self.assertRaisesRegex(ControlError,'episodic_reprepare_required'):
            generator.generate(c.runner,p['turn_id'],request,transport=transport)
        self.assertEqual(c.snapshot()['jobs'],[])
        renewed=c.runner.prepare(mode='bundled')
        result=generator.generate(c.runner,renewed['turn_id'],request,transport=transport)
        self.assertTrue(result['ok']);self.assertNotIn('reused',result)
        self.assertTrue(calls[0]['episodic_memory']['items']);self.assertNotIn('episodic_memory',calls[1])
        draft=c.draft();draft['dm_decisions'][0]['generator_id']=result['generator_id']
        with patch.object(c.runner,'finish',return_value={'next':'done'}):
            self.assertTrue(c.runner.commit(renewed['turn_id'],draft)['committed'])

    def test_http_cleanup_error_cannot_turn_success_into_source_failure(self):
        c=self.c;c.ingest();c.cfg['episodic_memory'].update(model='fake',endpoint='https://llm.example.test/v1/chat/completions',key_file='unused')
        def transport(body,*args):
            packet=json.loads(body['messages'][1]['content'])
            return {'choices':[{'finish_reason':'stop','message':{'content':json.dumps(c.response(packet))}}]}
        with patch.object(worker,'remove_response',return_value=False):
            result=worker.run(c.cfg,lambda:c.clock,transport)
        self.assertTrue(result['ok']);self.assertIn('cleanup_pending',result)
        self.assertEqual(mem.prepare(c.cfg,c.clock)['reason'],'no_work')

    def test_executor_cli_store_loss_requests_prepare_then_recovers_same_turn(self):
        c=self.c;c.remember();c.recent();executor=muse_executor.MuseExecutor(c.runner)
        packet=executor.prepare();turn=executor.load()['turn_id']
        response={'request_id':packet['request_id'],'input_sha256':packet['input_sha256'],
                  'input_read':True,'action':'commit','decision':c.draft()}
        config=Path(c.tmp.name)/'cfg.json';config.write_text(json.dumps(c.cfg))
        output=Path(c.tmp.name)/'response.json';output.write_text(json.dumps(response))
        mem.settings(c.cfg)['path'].unlink();stdout=io.StringIO()
        with patch.object(muse_executor,'DecisionSession',return_value=c.runner),redirect_stdout(stdout):
            rc=muse_executor.main(['--config',str(config),'--wake-id',c.runner.wake,
                                  'respond','--response','@'+str(output)])
        self.assertEqual(rc,2);self.assertEqual(json.loads(stdout.getvalue())['next'],'prepare')
        self.assertEqual(c.snapshot()['jobs'],[])
        renewed=executor.prepare();self.assertEqual(executor.load()['turn_id'],turn)
        with self.assertRaisesRegex(ControlError,'executor_input_mismatch'):
            executor.respond(response)
        with patch.object(c.runner,'finish',return_value={'next':'done'}):
            self.assertTrue(executor.respond({**response,'request_id':renewed['request_id'],
                'input_sha256':renewed['input_sha256'],'decision':c.draft()})['committed'])

    def test_executor_store_loss_during_commit_invalidates_saved_response(self):
        c=self.c;c.remember();c.recent();executor=muse_executor.MuseExecutor(c.runner)
        packet=executor.prepare();commit=c.runner.commit
        def lost(*args,**kwargs):
            mem.settings(c.cfg)['path'].unlink()
            return commit(*args,**kwargs)
        with patch.object(c.runner,'commit',side_effect=lost):
            with self.assertRaisesRegex(ControlError,'episodic_reprepare_required'):
                executor.respond({'request_id':packet['request_id'],'input_sha256':packet['input_sha256'],
                    'input_read':True,'action':'commit','decision':c.draft()})
        self.assertEqual(executor.load()['stage'],'invalidated')
        self.assertEqual(c.snapshot()['jobs'],[])
        self.assertEqual(executor.prepare()['next'],'muse')

    def test_executor_retracted_selection_requires_reprepare_without_suspending_memory(self):
        c=self.c;_,response=c.remember();c.recent();executor=muse_executor.MuseExecutor(c.runner)
        packet=executor.prepare();c.ingest('update',text='그 사건 기록을 정정할게')
        p=self.next_packet();selected=next(m for m in p['existing_memories'] if m['key']=='event:duo')
        mem.complete(c.cfg,c.response(p,[c.item(p,status='retracted',supersedes=selected['id'])]),c.clock)
        with self.assertRaisesRegex(ControlError,'memory_inputs_changed'):
            executor.respond({'request_id':packet['request_id'],'input_sha256':packet['input_sha256'],
                'input_read':True,'action':'commit','decision':c.draft()})
        self.assertIn('memory_inputs_changed',muse_executor.REPREPARE)
        state=c.runner.state();self.assertNotEqual(state.get('episodic_suspended_turn'),state['turn_id'])
        self.assertEqual(c.snapshot()['jobs'],[])

    def test_input_mismatch_splits_and_quarantines_instead_of_backing_off_forever(self):
        c=self.c
        for i in range(4):c.ingest('bad-input-'+str(i))
        sizes=[]
        for _ in range(5):
            p=self.next_packet();sizes.append(len(p['new_sources']))
            output=c.response(p);output['input_sha256']='wrong'
            with self.assertRaisesRegex(ControlError,'memory_input_mismatch'):
                mem.complete(c.cfg,output,c.clock)
        self.assertEqual(sizes,[4,2,1,1,1])
        with mem.connection(c.cfg) as db:
            self.assertEqual(db.execute("select count(*) from quarantined where status='quarantined'").fetchone()[0],1)
            self.assertEqual(db.execute('select not_before_ms from organizer_retries').fetchone()[0],0)

    def test_http_invalid_envelope_fails_actual_run_without_sqlite_type_errors(self):
        for field,value in (('run_id',[]),('run_id',None),('run_id','wrong'),
                            ('input_sha256',{}),('through_seq',True),('through_seq',[])):
            c=fixtures.EpisodicMemoryTests('test_no_controller_writes_and_idempotent_replay');c.setUp()
            try:
                with self.subTest(field=field,value=value):
                    c.ingest();c.cfg['episodic_memory'].update(model='fake',endpoint='https://llm.example.test/v1/chat/completions',key_file='unused')
                    def transport(body,*args):
                        packet=json.loads(body['messages'][1]['content']);output=c.response(packet);output[field]=value
                        return {'choices':[{'finish_reason':'stop','message':{'content':json.dumps(output)}}]}
                    result=worker.run(c.cfg,lambda:c.clock,transport)
                    self.assertFalse(result['ok'])
                    with mem.connection(c.cfg) as db:
                        self.assertEqual(db.execute('select status from runs').fetchone()[0],'failed')
                        self.assertEqual(db.execute('select count(*) from failures').fetchone()[0],1)
            finally:c.tearDown()

    def test_corrupt_saved_http_response_is_cleaned_and_retried_without_quarantine(self):
        c=self.c;c.ingest();c.cfg['episodic_memory'].update(model='fake',endpoint='https://llm.example.test/v1/chat/completions',key_file='unused')
        def transport(body,*args):
            packet=json.loads(body['messages'][1]['content'])
            return {'choices':[{'finish_reason':'stop','message':{'content':json.dumps(c.response(packet))}}]}
        with patch.object(mem,'complete',side_effect=ControlError('busy')):
            result=worker.run(c.cfg,lambda:c.clock,transport)
        path=Path(result['response_file']);path.write_text('{broken')
        with patch.object(worker.model_adapter,'invoke') as model:
            result=worker.run(c.cfg,lambda:c.clock)
        model.assert_not_called();self.assertEqual(result['reason'],'memory_saved_response_corrupt')
        self.assertFalse(path.exists())
        with mem.connection(c.cfg) as db:
            self.assertEqual(db.execute('select status from runs').fetchone()[0],'failed')
            self.assertEqual(db.execute('select count(*) from failures').fetchone()[0],0)
            self.assertEqual(db.execute('select count(*) from quarantined').fetchone()[0],0)
        waiting=mem.prepare(c.cfg,c.clock);self.assertEqual(waiting['reason'],'retry_later')
        c.clock+=waiting['retry_after_ms']
        self.assertTrue(worker.run(c.cfg,lambda:c.clock,transport)['ok'])

    def test_saved_http_validation_failure_clears_file_and_records_once(self):
        c=self.c;c.ingest();c.cfg['episodic_memory'].update(model='fake',endpoint='https://llm.example.test/v1/chat/completions',key_file='unused')
        p=self.next_packet();path=mem.settings(c.cfg)['path'].parent/('memory-http-response-'+p['run_id']+'.json')
        output=c.response(p);output['input_sha256']='wrong';worker.private_write(path,output)
        result=worker.run(c.cfg,lambda:c.clock)
        self.assertEqual(result['reason'],'memory_input_mismatch');self.assertFalse(path.exists())
        with mem.connection(c.cfg) as db:
            self.assertEqual(db.execute('select status from runs').fetchone()[0],'failed')
            self.assertEqual(db.execute('select attempts from failures').fetchone()[0],1)
