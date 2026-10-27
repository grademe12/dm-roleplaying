"""Offline tests for the Slack bridge, the Slack transport wrapper and the poller's channel split.

Run:  CONTROLLER_CONTROLLER=/abs/controller-dir python3 -m unittest discover -s slack/tests -v
The controller dir must contain the 2026-10-16 controller (store.py with channels).
No Slack account, token or network access is used: a local fake Slack API answers.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]           # .../slack
ROOT = HERE.parent                                     # handoff folder (poller.py, gate.py)
CONTROLLER = Path(os.environ.get('CONTROLLER_CONTROLLER', ROOT / 'controller'))
sys.path.insert(0, str(HERE))
from slack_listener import SlackBridge, ts_key  # noqa: E402

PY = sys.executable
TOKEN = 'xoxb-test-secret-token'


class FakeSlack(BaseHTTPRequestHandler):
    calls = []
    mode = {'post': 'ok'}

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
        FakeSlack.calls.append({'path': self.path, 'auth': self.headers.get('Authorization'), 'body': body})
        if self.path.startswith('/upload/'):
            return self._raw(200, b'OK - 3')
        method = self.path.rsplit('/', 1)[-1]
        if method == 'chat.postMessage':
            m = FakeSlack.mode['post']
            if m == '429':
                return self._raw(429, b'{}', {'Retry-After': '30'})
            if m == 'error':
                return self._json({'ok': False, 'error': 'channel_not_found'})
            if m == 'weird':
                return self._json({'ok': False, 'error': 'internal_error'})
            return self._json({'ok': True, 'ts': f'{time.time():.6f}'})
        if method == 'files.getUploadURLExternal':
            return self._json({'ok': True, 'upload_url': f'http://127.0.0.1:{self.server.server_port}/upload/F1', 'file_id': 'F1'})
        if method == 'files.completeUploadExternal':
            return self._json({'ok': True, 'files': [{'id': 'F1'}]})
        return self._json({'ok': False, 'error': 'unknown_method'})

    def _json(self, data):
        self._raw(200, json.dumps(data).encode())

    def _raw(self, code, body, headers=None):
        self.send_response(code)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class SlackTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), FakeSlack)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.api = f'http://127.0.0.1:{cls.server.server_port}/api'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        FakeSlack.calls.clear(); FakeSlack.mode['post'] = 'ok'
        self.tmp = Path(tempfile.mkdtemp())
        self.ctrl = self.tmp / 'ctrl'
        shutil.copytree(CONTROLLER, self.ctrl, ignore=shutil.ignore_patterns('*.db', '__pycache__', 'automation-state'))
        # The isolated flat gate entrypoint needs the same routing module as deployment.
        for f in ('poller.py', 'gate.py', 'decision_routing.py'):
            shutil.copy(ROOT / f, self.tmp / f)
        shutil.copy(HERE / 'slack_send.py', self.tmp / 'slack_send.py')
        (self.tmp / 'token').write_text(TOKEN + '\n')
        (self.tmp / 'slack-send.json').write_text(json.dumps({'channel': 'D1', 'token_path': str(self.tmp / 'token'),
                                                              'api_base': self.api, 'timeout_seconds': 5}))
        (self.tmp / 'transport-slack.json').write_text(json.dumps({'contract': 'dm-send-v2', 'enabled': True, 'timeout_seconds': 30,
            'argv': [PY, str(self.tmp / 'slack_send.py'), str(self.tmp / 'slack-send.json')]}))
        self.cfg = {'session': 'main', 'python': PY, 'ctl': str(self.ctrl / 'ctl.py'), 'db': str(self.tmp / 'c.db'),
                    'state_dir': str(self.tmp / 'state'), 'instagram_enabled': False,
                    'slack': {'channel': 'D1', 'peer_user_id': 'UPEER', 'self_user_id': 'UBOT', 'ingest_after_ts': '1700000000.000000',
                              'transport_config': str(self.tmp / 'transport-slack.json')}}
        (self.tmp / 'automation.json').write_text(json.dumps(self.cfg))
        (self.tmp / 'seed.json').write_text(json.dumps({'transport_mode': 'external'}))
        self.assertTrue(self.ctl('seed', '--import', '@' + str(self.tmp / 'seed.json'))[1]['ok'])
        self.bridge = SlackBridge(self.cfg, history=self.fake_history, run_ctl=self.ctl)
        self.history_pages = []

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def ctl(self, *args):
        p = subprocess.run([PY, self.cfg['ctl'], '--db', self.cfg['db'], *args], capture_output=True, text=True, encoding='utf-8')
        return p.returncode, json.loads(p.stdout)

    def fake_history(self, **kw):
        self.history_calls = getattr(self, 'history_calls', []) + [kw]
        return self.history_pages.pop(0) if self.history_pages else {'messages': [], 'has_more': False}

    def send(self, request):
        p = subprocess.run([PY, str(self.tmp / 'slack_send.py'), str(self.tmp / 'slack-send.json')],
                           input=json.dumps(request).encode(), capture_output=True)
        self.assertNotIn(TOKEN, p.stdout.decode() + p.stderr.decode())
        return json.loads(p.stdout)

    def ev(self, ts, user='UPEER', text='안녕', **extra):
        return {'type': 'message', 'channel': 'D1', 'user': user, 'ts': ts, 'text': text, **extra}

    # --- webhook nudge ---------------------------------------------------------------
    def test_nudge_after_ingest_is_rate_limited_and_off_by_default(self):
        self.assertEqual(self.bridge.handle(self.ev('1700000001.000001')), 'ingested')
        time.sleep(0.2)
        self.assertEqual([c for c in FakeSlack.calls if c['path'] == '/nudge'], [])   # no nudge config: off
        (self.tmp / 'nudge-url').write_text(f'http://127.0.0.1:{self.server.server_port}/nudge\n')
        (self.tmp / 'nudge-key').write_text('nudge-secret\n')
        cfg = {**self.cfg, 'nudge': {'enabled': True, 'url_path': str(self.tmp / 'nudge-url'),
                                     'key_path': str(self.tmp / 'nudge-key'), 'min_interval_seconds': 20}}
        bridge = SlackBridge(cfg, history=self.fake_history, run_ctl=self.ctl)
        self.assertEqual(bridge.handle(self.ev('1700000002.000002')), 'ingested')
        self.assertEqual(bridge.handle(self.ev('1700000003.000003')), 'ingested')   # inside min interval
        self.assertEqual(bridge.handle(self.ev('1700000004.000004', user='UBOT')), 'recorded')
        deadline = time.time() + 5
        while time.time() < deadline and not any(c['path'] == '/nudge' for c in FakeSlack.calls):
            time.sleep(0.05)
        time.sleep(0.2)
        nudges = [c for c in FakeSlack.calls if c['path'] == '/nudge']
        self.assertEqual(len(nudges), 1)
        self.assertEqual(nudges[0]['auth'], 'Bearer nudge-secret')
        self.assertEqual(json.loads(nudges[0]['body']),
                         {'session': 'main', 'reason': 'dm_ingested', 'ts': '1700000002.000002'})
        (self.tmp / 'nudge-url').write_text('http://127.0.0.1:9/unreachable\n')
        bridge.last_nudge = None
        self.assertEqual(bridge.handle(self.ev('1700000005.000005')), 'ingested')   # failure never blocks ingest

    # --- slack_send.py --------------------------------------------------------------
    def test_send_text_results(self):
        r = self.send({'contract': 'dm-send-v2', 'text': '<b>&ㅋㅋ', 'job_id': 'g:0', 'channel': 'slack'})
        self.assertEqual(r['status'], 'confirmed')
        body = json.loads(FakeSlack.calls[-1]['body'])
        self.assertEqual((body['channel'], body['text']), ('D1', '&lt;b&gt;&amp;ㅋㅋ'))
        self.assertEqual(FakeSlack.calls[-1]['auth'], 'Bearer ' + TOKEN)
        FakeSlack.mode['post'] = '429'
        r = self.send({'contract': 'dm-send-v2', 'text': 'x', 'job_id': 'g:1'})
        self.assertEqual((r['status'], r['not_sent'], r['code'], r['retry_after_ms']), ('failed', True, 429, 30000))
        FakeSlack.mode['post'] = 'error'
        self.assertEqual(self.send({'contract': 'dm-send-v2', 'text': 'x', 'job_id': 'g:2'})['not_sent'], True)
        FakeSlack.mode['post'] = 'weird'
        self.assertEqual(self.send({'contract': 'dm-send-v2', 'text': 'x', 'job_id': 'g:3'})['status'], 'unknown')
        r = self.send({'contract': 'dm-send-v2', 'text': 'x', 'job_id': 'g:4', 'channel': 'instagram'})
        self.assertEqual((r['status'], r['evidence']), ('failed', 'wrong_channel_for_transport'))

    def test_send_image_three_steps(self):
        img = self.tmp / 'a.png'; img.write_bytes(b'png')
        r = self.send({'contract': 'dm-send-v2', 'text': '[사진]', 'job_id': 'g:0', 'kind': 'image',
                       'attachment_path': str(img), 'media_id': 'm1'})
        self.assertEqual((r['status'], r['provider_message_id']), ('confirmed', 'F1'))
        self.assertEqual([c['path'].rsplit('/', 1)[-1] for c in FakeSlack.calls],
                         ['files.getUploadURLExternal', 'F1', 'files.completeUploadExternal'])
        self.assertEqual(json.loads(FakeSlack.calls[-1]['body'])['channel_id'], 'D1')
        r = self.send({'contract': 'dm-send-v2', 'text': '[사진]', 'job_id': 'g:1', 'kind': 'image',
                       'attachment_path': str(self.tmp / 'missing.png'), 'media_id': 'm2'})
        self.assertEqual((r['status'], r['not_sent']), ('failed', True))

    def test_server_down_means_not_sent(self):
        cfg = json.loads((self.tmp / 'slack-send.json').read_text()); cfg['api_base'] = 'http://127.0.0.1:9/api'
        (self.tmp / 'slack-send.json').write_text(json.dumps(cfg))
        r = self.send({'contract': 'dm-send-v2', 'text': 'x', 'job_id': 'g:0'})
        self.assertEqual((r['status'], r.get('not_sent')), ('failed', True))

    # --- SlackBridge ----------------------------------------------------------------
    def test_classify_filters(self):
        b = self.bridge
        self.assertEqual(b.classify(self.ev('1800000000.000100'))['direction'], 'incoming')
        self.assertEqual(b.classify(self.ev('1800000000.000200', user=None, bot_id='B1'))['direction'], 'outgoing')
        self.assertIsNone(b.classify(self.ev('1800000000.000300', subtype='message_changed')))
        self.assertIsNone(b.classify(self.ev('1800000000.000400', thread_ts='1800000000.000100')))
        self.assertIsNone(b.classify(dict(self.ev('1800000000.000500'), channel='DOTHER')))
        self.assertIsNone(b.classify(self.ev('1800000000.000600', user='USTRANGER')))
        self.assertIn('unknown_sender', b.health['alerts'])
        rec = b.classify(self.ev('1800000000.000700', text='봐봐 &lt;3', files=[{'mimetype': 'image/png'}, {'mimetype': 'image/jpeg'}]))
        self.assertEqual(rec['text'], '봐봐 <3 [사진 2장]')

    def test_handle_ingests_once_with_slack_channel(self):
        self.assertEqual(self.bridge.handle(self.ev('1800000000.000100')), 'ingested')
        self.assertEqual(self.bridge.handle(self.ev('1800000000.000100')), 'duplicate')
        self.assertEqual(self.bridge.handle(self.ev('1600000000.000100')), 'recorded')   # before boundary: context only
        msgs = self.ctl('tick', '--session', 'main')[1]['messages']
        self.assertEqual([m['id'] for m in msgs], ['slack:D1:1800000000.000100'])
        self.assertEqual(msgs[0]['observation']['channel'], 'slack')
        recent = json.loads((self.tmp / 'state' / 'recent_thread_slack.json').read_text(encoding='utf-8'))
        self.assertEqual(len(recent['messages']), 2)

    def test_ingest_failure_is_retried_by_backfill(self):
        failing = SlackBridge(self.cfg, history=self.fake_history, run_ctl=lambda *a: (1, {'ok': False, 'reason': 'x'}))
        self.assertEqual(failing.handle(self.ev('1800000000.000100')), 'ingest_failed')
        failing.handle(self.ev('1800000000.000200', user=None, bot_id='B1'))   # a later outgoing must not hide it
        self.assertEqual(failing.retry_ts, '1800000000.000100')
        fixed = SlackBridge(self.cfg, history=self.fake_history, run_ctl=self.ctl)   # restart with a working ctl
        self.history_pages = [{'messages': [self.ev('1800000000.000200', user=None, bot_id='B1'), self.ev('1800000000.000100')],
                               'has_more': False}]
        self.assertEqual(fixed.backfill(), 1)
        self.assertEqual((self.history_calls[-1]['oldest'], self.history_calls[-1]['inclusive']), ('1800000000.000100', True))
        self.assertIsNone(fixed.retry_ts)
        self.assertEqual(len(self.ctl('tick', '--session', 'main')[1]['messages']), 1)

    # --- poller + controller end to end ------------------------------------------------
    def poll(self):
        p = subprocess.run([PY, str(self.tmp / 'poller.py'), '--config', str(self.tmp / 'automation.json')],
                           capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(p.returncode, 0, p.stderr[-300:])
        return json.loads(p.stdout)

    def reply(self, mid, gid, text, rev=None):
        t = self.ctl('tick', '--session', 'main')[1]
        review = {'context': {'channel': 'slack', 'recent_message_ids': [mid], 'target_message_ids': [mid],
                              'previous_self': None, 'user_uptake': 'test', 'topic': 'test'},
                  'memory': {'kind': 'general', 'topic_state_key': None, 'result': 'not_applicable'},
                  'draft_parts': [text],
                  'checks': {k: {'passed': True, 'evidence': 'test'} for k in ('self_reset', 'missed_answer', 'conflict')}}
        d = {'decision_id': 'd-' + gid, 'expected_revision': t['revision'], 'advance_to_ms': t['now_ms'],
             'next_decision_ms': t['now_ms'] + 3600000,
             'dm_decisions': [{'action': 'reply', 'message_ids': [mid], 'group_id': gid, 'due_ms': 0, 'parts': [text], 'reason': 'r', 'conversation_review': review}]}
        (self.tmp / 'd.json').write_text(json.dumps(d, ensure_ascii=False), encoding='utf-8')
        return self.ctl('apply', '--session', 'main', '--decision', '@' + str(self.tmp / 'd.json'))[1]

    def test_poller_sends_slack_only_while_listener_is_alive(self):
        self.bridge.handle(self.ev(f'{time.time():.6f}', text='뭐해?'))
        mid = self.ctl('tick', '--session', 'main')[1]['messages'][0]['id']
        self.assertTrue(self.reply(mid, 'g1', '누워 있어')['ok'])
        self.bridge.heartbeat(False)
        out = self.poll()
        self.assertNotIn('chat.postMessage', [c['path'].rsplit('/', 1)[-1] for c in FakeSlack.calls])
        self.assertIn('slack_listener_down', out['alerts'])
        self.bridge.heartbeat(True)
        out = self.poll()
        posts = [json.loads(c['body'])['text'] for c in FakeSlack.calls if c['path'].endswith('chat.postMessage')]
        self.assertEqual(posts, ['누워 있어'])
        self.assertNotIn('slack_listener_down', out['alerts'])
        self.assertEqual(self.ctl('tick', '--session', 'main')[1]['jobs'], [])

    def gate(self):
        p = subprocess.run([PY, str(self.tmp / 'gate.py'), '--config', str(self.tmp / 'automation.json')], capture_output=True)
        wake = json.loads((self.tmp / 'state' / 'wake.json').read_text(encoding='utf-8')) if p.returncode == 0 else None
        return p.returncode, wake

    def test_gate_sees_review_required_and_slack_alerts(self):
        self.ctl('ingest', '--session', 'main', '--batch', json.dumps({'read_ok': True, 'messages': [
            {'id': 'u1', 'text': 'x', 'direction': 'incoming', 'observation': {'identity': 'uncertain', 'channel': 'slack'}}]}))
        self.bridge.classify(self.ev('1800000000.000600', user='USTRANGER')); self.bridge.heartbeat(True)
        rc, wake = self.gate()
        self.assertEqual(rc, 0)
        self.assertIn('message_review', wake['reasons'])
        self.assertIn('slack:unknown_sender', wake['poller_alerts'])

    def test_instagram_429_does_not_stop_slack(self):
        fixture = self.tmp / 'ig.json'
        (self.tmp / 'fake_ig.py').write_text('import sys;sys.stderr.write("HTTP 429 Too Many Requests");sys.exit(1)')
        cfg = dict(self.cfg, instagram_enabled=True, read_argv=[PY, str(self.tmp / 'fake_ig.py')], read_page_size=20,
                   self_fbid='S', peer_fbid='P', thread_id='t', ingest_after_ms=1, transport_config=str(self.tmp / 'transport-slack.json'))
        (self.tmp / 'automation.json').write_text(json.dumps(cfg))
        self.bridge.heartbeat(True)
        self.bridge.handle(self.ev(f'{time.time():.6f}', text='있어?'))
        mid = self.ctl('tick', '--session', 'main')[1]['messages'][0]['id']
        self.reply(mid, 'g1', '웅 있어')
        out = self.poll()
        self.assertIn('read_failing', out['alerts'])                 # instagram is rate limited
        self.assertEqual(out['send'].get('slack'), 'send_interval')   # slack still sent
        self.assertTrue(any(c['path'].endswith('chat.postMessage') for c in FakeSlack.calls))


if __name__ == '__main__':
    unittest.main()
