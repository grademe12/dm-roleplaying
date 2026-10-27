"""Offline durable handoff, restart, deadline and hook coverage. No Slack/model calls."""
from contextlib import closing
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from common import ControlError
from doorbell import Doorbell
from slack.doorbell_listener import record
from slack.receive_once import synchronize
from slack.slack_listener import SlackBridge
from store import Store
from wake_hook import check
from wake_receiver import WakeReceiver, durable_json, state_json

TARGET = {'persona':'yujin','workspace':'T1','channel':'D1'}
T = 1791946800000


class DoorbellTests(unittest.TestCase):
    def test_runtime_imports_are_not_shadowed_by_sdk_legacy_package(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            vendor = root/'vendor'
            (vendor/'slack').mkdir(parents=True)
            (vendor/'slack/__init__.py').write_text(
                "raise RuntimeError('SDK legacy package shadowed local transport')\n")
            source = Path(__file__).resolve().parents[1]
            env = {**os.environ, 'PYTHONPATH': str(source)+os.pathsep+str(vendor)}
            result = subprocess.run([sys.executable, '-c',
                'from slack.doorbell_listener import record; '
                'from slack.receive_once import synchronize'],
                cwd=root, env=env, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.ledger = Doorbell(self.root/'doorbell.db')
        self.ledger.initialize()
        self.now = T

    def tearDown(self):
        self.tmp.cleanup()

    def hint(self, suffix='000001'):
        ts = '1791946800.'+suffix
        return self.ledger.ring(TARGET,'slack:'+ts,ts,self.now)['seq']

    def peek(self):
        return self.ledger.snapshot(TARGET,'vm',self.now)

    def ack(self, offer, **changes):
        req = {'operation':'ack','binding':TARGET,'consumer':'vm','epoch':offer['epoch'],
               'offer_id':offer['offer_id'],'through_seq':offer['through_seq']}
        req.update(changes)
        return self.ledger.request(req)

    def test_missing_ledger_never_silently_resets_sequence(self):
        with self.assertRaisesRegex(ControlError,'not_initialized'):
            Doorbell(self.root/'missing.db').snapshot(TARGET,'vm')
        with self.assertRaises(FileExistsError):
            self.ledger.initialize()

    def test_duplicate_socket_event_is_one_durable_hint(self):
        self.assertEqual(self.hint(),self.hint())
        self.assertEqual(len(Doorbell(self.ledger.path).snapshot(TARGET,'vm')['hints']),1)

    def test_ack_does_not_swallow_later_arrival(self):
        first = self.hint()
        offer = self.peek()
        later = self.hint('000002')
        self.assertEqual(self.ack(offer)['ack_seq'],first)
        self.assertEqual([r['seq'] for r in self.peek()['hints']],[later])
        self.assertEqual(self.ack(offer)['ack_seq'],first)

    def test_ack_is_fenced_by_offer_consumer_binding_epoch_and_watermark(self):
        self.hint()
        offer = self.peek()
        for change in ({'consumer':'other'},{'epoch':'lost'},{'through_seq':999},
                       {'binding':{**TARGET,'persona':'other-persona'}}):
            with self.subTest(change=change), self.assertRaises(ControlError):
                self.ack(offer,**change)
        self.assertEqual(self.peek()['ack_seq'],0)

    def test_snapshot_batches_and_consumer_isolation(self):
        for i in range(105):
            self.ledger.ring(TARGET,str(i),now=self.now)
        offer = self.peek()
        self.assertEqual(len(offer['hints']),100)
        self.assertTrue(offer['has_more'])
        self.ack(offer)
        self.assertEqual(len(self.peek()['hints']),5)
        self.assertEqual(len(self.ledger.snapshot(TARGET,'other')['hints']),100)

    def test_listener_only_persists_matching_incoming_metadata(self):
        cfg = {'binding':TARGET,'peer_user_id':'U1','ingest_after_ts':'1791946700.000000'}
        payload = {'team_id':'T1','event':{'type':'message','channel':'D1','user':'U1',
                   'ts':'1791946800.000001','text':'PRIVATE BODY','files':[{'name':'PRIVATE FILE'}]}}
        self.assertEqual(record(self.ledger,cfg,payload,T),'recorded')
        record(self.ledger,cfg,payload,T)
        self.assertEqual(len(self.peek()['hints']),1)
        with closing(self.ledger.connect()) as db:
            dump = '\n'.join(db.iterdump())
        self.assertNotIn('PRIVATE',dump)
        for changed in ({'user':'OTHER'},{'channel':'D2'},{'bot_id':'B1'},
                        {'thread_ts':'1791946800.000000'},{'subtype':'message_changed'}):
            record(self.ledger,cfg,{'team_id':'T1','event':{**payload['event'],**changed}},T)
        record(self.ledger,cfg,{**payload,'team_id':'OTHER'},T)
        self.assertEqual(len(self.peek()['hints']),1)

    def test_broker_rpc_uses_existing_database_and_does_not_expose_bodies(self):
        self.hint()
        proc = subprocess.run([sys.executable,str(Path(__file__).parents[1]/'doorbell.py'),
                               '--db',str(self.ledger.path)],input=json.dumps(
                                  {'operation':'snapshot','binding':TARGET,'consumer':'vm'}),
                              text=True,capture_output=True)
        self.assertEqual(proc.returncode,0,proc.stderr)
        self.assertEqual(len(json.loads(proc.stdout)['hints']),1)

    def test_concurrent_detectors_dedupe_same_event(self):
        source = Path(__file__).parents[1]
        code = ('import sys; from doorbell import Doorbell; '
                'Doorbell(sys.argv[1]).ring('+repr(TARGET)+',"same-event")')
        children = [subprocess.Popen([sys.executable,'-c',code,str(self.ledger.path)],cwd=source,
                                     stdout=subprocess.PIPE,stderr=subprocess.PIPE) for _ in range(3)]
        for child in children:
            _, errors = child.communicate(timeout=15)
            self.assertEqual(child.returncode,0,errors)
        self.assertEqual(len(self.peek()['hints']),1)


class ReceiverTests(unittest.TestCase):
    hint = DoorbellTests.hint
    peek = DoorbellTests.peek
    tearDown = DoorbellTests.tearDown

    def setUp(self):
        DoorbellTests.setUp(self)
        self.cfg = {'db':str(self.root/'main.db'),'state_dir':str(self.root/'state'), 'session':'main',
                    'wake':{'enabled':True,'binding':TARGET,'consumer':'vm'}}
        self.calls = []
        self.sync = lambda offer: self.cover(offer)

    def cover(self, offer):
        self.calls.append(offer)
        return {'ok':True,'complete':True,'binding':TARGET,'epoch':(offer or {}).get('epoch'),
                'through_seq':(offer or {}).get('through_seq')}

    def runner(self, broker=None, sync=None):
        return WakeReceiver(self.cfg,broker=broker or self.ledger.request,
                            sync=sync or self.sync,clock=lambda:self.now)

    def test_receiver_ack_after_complete_ingest_only(self):
        self.hint()
        def sync(offer):
            self.assertEqual(self.peek()['ack_seq'],0)
            return self.cover(offer)
        self.assertTrue(self.runner(sync=sync).cycle()['acked'])
        self.assertEqual(len(self.peek()['hints']),0)

    def test_partial_or_wrong_coverage_is_not_acked(self):
        for report in ({'ok':False},{'ok':True,'complete':False},
                       {'ok':True,'complete':True,'binding':TARGET,'epoch':'wrong','through_seq':1}):
            with self.subTest(report=report):
                self.hint()
                runner = self.runner(sync=lambda _:report)
                self.now += 20000
                self.assertFalse(runner.cycle()['acked'])
                self.assertEqual(self.peek()['ack_seq'],0)

    def test_new_arrival_during_sync_survives_restart_and_ack(self):
        self.hint()
        def sync(offer):
            self.hint('000002')
            return self.cover(offer)
        self.runner(sync=sync).cycle()
        self.assertEqual(len(self.peek()['hints']),1)
        self.runner().cycle()
        self.assertEqual(len(self.peek()['hints']),0)

    def test_lost_ack_response_retries_same_offer_without_repeating_ingest(self):
        self.hint()
        def broken(req):
            result = self.ledger.request(req)
            if req['operation']=='ack':
                raise ControlError('doorbell_broker_unavailable')
            return result
        self.assertFalse(self.runner(broker=broken).cycle()['acked'])
        self.assertIsNotNone(state_json(self.runner().path)['handoff'])
        self.assertTrue(self.runner().cycle()['acked'])
        self.assertEqual(len(self.calls),1)

    def test_broker_failure_still_runs_receive_fallback(self):
        def broken(_):
            raise ControlError('doorbell_broker_unavailable')
        result = self.runner(broker=broken).cycle()
        self.assertFalse(result['ok'])
        self.assertTrue(result['received'])
        self.assertEqual(self.calls,[None])

    def test_new_hint_does_not_wait_for_periodic_fallback(self):
        self.runner().cycle()
        self.now += 1000
        self.hint()
        self.assertTrue(self.runner().cycle()['acked'])

    def test_rate_limit_backoff_survives_new_hints_and_restart(self):
        self.hint()
        self.runner(sync=lambda _:{'ok':False,'retry_after_ms':60000}).cycle()
        self.now += 15000
        self.hint('000002')
        self.assertFalse(self.runner().cycle()['received'])
        self.assertEqual(self.calls,[])
        self.now += 60000
        self.assertTrue(self.runner().cycle()['acked'])

    def test_disabled_receiver_does_nothing(self):
        self.cfg['wake']['enabled'] = False
        self.assertEqual(self.runner().cycle()['state'],'disabled')
        self.assertFalse(self.runner().path.exists())

    def test_changed_broker_epoch_is_an_error_not_empty_inbox(self):
        self.runner().cycle()
        other = Doorbell(self.root/'other.db')
        other.initialize()
        result = self.runner(broker=other.request).cycle()
        self.assertEqual(result['broker'],'doorbell_epoch_changed')
        self.assertFalse(result['acked'])


class HookTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = {'db':str(Path(self.tmp.name)/'main.db'),'state_dir':self.tmp.name,
                    'wake':{'enabled':True}}
        self.due = lambda:{'ok':True,'wake_eligible':True,'reasons':['activity_due'],'revision':1}

    def tearDown(self):
        self.tmp.cleanup()

    def test_deadline_without_dm_wakes_and_failed_launch_is_retried(self):
        self.assertEqual(check(self.cfg,gate_check=self.due,now=T)['action'],'wake')
        self.assertEqual(check(self.cfg,gate_check=self.due,now=T+15000)['action'],'silent')
        self.assertEqual(check(self.cfg,gate_check=self.due,now=T+30000)['action'],'wake')

    def test_active_worker_or_gate_retry_wait_does_not_wake(self):
        result = check(self.cfg,gate_check=lambda:{'wake_eligible':False},now=T)
        self.assertEqual(result['action'],'silent')
        self.assertFalse((Path(self.tmp.name)/'wake-hook.json').exists())

    def test_dry_run_does_not_consume_debounce(self):
        self.assertEqual(check(self.cfg,gate_check=self.due,now=T,dry_run=True)['action'],'wake')
        self.assertFalse((Path(self.tmp.name)/'wake-hook.json').exists())
        self.assertEqual(check(self.cfg,gate_check=self.due,now=T)['action'],'wake')

    def test_clock_rollback_does_not_suppress_forever(self):
        check(self.cfg,gate_check=self.due,now=T)
        self.assertEqual(check(self.cfg,gate_check=self.due,now=T-1000)['action'],'wake')

    def test_runtime_shell_calls_exactly_one_helper_and_worker_claim_remains_authoritative(self):
        import shutil
        if not shutil.which('bash'):
            self.skipTest('runtime shell dependencies unavailable')
        root = Path(self.tmp.name)
        helper = root/'runtime.sh'
        helper.write_text('wake() { printf "WAKE %s\\n" "$1"; }\n'
                          'silent() { printf "SILENT %s\\n" "$1"; }\n'
                          'log() { :; }\n')
        source = root/'source'
        source.mkdir()
        (source/'wake_hook.py').write_text('import json\n'
            'print(json.dumps({"action":"wake","reason":"gate_work_due",'
            '"payload":{"entry":"gate_claim"}}))\n')
        env = {**os.environ,'HATCH_HOOK_RUNTIME':str(helper),'CONTROLLER_SOURCE':str(source),
               'CONTROLLER_AUTOMATION_CONFIG':str(root/'unused.json'),'CONTROLLER_PYTHON':sys.executable}
        script = Path(__file__).parents[2]/'operations/hooks/wake.sh'
        result = subprocess.run(['bash',str(script)],env=env,capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(result.stdout.strip(),'WAKE gate_work_due')
        (source/'wake_hook.py').write_text('raise SystemExit(1)\n')
        result = subprocess.run(['bash',str(script)],env=env,capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(result.stdout.strip(),'SILENT wake hook failed')


class ReceiveSyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = Store(self.root/'main.db')
        self.store.seed({},T)
        self.cfg = {'db':str(self.store.path),'state_dir':str(self.root/'state'),'session':'main',
                    'slack':{'channel':'D1','peer_user_id':'U1','self_user_id':'SELF',
                             'ingest_after_ts':'1791946700.000000'},'wake':{'binding':TARGET}}
        self.msg = {'type':'message','channel':'D1','user':'U1','ts':'1791946800.000001','text':'hello'}
        self.offer = {'binding':TARGET,'epoch':'epoch','through_seq':1,
                      'hints':[{'message_ts':self.msg['ts']}]}
        owner = self
        class Web:
            def auth_test(self): return {'ok':True,'team_id':'T1'}
            def conversations_history(self,**kw):
                return {'messages':[owner.msg],'has_more':False}
        self.web = Web()

    def tearDown(self):
        self.tmp.cleanup()

    def ctl(self,*args):
        if args[0]=='ingest':
            batch = json.loads(Path(args[args.index('--batch')+1][1:]).read_text())
            return 0,self.store.ingest('main',batch,T+1000)
        raise AssertionError(args)

    def sync(self):
        return synchronize(self.cfg,self.offer,web=self.web,run_ctl=self.ctl,clock=lambda:T+1000)

    def test_completed_receive_proves_hint_is_in_authoritative_database(self):
        result = self.sync()
        self.assertTrue(result['complete'])
        self.assertEqual(result['new_incoming_count'],1)
        with self.store.connect() as db:
            row = db.execute('SELECT text FROM dm_messages WHERE id=?',('slack:D1:'+self.msg['ts'],)).fetchone()
        self.assertEqual(row[0],'hello')
        health = state_json(Path(self.cfg['state_dir'])/'slack_health.json')
        self.assertTrue(health['connected'])
        self.assertEqual(health['receive_mode'],'doorbell_backfill')

    def test_history_event_not_visible_yet_never_certifies_handoff(self):
        self.web.conversations_history = lambda **kw:{'messages':[],'has_more':False}
        with self.assertRaisesRegex(ControlError,'hint_not_ingested'):
            self.sync()

    def test_paginated_failure_keeps_unfinished_watermark_and_no_certificate(self):
        self.web.conversations_history = lambda **kw:{'messages':[self.msg], 'has_more':True,
                                                     'response_metadata':{'next_cursor':'again'}}
        self.cfg['wake']['backfill_pages'] = 1
        self.assertFalse(self.sync()['ok'])
        state = state_json(Path(self.cfg['state_dir'])/'slack_seen.json')
        self.assertEqual(state['backfill_cursor'],'1791946700.000000')

    def test_database_restore_replays_seen_hint_inclusively(self):
        self.sync()
        with self.store.connect(True) as db:
            db.execute('DELETE FROM input_events')
            db.execute('DELETE FROM dm_messages')
        self.assertTrue(self.sync()['complete'])

    def test_wrong_workspace_does_not_read_or_ingest(self):
        self.web.auth_test = lambda:{'ok':True,'team_id':'OTHER'}
        with self.assertRaisesRegex(ControlError,'identity_mismatch'):
            self.sync()

    def test_receipts_are_observed_only_when_explicitly_enabled(self):
        for enabled in (False,True):
            cfg = {**self.cfg,'slack':{**self.cfg['slack'],'backfill_receipts_enabled':enabled}}
            bridge = SlackBridge(cfg,lambda **_:None,self.ctl,clock=lambda:T+1000)
            bridge.delivery_workspace = 'T1'
            with patch.object(bridge,'_observe_delivery') as observe:
                list(bridge._ingest_history_page([dict(self.msg)]))
            self.assertEqual(observe.call_count,int(enabled))


if __name__ == '__main__':
    unittest.main()
