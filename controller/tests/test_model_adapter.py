"""Task-neutral boundary tests: local HTTP and processes, never real providers."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest

from common import ControlError
import model_adapter as adapter


class ModelAdapterTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.folder=Path(self.tmp.name)
        self.req=adapter.request('test-task','run-1','digest-1','Read every input.',{'text':'neutral fixture'},
                                 {'max_tokens':64},123456)

    @contextmanager
    def server(self,redirect=False):
        seen=[]
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_POST(self):
                body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                seen.append((self.path,dict(self.headers),body))
                if redirect:
                    self.send_response(302);self.send_header('Location','/other');self.end_headers();return
                self.send_response(200);self.end_headers()
                self.wfile.write(json.dumps({'choices':[{'finish_reason':'stop','message':{'content':json.dumps({'answer':'ok'})}}],
                    'usage':{'prompt_tokens':11,'completion_tokens':4,'prompt_tokens_details':{'cached_tokens':3}},
                    'cost':{'usd':0.002}}).encode())
        http=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=http.serve_forever,daemon=True);thread.start()
        try:yield 'http://127.0.0.1:'+str(http.server_port)+'/chat',seen
        finally:http.shutdown();http.server_close();thread.join()

    def test_legacy_settings_use_the_configured_http_endpoint(self):
        options=adapter.options({},legacy={'model':'configured-model','key_file':'unused',
                                           'endpoint':'https://llm.example.test/v1/chat/completions'})
        self.assertEqual((options['backend'],options['endpoint']),
                         ('http','https://llm.example.test/v1/chat/completions'))
        body=adapter.chat_body(options,self.req)
        self.assertEqual(body,{'model':'configured-model','max_tokens':64,
            'messages':[{'role':'system','content':'Read every input.'},
                        {'role':'user','content':'{"text": "neutral fixture"}'}]})

    def test_missing_endpoint_is_a_configuration_error(self):
        with self.assertRaisesRegex(ControlError,'model_endpoint'):
            adapter.options({},legacy={'model':'configured-model','key_file':'unused'})

    def test_real_custom_http_worker_reports_usage_and_sends_only_task_input(self):
        key=self.folder/'key';key.write_text('test-key');key.chmod(0o600)
        with self.server() as (endpoint,seen):
            options=adapter.options({'model_adapters':{'test':{'backend':'http','model':'other-model',
                'endpoint':endpoint,'key_file':str(key)}}},'test')
            result=adapter.invoke(options,self.req,5)
        self.assertEqual(result['output'],{'answer':'ok'})
        self.assertEqual(result['usage'],{'prompt_tokens':11,'completion_tokens':4,'cached_tokens':3,'cost_usd':0.002})
        self.assertEqual(seen[0][1]['Authorization'],'Bearer test-key')
        self.assertEqual(seen[0][2]['model'],'other-model')
        self.assertNotIn(str(key),json.dumps(seen[0][2]))

    def test_http_redirect_does_not_forward_credentials(self):
        key=self.folder/'key';key.write_text('test-key');key.chmod(0o600)
        with self.server(redirect=True) as (endpoint,seen):
            opts=adapter.options({'model_adapters':{'test':{'backend':'http','model':'test',
                'endpoint':endpoint,'key_file':str(key)}}},'test')
            with self.assertRaisesRegex(ControlError,'http_302'):adapter.invoke(opts,self.req,5)
        self.assertEqual(len(seen),1)

    def test_command_result_is_bound_and_unknown_diagnostics_are_not_forwarded(self):
        program=('import sys,json; r=json.load(sys.stdin); print(json.dumps({'
                 '"schema":"persona-model-response-v1","request_id":r["request_id"],'
                 '"input_sha256":r["input_sha256"],"output":{"answer":"ok"},'
                 '"usage":{"prompt_tokens":2,"prompt":"do not forward"},"debug":"private"}))')
        opts=adapter.options({'model_adapters':{'test':{'backend':'command','argv':[sys.executable,'-c',program]}}},'test')
        result=adapter.invoke(opts,self.req,5)
        self.assertEqual(result['output'],{'answer':'ok'});self.assertNotIn('debug',result)
        self.assertEqual(result['usage'],{'prompt_tokens':2})

    def test_command_failure_never_exposes_stderr(self):
        opts=adapter.options({'model_adapters':{'test':{'backend':'command',
            'argv':[sys.executable,'-c','import sys;print("private contents",file=sys.stderr);sys.exit(2)']}}},'test')
        with self.assertRaises(ControlError) as error:adapter.invoke(opts,self.req,2)
        self.assertEqual(error.exception.reason,'model_process_failed')

    def test_timeout_terminates_child_process_group(self):
        marker=self.folder/'orphan-output'
        child='import time,pathlib;time.sleep(1);pathlib.Path('+repr(str(marker))+').write_text("unexpected")'
        parent='import subprocess,sys,time;subprocess.Popen([sys.executable,"-c",'+repr(child)+']);time.sleep(5)'
        opts=adapter.options({'model_adapters':{'test':{'backend':'command',
            'argv':[sys.executable,'-c',parent]}}},'test')
        with self.assertRaisesRegex(ControlError,'model_timeout'):adapter.invoke(opts,self.req,.2)
        time.sleep(1.1)
        self.assertFalse(marker.exists())

    def test_wrong_result_binding_is_rejected(self):
        result=adapter.result(self.req,{'answer':'ok'});result['request_id']='other'
        with self.assertRaisesRegex(ControlError,'model_input_mismatch'):adapter.validate_result(result,self.req)

    def test_configuration_paths_are_relative_to_config_not_cwd(self):
        cfg={'model_adapters':{'test':{'backend':'http','key_file':'keys/key','cwd':'jobs'},
                               'unused':['invalid but unrelated']}}
        resolved=adapter.resolve_paths(cfg,self.folder/'config.json')
        self.assertEqual(resolved['model_adapters']['test']['key_file'],str(self.folder/'keys/key'))
        self.assertEqual(cfg['model_adapters']['test']['key_file'],'keys/key')
        self.assertEqual(resolved['model_adapters']['unused'],['invalid but unrelated'])

    def test_incomplete_or_non_json_output_is_not_a_transport_failure(self):
        for response,reason in (({'choices':[{'finish_reason':'length'}]},'model_incomplete'),
                                ({'choices':[{'finish_reason':'stop','message':{'content':'broken'}}]},'model_output_invalid')):
            with self.subTest(reason=reason):
                with self.assertRaisesRegex(ControlError,reason):adapter.parse_chat(response,self.req)

    def test_invalid_http_endpoint_and_command_cwd_are_configuration_errors(self):
        for options,reason in (({'backend':'http','model':'test','endpoint':'http://[bad'},'invalid_model_endpoint'),
                               ({'backend':'http','model':'test','endpoint':'http://local/chat','key_file':[]},'invalid_model_key_file'),
                               ({'backend':'command','argv':['python3'],'cwd':[]},'invalid_model_cwd')):
            with self.subTest(reason=reason):
                with self.assertRaisesRegex(ControlError,reason):
                    adapter.options({'model_adapters':{'test':options}},'test')


if __name__=='__main__':unittest.main()
