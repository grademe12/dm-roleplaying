"""Code-first character editor execution and its Dropbox exchange; synthetic SQLite, fake Dropbox, no network."""
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import re
import unittest
from unittest.mock import patch

from common import ControlError
import character_documents as docs
import character_editor as editor
import character_exchange_bridge as char_bridge
import character_executor
import memory_exchange_bridge as bridge
import model_adapter
import test_character_documents as fixtures
from test_memory_exchange_bridge import FakeDropbox

NAME_IN=re.compile(r'character-model-input-[0-9a-f]{32}\.json')
NAME_OUT=re.compile(r'character-model-response-[0-9a-f]{32}\.json')


class CharacterExecutorTests(unittest.TestCase):
    def setUp(self):
        self.t=fixtures.CharacterDocumentTests('test_seed_partitions_exact_variable_spans_without_altering_originals')
        self.t.setUp();self.addCleanup(self.t.tearDown)
        self.c=self.t.c
        self.config=Path(self.c.tmp.name)/'config.json'
        self.config.write_text(json.dumps(self.c.cfg))

    def cli(self,*args,module=character_executor):
        out=io.StringIO()
        with patch.object(module,'now_ms',return_value=self.c.clock),redirect_stdout(out):
            code=module.main(['--config',str(self.config),*args])
        lines=out.getvalue().splitlines();self.assertEqual(len(lines),1)
        return code,json.loads(lines[0])

    def folder(self):return docs.settings(self.c.cfg)['path'].parent/'character-editor'

    def answer(self,first,operations=True):
        req=json.loads(Path(first['input_file']).read_text())
        ops=[self.t.operation(req['input'])] if operations else []
        docs.private_write(first['response_file'],self.t.response(req['input'],ops))

    def test_no_work_ends_quietly_without_files_adapter_or_model(self):
        with patch.object(model_adapter,'options',side_effect=AssertionError('adapter resolved')), \
             patch.object(model_adapter,'invoke',side_effect=AssertionError('model woke')):
            code,result=self.cli('run')
        self.assertEqual(code,0);self.assertEqual(result['reason'],'no_work');self.assertTrue(result['ok'])
        self.assertNotIn('input_file',result)
        self.assertFalse(list(self.folder().glob('character-model-*')) if self.folder().exists() else [])

    def test_off_is_a_quiet_success(self):
        self.c.cfg['character_documents']={'mode':'off'};self.config.write_text(json.dumps(self.c.cfg))
        code,result=self.cli('run')
        self.assertEqual((code,result['reason']),(0,'off'))
        self.assertFalse(docs.settings(self.c.cfg)['path'].exists() if docs.settings(self.c.cfg).get('path') else False)

    def test_work_writes_only_the_input_file_with_exchange_name_and_private_mode(self):
        self.t.put('private-marker',summary='비공개표지 사건');code,result=self.cli('run')
        self.assertEqual(code,0)
        self.assertEqual((result['ok'],result['reason'],result['next']),(True,'prepared','model'))
        self.assertRegex(Path(result['input_file']).name,NAME_IN)
        self.assertEqual(Path(result['response_file']).name,'character-model-response-%s.json'%result['run_id'])
        self.assertEqual(Path(result['input_file']).stat().st_mode&0o777,0o600)
        self.assertFalse(Path(result['response_file']).exists())
        self.assertNotIn('비공개표지',json.dumps(result))
        req=json.loads(Path(result['input_file']).read_text())
        self.assertEqual(req['request_id'],result['run_id']);self.assertTrue(req['instructions']);self.assertIn('input_sha256',req)

    def test_waiting_states_are_not_failures(self):
        self.t.put();code,first=self.cli('run');self.assertEqual(code,0)
        code,second=self.cli('run')
        self.assertEqual((code,second['ok'],second['reason'],second['next']),(0,True,'busy','await_response'))
        self.assertEqual(second['input_file'],first['input_file'])
        with patch.object(character_executor.editor,'run',side_effect=ControlError('busy')):
            code,locked=self.cli('run')
        self.assertEqual((code,locked['ok'],locked['reason']),(0,True,'busy'))
        # failure backoff -> retry_later is also normal
        docs.fail(self.c.cfg,first['run_id'],self.c.clock,'network_error')
        code,later=self.cli('run');self.assertEqual((code,later['ok'],later['reason']),(0,True,'retry_later'))

    def test_real_failures_still_fail(self):
        self.c.cfg['character_documents']['mode']='bogus';self.config.write_text(json.dumps(self.c.cfg))
        code,result=self.cli('run');self.assertEqual((code,result['ok'],result['reason']),(1,False,'invalid_character_mode'))

    def test_complete_publishes_cleans_files_and_rejects_bad_response(self):
        self.t.put();_,first=self.cli('run');self.answer(first)
        code,done=self.cli('complete','--run-id',first['run_id'],'--response','@'+first['response_file'])
        self.assertEqual((code,done['ok'],done['reason']),(0,True,'published'))
        self.assertFalse(Path(first['input_file']).exists());self.assertFalse(Path(first['response_file']).exists())
        self.c.clock+=1000;self.t.put('second',summary='두 번째 사건');_,again=self.cli('run');Path(again['response_file']).write_text('{broken')
        code,bad=self.cli('complete','--run-id',again['run_id'],'--response','@'+again['response_file'])
        self.assertEqual((code,bad['ok'],bad['reason']),(1,False,'invalid_character_response'))

    def test_next_run_completes_a_response_dropped_by_the_processor(self):
        self.t.put();_,first=self.cli('run');self.answer(first)
        code,done=self.cli('run');self.assertEqual((code,done['reason']),(0,'published'))
        self.assertTrue(Path(done['document_file']).is_file())

    def test_expired_hand_off_files_are_swept_but_pending_ones_kept(self):
        self.t.put();_,first=self.cli('run')
        self.assertTrue(Path(first['input_file']).exists())
        self.c.clock=first['deadline_ms']
        code,later=self.cli('run');self.assertEqual((code,later['reason']),(0,'retry_later'))
        self.assertEqual(later['swept'],1);self.assertFalse(Path(first['input_file']).exists())
        # pending run keeps its file
        self.c.clock+=later['retry_after_ms'];_,second=self.cli('run');self.assertEqual(second['next'],'model')
        code,waiting=self.cli('run');self.assertTrue(Path(second['input_file']).exists());self.assertNotIn('swept',waiting)

    def test_executor_never_touches_dm_gate_files_or_source_db(self):
        c=self.c;self.t.put();paths=[c.state_dir/n for n in ('wake.json','gate_state.json','wake_ack.json')]
        before={p:p.read_bytes() for p in paths};db=Path(c.cfg['db']).read_bytes()
        _,first=self.cli('run');self.answer(first);self.cli('complete','--run-id',first['run_id'],'--response','@'+first['response_file'])
        self.assertEqual({p:p.read_bytes() for p in paths},before);self.assertEqual(Path(c.cfg['db']).read_bytes(),db)


class CharacterBridgeTests(unittest.TestCase):
    ROOT='/yujin-project/character-exchange'

    def setUp(self):
        self.x=CharacterExecutorTests('test_no_work_ends_quietly_without_files_adapter_or_model')
        self.x.setUp();self.addCleanup(self.x.doCleanups)
        self.dbx=FakeDropbox()

    def run_main(self,*extra,config=None):
        out=io.StringIO()
        with redirect_stdout(out):
            code=char_bridge.main(['--config',str(config or self.x.config),*extra],client=self.dbx)
        lines=out.getvalue().splitlines();self.assertEqual(len(lines),1)
        return code,json.loads(lines[0])

    def test_profile_names_roots_and_isolation_from_memory(self):
        p=char_bridge.PROFILE;a='a'*32
        self.assertEqual(p.remote_root,'/yujin-project/character-exchange')
        self.assertEqual(bridge.DEFAULT_REMOTE_ROOT,'/yujin-project/memory-exchange')
        self.assertTrue(p.input_re.fullmatch('character-model-input-%s.json'%a))
        self.assertFalse(p.input_re.fullmatch('memory-model-input-%s.json'%a))
        self.assertFalse(bridge.MEMORY_PROFILE.input_re.fullmatch('character-model-input-%s.json'%a))
        self.assertFalse(p.response_re.fullmatch('character-model-response-%s.json.part'%a))
        self.assertFalse(p.input_re.fullmatch('%s.input.json'%a))
        self.assertEqual(p.input_name(a),'character-model-input-%s.json'%a)

    def test_resolves_editor_folder_from_config_like_the_editor_does(self):
        self.assertEqual(char_bridge.resolve_exchange_dir(self.x.config),self.x.folder().resolve())
        cfg=json.loads(self.x.config.read_text());cfg['character_documents']['path']='custom/store.sqlite3'
        other=Path(self.x.c.tmp.name)/'other.json';other.write_text(json.dumps(cfg))
        self.assertEqual(char_bridge.resolve_exchange_dir(other),(other.parent/'custom/character-editor').resolve())

    def test_pass_uploads_character_inputs_only_and_cleans_when_run_ends(self):
        x=self.x;x.t.put();_,first=x.cli('run');rid=first['run_id']
        memory_file=x.folder()/('memory-model-input-%s.json'%('b'*32));memory_file.write_text('{}')
        (x.folder()/'partition.json').write_text('{}')
        code,summary=self.run_main()
        self.assertEqual(code,0);self.assertEqual([u['run_id'] for u in summary['uploaded']],[rid])
        self.assertEqual(list(self.dbx.files),['%s/in/character-model-input-%s.json'%(self.ROOT,rid)])
        self.assertTrue((x.folder()/'character-bridge-state.json').is_file())
        self.assertTrue((x.folder()/'character-bridge.lock').is_file())
        self.assertFalse((x.folder().parent/'memory-bridge-state.json').exists())
        Path(first['input_file']).unlink()
        code,summary=self.run_main();self.assertEqual(summary['cleaned'],[rid]);self.assertEqual(self.dbx.files,{})

    def test_response_download_then_complete_publishes_end_to_end(self):
        x=self.x;x.t.put();_,first=x.cli('run');rid=first['run_id']
        self.run_main()
        out_path='%s/out/character-model-response-%s.json'%(self.ROOT,rid)
        # processor ignores/does not use a memory-named response; names must match exactly
        self.dbx.put('%s/out/memory-model-response-%s.json'%(self.ROOT,rid),b'{}')
        req=json.loads(Path(first['input_file']).read_text())
        self.dbx.put(out_path,json.dumps(x.t.response(req['input'],[x.t.operation(req['input'])])).encode())
        def complete(run_id,path):
            code,result=x.cli('complete','--run-id',run_id,'--response','@'+str(path))
            return {'run_id':run_id,'ok':bool(result['ok']),'reason':result['reason']}
        summary=bridge.run_pass(self.dbx,x.folder(),x.folder()/'character-bridge-state.json',complete=complete,profile=char_bridge.PROFILE,now=1_900_000_000.0)
        self.assertEqual(summary['completed'],[{'run_id':rid,'ok':True,'reason':'published'}])
        self.assertTrue(summary['ok']);self.assertNotIn(out_path,self.dbx.files)
        self.assertFalse(Path(first['input_file']).exists());self.assertEqual(docs.view(x.c.cfg,x.c.clock)['changes'][0]['after']['item_id'],'goals:thesis')

    def test_complete_flag_invokes_the_character_executor(self):
        calls=[]
        class Done:
            stdout='{"ok":true,"reason":"published"}\n'
        def fake(argv,**kw):calls.append((argv,kw));return Done()
        with patch.object(bridge.subprocess,'run',side_effect=fake):
            result=bridge.run_complete('c'*32,Path('/x/y.json'),'cfg.json',profile=char_bridge.PROFILE)
        self.assertEqual(result,{'run_id':'c'*32,'ok':True,'reason':'published'})
        self.assertEqual(Path(calls[0][0][1]).name,'character_executor.py')
        self.assertEqual(calls[0][0][2:],['--config','cfg.json','complete','--run-id','c'*32,'--response','@/x/y.json'])
        with patch.object(bridge.subprocess,'run',side_effect=fake):
            bridge.run_complete('c'*32,Path('/x/y.json'),'cfg.json')
        self.assertEqual(Path(calls[1][0][1]).name,'memory_executor.py')

    def test_off_config_skips_without_dropbox_or_folders(self):
        cfg=json.loads(self.x.config.read_text());cfg['character_documents']={'mode':'off'}
        off=Path(self.x.c.tmp.name)/'off.json';off.write_text(json.dumps(cfg))
        code,summary=self.run_main(config=off)
        self.assertEqual((code,summary['skipped']),(0,'off'));self.assertEqual(self.dbx.calls,[])
        self.assertFalse(self.x.folder().exists())

    def test_dropbox_error_fails_soft_and_summary_has_no_contents(self):
        self.x.t.put('private-marker',summary='비공개표지 사건');self.x.cli('run')
        code,summary=self.run_main();self.assertEqual(code,0);self.assertNotIn('비공개표지',json.dumps(summary))
        self.dbx.fail_with=bridge.DropboxError('dropbox_http_500',fatal=True)
        code,summary=self.run_main();self.assertEqual(code,1);self.assertFalse(summary['ok'])


if __name__=='__main__':unittest.main()
