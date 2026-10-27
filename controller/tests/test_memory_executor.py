"""Independent memory execution against synthetic SQLite; main reply path stays intact."""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from common import ControlError
import episodic_memory as memory
import memory_executor
import memory_organizer as worker
import model_adapter
import test_episodic_memory as fixtures


class MemoryExecutorTests(unittest.TestCase):
    def setUp(self):
        self.c=fixtures.EpisodicMemoryTests('test_no_controller_writes_and_idempotent_replay')
        self.c.setUp();self.addCleanup(self.c.tearDown)

    def configure(self,backend='file',**options):
        c=self.c;c.cfg['episodic_memory']['model_adapter']='memory-test'
        c.cfg['model_adapters']={'memory-test':{'backend':backend,**options}}

    def http(self,body,*args):
        packet=json.loads(body['messages'][1]['content'])
        output=self.c.response(packet,[self.c.item(packet)])
        return {'choices':[{'finish_reason':'stop','message':{'content':json.dumps(output)}}],
                'usage':{'prompt_tokens':13,'completion_tokens':7}}

    def test_empty_work_does_not_resolve_adapter_read_keys_or_wake_model(self):
        c=self.c;c.cfg['episodic_memory']['model_adapter']='not-configured'
        with patch.object(model_adapter,'options',side_effect=AssertionError('adapter was resolved')), \
             patch.object(model_adapter,'invoke',side_effect=AssertionError('model woke')):
            result=worker.run(c.cfg,lambda:c.clock)
        self.assertEqual(result['reason'],'no_work')
        self.assertFalse(list(memory.settings(c.cfg)['path'].parent.glob('memory-model-*')))

    def test_off_and_retry_wait_do_not_invoke_adapter(self):
        c=self.c;c.cfg['episodic_memory']={'mode':'off','model_adapter':[]};c.cfg['model_adapters']=[]
        with patch.object(model_adapter,'options',side_effect=AssertionError('adapter was resolved')):
            self.assertEqual(worker.run(c.cfg,lambda:c.clock)['reason'],'off')
        c.cfg['episodic_memory']={'mode':'enforce'};c.ingest()
        packet=memory.prepare(c.cfg,c.clock)['packet'];memory.fail(c.cfg,packet['run_id'],c.clock,'network_error')
        with patch.object(model_adapter,'options',side_effect=AssertionError('adapter was resolved')):
            self.assertEqual(worker.run(c.cfg,lambda:c.clock)['reason'],'retry_later')

    def test_file_handoff_contains_full_instructions_and_can_complete_and_replay(self):
        c=self.c;c.ingest(text='마지막 작업을 완료했어');self.configure()
        before=Path(c.cfg['db']).read_bytes();first=worker.run(c.cfg,lambda:c.clock)
        self.assertEqual(first['next'],'model')
        req=json.loads(Path(first['input_file']).read_text())
        self.assertEqual(req['instructions'],(worker.ROOT/'runbooks/memory-organizer.md').read_text())
        self.assertEqual(req['input']['new_sources'][0]['text'],'마지막 작업을 완료했어')
        self.assertNotIn(c.cfg['db'],json.dumps(req))
        self.assertEqual(Path(first['input_file']).stat().st_mode&0o777,0o600)
        waiting=worker.run(c.cfg,lambda:c.clock)
        self.assertEqual(waiting['reason'],'busy');self.assertEqual(waiting['next'],'await_response')
        output=c.response(req['input'],[c.item(req['input'])])
        path=Path(first['response_file']);worker.private_write(path,model_adapter.result(req,output))
        result=worker.complete_model_file(c.cfg,first['run_id'],path,lambda:c.clock)
        self.assertTrue(result['ok']);self.assertEqual(result['memories'],1)
        self.assertFalse(path.exists());self.assertFalse(Path(first['input_file']).exists())
        replay=worker.complete_model_file(c.cfg,first['run_id'],path,lambda:c.clock)
        self.assertTrue(replay['replayed']);self.assertEqual(replay['through_seq'],result['through_seq'])
        self.assertEqual(Path(c.cfg['db']).read_bytes(),before)

    def test_file_result_binding_is_from_database_even_if_input_file_is_changed(self):
        c=self.c;c.ingest();self.configure();first=worker.run(c.cfg,lambda:c.clock)
        req=json.loads(Path(first['input_file']).read_text());value=model_adapter.result(req,c.response(req['input']))
        value['input_sha256']='wrong';req['input_sha256']='wrong';worker.private_write(first['input_file'],req)
        worker.private_write(first['response_file'],value)
        result=worker.complete_model_file(c.cfg,first['run_id'],first['response_file'],lambda:c.clock)
        self.assertEqual(result['reason'],'memory_input_mismatch')
        with memory.connection(c.cfg) as db:
            self.assertEqual(db.execute('select seq from positions').fetchone()[0],0)
            self.assertEqual(db.execute('select status from runs').fetchone()[0],'failed')

    def test_command_backend_runs_existing_storage_validation(self):
        c=self.c;c.ingest()
        program=('import json,sys; r=json.load(sys.stdin); p=r["input"]; '
                 'o={k:p[k] for k in ("schema","run_id","input_sha256","through_seq")};o["memories"]=[];'
                 'print(json.dumps({"schema":"persona-model-response-v1","request_id":r["request_id"],'
                 '"input_sha256":r["input_sha256"],"output":o}))')
        self.configure('command',argv=[sys.executable,'-c',program])
        before=Path(c.cfg['db']).read_bytes();result=worker.run(c.cfg,lambda:c.clock)
        self.assertTrue(result['ok']);self.assertEqual(result['reason'],'completed')
        self.assertEqual(Path(c.cfg['db']).read_bytes(),before)

    def test_http_adapter_records_only_metadata_and_keeps_core_instructions(self):
        c=self.c;c.ingest(text='private-source-marker');self.configure('http',model='other-model',endpoint='http://unused/chat')
        bodies=[]
        def transport(body,*args):bodies.append(body);return self.http(body,*args)
        result=worker.run(c.cfg,lambda:c.clock,transport)
        self.assertTrue(result['ok'])
        self.assertEqual(bodies[0]['messages'][0]['content'],(worker.ROOT/'runbooks/memory-organizer.md').read_text())
        log=(memory.settings(c.cfg)['path'].parent/'memory-executor.jsonl').read_text()
        self.assertNotIn('private-source-marker',log);self.assertNotIn('"instructions"',log)
        event=next(json.loads(line) for line in log.splitlines() if json.loads(line)['phase']=='model_response')
        self.assertEqual(event['usage'],{'prompt_tokens':13,'completion_tokens':7})

    def test_paid_response_resumes_after_adapter_configuration_changes(self):
        c=self.c;c.ingest();self.configure('http',model='old-model',endpoint='http://unused/chat')
        with patch.object(memory,'complete',side_effect=ControlError('busy')):
            first=worker.run(c.cfg,lambda:c.clock,self.http)
        self.assertEqual(first['reason'],'busy')
        c.cfg['model_adapters']={}
        with patch.object(model_adapter,'options',side_effect=AssertionError('adapter resolved again')), \
             patch.object(model_adapter,'invoke',side_effect=AssertionError('paid twice')):
            result=worker.run(c.cfg,lambda:c.clock)
        self.assertTrue(result['ok'])
        self.assertFalse(list(memory.settings(c.cfg)['path'].parent.glob('memory-model-*')))

    def test_pending_generic_response_survives_canonical_write_io_failure(self):
        c=self.c;c.ingest();self.configure('http',model='test',endpoint='http://unused/chat')
        original=worker.private_write
        def broken(path,value):
            if Path(path).name.startswith('memory-http-response-'):raise OSError('disk busy')
            return original(path,value)
        with patch.object(worker,'private_write',side_effect=broken):
            first=worker.run(c.cfg,lambda:c.clock,self.http)
        self.assertEqual(first['next'],'resume');self.assertTrue(Path(first['response_file']).exists())
        with patch.object(model_adapter,'invoke',side_effect=AssertionError('paid twice')):
            self.assertTrue(worker.run(c.cfg,lambda:c.clock)['ok'])

    def test_command_transport_failure_does_not_quarantine_sources(self):
        c=self.c;c.ingest();self.configure('command',argv=[sys.executable,'-c','raise SystemExit(3)'])
        result=worker.run(c.cfg,lambda:c.clock)
        self.assertEqual(result['reason'],'model_process_failed')
        with memory.connection(c.cfg) as db:
            self.assertEqual(db.execute('select count(*) from quarantined').fetchone()[0],0)
            self.assertEqual(db.execute('select count(*) from failures').fetchone()[0],0)
        self.assertEqual(worker.run(c.cfg,lambda:c.clock)['reason'],'retry_later')

    def test_invalid_file_model_output_is_failed_without_advancing_position(self):
        c=self.c;c.ingest();self.configure();first=worker.run(c.cfg,lambda:c.clock)
        Path(first['response_file']).write_text('{broken')
        result=worker.run(c.cfg,lambda:c.clock)
        self.assertEqual(result['reason'],'invalid_memory_response')
        with memory.connection(c.cfg) as db:
            self.assertEqual(db.execute('select seq from positions').fetchone()[0],0)
            self.assertEqual(db.execute('select count(*) from failures').fetchone()[0],1)

    def test_completed_receipt_rejects_changed_response_if_resubmitted(self):
        c=self.c;c.ingest();self.configure();first=worker.run(c.cfg,lambda:c.clock)
        req=json.loads(Path(first['input_file']).read_text());output=c.response(req['input'])
        external=Path(c.tmp.name)/'retained-response.json';worker.private_write(external,model_adapter.result(req,output))
        self.assertTrue(worker.complete_model_file(c.cfg,first['run_id'],external,lambda:c.clock)['ok'])
        output['memories']=[c.item(req['input'])];worker.private_write(external,model_adapter.result(req,output))
        result=worker.complete_model_file(c.cfg,first['run_id'],external,lambda:c.clock)
        self.assertEqual(result['reason'],'memory_response_changed')

    def test_executor_cli_is_single_json_and_never_claims_dm_gate(self):
        c=self.c;c.ingest();self.configure();config=Path(c.tmp.name)/'config.json';config.write_text(json.dumps(c.cfg))
        gate_paths=[c.state_dir/name for name in ('wake.json','gate_state.json','wake_ack.json')]
        before={path:path.read_bytes() for path in gate_paths}
        stdout=io.StringIO()
        with patch.object(memory_executor,'now_ms',return_value=c.clock),redirect_stdout(stdout):
            rc=memory_executor.main(['--config',str(config),'run'])
        self.assertEqual(rc,0);first=json.loads(stdout.getvalue());self.assertEqual(first['next'],'model')
        req=json.loads(Path(first['input_file']).read_text())
        worker.private_write(first['response_file'],model_adapter.result(req,c.response(req['input'])))
        stdout=io.StringIO()
        with patch.object(memory_executor,'now_ms',return_value=c.clock),redirect_stdout(stdout):
            rc=memory_executor.main(['--config',str(config),'complete','--run-id',first['run_id'],
                                     '--response','@'+first['response_file']])
        self.assertEqual(rc,0);self.assertTrue(json.loads(stdout.getvalue())['ok'])
        self.assertEqual({path:path.read_bytes() for path in gate_paths},before)

    def test_bad_adapter_configuration_does_not_count_against_source_content(self):
        c=self.c;c.ingest();self.configure('http',model='test',endpoint='http://[bad')
        result=worker.run(c.cfg,lambda:c.clock)
        self.assertEqual(result['reason'],'invalid_model_endpoint')
        with memory.connection(c.cfg) as db:
            self.assertEqual(db.execute('select count(*) from failures').fetchone()[0],0)
            self.assertEqual(db.execute('select count(*) from quarantined').fetchone()[0],0)
        self.assertEqual(worker.run(c.cfg,lambda:c.clock)['reason'],'retry_later')

    def test_corrupted_adapter_cache_is_a_storage_retry_not_source_failure(self):
        c=self.c;c.ingest();self.configure('http',model='test',endpoint='http://unused/chat')
        original=worker.private_write
        def broken(path,value):
            if Path(path).name.startswith('memory-http-response-'):raise OSError('disk busy')
            return original(path,value)
        with patch.object(worker,'private_write',side_effect=broken):
            first=worker.run(c.cfg,lambda:c.clock,self.http)
        Path(first['response_file']).write_text('{broken')
        result=worker.run(c.cfg,lambda:c.clock)
        self.assertEqual(result['reason'],'memory_saved_response_corrupt')
        with memory.connection(c.cfg) as db:
            self.assertEqual(db.execute('select count(*) from failures').fetchone()[0],0)
            self.assertEqual(db.execute('select status from runs').fetchone()[0],'failed')

    def test_file_model_can_return_existing_task_json_without_protocol_rewriting(self):
        c=self.c;c.ingest();self.configure();first=worker.run(c.cfg,lambda:c.clock)
        self.assertEqual(first['response_format'],'task-json')
        req=json.loads(Path(first['input_file']).read_text())
        output=c.response(req['input'],[c.item(req['input'])])
        worker.private_write(first['response_file'],output)
        result=worker.run(c.cfg,lambda:c.clock)
        self.assertTrue(result['ok']);self.assertEqual(result['memories'],1)


if __name__=='__main__':unittest.main()
