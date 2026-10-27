"""Fake clock + real SQLite inbox and main ledger; no network or model."""
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import unittest
from unittest.mock import patch

import test_topic_materials as baseline
from common import ControlError, file_lock
import conversation_flow as flow
import topic_materials as legacy
import topic_producer as producer
import topic_search_worker as worker


class TopicProducerTests(unittest.TestCase):
    setUp=baseline.TopicMaterialsTests.setUp
    tearDown=baseline.TopicMaterialsTests.tearDown
    snapshot=baseline.TopicMaterialsTests.snapshot
    decision=baseline.TopicMaterialsTests.decision
    review=baseline.TopicMaterialsTests.review
    ingest=baseline.TopicMaterialsTests.ingest
    activity=baseline.TopicMaterialsTests.activity
    enable=baseline.TopicMaterialsTests.enable
    note=baseline.TopicMaterialsTests.note

    def claim(self):return producer.claim(self.store,'main',self.clock)

    def finish(self,run,status='no_candidates',**payload):
        return producer.result(self.store,'main',{'run_id':run['run_id'],'token':run['token'],
                                                'status':status,**payload},self.clock)

    def ready(self):
        self.enable();run=self.claim();self.assertTrue(run['claimed'])
        self.clock+=1000;fact,material=self.note()
        material['subject_key']=run['task']['subject_key']
        self.finish(run,'success',facts=[fact],materials=[material])
        return run,fact,material

    def test_180_second_collection_accepts_success_after_old_60_second_boundary(self):
        self.enable();before=self.snapshot()['revision'];run=self.claim()
        self.assertEqual(run['deadline_ms']-self.clock,180000)
        self.clock+=120000
        fact,material=self.note();material['subject_key']=run['task']['subject_key']
        saved=self.finish(run,'success',facts=[fact],materials=[material])
        self.assertEqual(saved['status'],'ready')
        self.assertEqual(self.snapshot()['revision'],before)
        self.assertEqual(producer.publish_pending(self.store,'main',self.clock)['published'],1)

    def test_180_second_collection_expires_after_its_deadline(self):
        self.enable();run=self.claim();self.clock=run['deadline_ms']+1
        fact,material=self.note();material['subject_key']=run['task']['subject_key']
        saved=self.finish(run,'success',facts=[fact],materials=[material])
        self.assertEqual(saved['status'],'expired')
        self.assertEqual(producer.publish_pending(self.store,'main',self.clock)['published'],0)

    def test_worker_passes_full_remaining_180_second_budget_to_adapter(self):
        self.enable();before=self.snapshot()['revision']
        cfg={'db':str(self.path),'topic_search':{'worker_argv':['mock-search']}}
        with patch.object(worker,'invoke',return_value={'status':'no_candidates'}) as invoke:
            saved=worker.run(cfg,lambda:self.clock)
        args=invoke.call_args.args
        self.assertEqual(args[2],180)
        self.assertEqual(args[1]['deadline_ms']-args[1]['started_ms'],180000)
        self.assertNotIn('token',args[1])
        self.assertEqual(saved['status'],'no_candidates')
        self.assertEqual(self.snapshot()['revision'],before)

    def test_yujin_topics_link_overwatch_and_stardew_without_exporting_private_bodies(self):
        self.enable()
        self.store.apply('main',self.decision(facts=[
            {'id':'ow-topic','source':'simulated','summary':'topic_state:overwatch current interest',
             'details':{'private_text':'must not leave the database'}},
            {'id':'sv-topic','source':'simulated','summary':'topic_state:stardew current interest',
             'details':{'private_text':'must not leave the database'}}]),self.clock)
        first=self.claim()
        self.assertEqual(first['task']['subject_key'],'games:overwatch')
        self.assertEqual(first['task']['topic_state_ids'],['ow-topic'])
        self.assertIn('오버워치',first['task']['query'])
        self.assertNotIn('private_text',json.dumps(first['task']))
        self.finish(first);self.clock+=6*flow.HOUR
        second=self.claim()
        self.assertEqual(second['task']['subject_key'],'games:stardew')
        self.assertEqual(second['task']['topic_state_ids'],['sv-topic'])
        self.assertNotIn('must not leave',json.dumps(second['task']))

    def test_claim_and_staging_do_not_invalidate_existing_decision(self):
        self.enable();before=self.snapshot();draft=self.decision();run=self.claim()
        self.finish(run)
        self.assertEqual(self.snapshot()['revision'],before['revision'])
        self.assertEqual(self.snapshot()['needs_decision'],before['needs_decision'])
        self.assertEqual(self.snapshot()['state']['next_decision_ms'],before['state']['next_decision_ms'])
        self.store.apply('main',draft,self.clock)

    def test_pending_dm_focus_sleep_lease_and_unknown_do_not_gate_producer(self):
        self.enable();self.ingest()
        # Pending groups and uncertain delivery are irrelevant to collecting.
        self.store.apply('main',self.decision(dm_decisions=[{
            'action':'reply','message_ids':['m1'],'group_id':'uncertain','parts':['answer'],
            'reason':'fixture','due_ms':self.clock}]),self.clock)
        self.ingest('m2')
        with self.store.connect(True) as db:
            db.execute("UPDATE dm_jobs SET status='unknown'")
            db.execute("UPDATE activities SET attention='focused',category='work'")
            db.execute('INSERT INTO conversation_leases(session_id,until_ms,started_ms) VALUES (?,?,?)',('main',self.clock+60000,self.clock))
        before=self.snapshot();run=self.claim()
        self.assertTrue(run['claimed'])
        self.assertEqual(self.snapshot()['revision'],before['revision'])
        self.assertEqual([j['status'] for j in self.snapshot()['jobs']],['unknown'])
        self.finish(run);self.clock+=6*flow.HOUR
        with self.store.connect(True) as db:
            db.execute("UPDATE activities SET attention='unavailable',category='sleep'")
        self.assertTrue(self.claim()['claimed'])

    def test_disabled_attempt_logged_without_budget_use(self):
        result=self.claim()
        self.assertFalse(result['claimed'])
        report=producer.inspect(self.store,'main',self.clock)
        self.assertEqual(report['runs'],[])
        self.assertEqual(report['events'][0]['reason'],'topic_search_disabled')
        self.enable();self.assertEqual(self.claim()['attempts_today'],1)

    def test_duplicate_claim_budget_and_cooldown_shared_with_legacy(self):
        self.enable();run=self.claim()
        self.assertEqual(self.claim()['reason'],'topic_search_already_active')
        with self.assertRaisesRegex(ControlError,'topic_search_already_active'):
            legacy.claim(self.store,'main',self.clock)
        self.finish(run,'failed')
        self.assertEqual(self.claim()['reason'],'topic_search_cooldown')
        self.clock+=6*flow.HOUR
        second=legacy.claim(self.store,'main',self.clock)
        self.assertEqual(second['attempts_today'],2)
        legacy.result(self.store,'main',{'run_id':second['run_id'],'token':second['token'],'status':'no_candidates'},self.clock)
        self.assertEqual(self.claim()['reason'],'topic_search_daily_budget')

    def test_legacy_attempts_count_toward_producer_budget(self):
        self.enable();run=legacy.claim(self.store,'main',self.clock)
        legacy.result(self.store,'main',{'run_id':run['run_id'],'token':run['token'],'status':'failed'},self.clock)
        self.clock+=6*flow.HOUR
        self.assertEqual(self.claim()['attempts_today'],2)

    def test_day_reset_does_not_reset_interval(self):
        from datetime import datetime
        self.clock=int(datetime(2026,10,20,23,59,tzinfo=producer.KST).timestamp()*1000)
        self.enable();run=self.claim();self.finish(run)
        self.clock+=120000
        self.assertEqual(self.claim()['reason'],'topic_search_cooldown')
        self.clock+=6*flow.HOUR
        self.assertEqual(self.claim()['attempts_today'],1)

    def test_claim_lock_is_a_soft_skip(self):
        self.enable()
        with file_lock(self.store.path,'topic_search_budget'):
            result=self.claim()
        self.assertTrue(result['ok']);self.assertFalse(result['claimed'])
        self.assertEqual(result['reason'],'busy')
        self.assertEqual(self.claim()['attempts_today'],1)

    def test_deadline_expiry_records_terminal_without_publishing(self):
        self.enable();run=self.claim();self.clock=run['deadline_ms']+1
        self.assertEqual(self.finish(run,'timeout')['status'],'expired')
        self.assertEqual(producer.publish_pending(self.store,'main',self.clock)['runs'],0)
        self.assertEqual(self.claim()['reason'],'topic_search_cooldown')

    def test_late_success_is_discarded_and_retry_is_idempotent(self):
        self.enable();run=self.claim();self.clock=run['deadline_ms']+1
        f,m=self.note();first=self.finish(run,'success',facts=[f],materials=[m])
        again=self.finish(run,'success',facts=[f],materials=[m])
        self.assertEqual(first['status'],'expired');self.assertEqual(again['status'],'expired')
        with self.assertRaisesRegex(ControlError,'topic_search_result_collision'):
            self.finish(run,'failed')

    def test_unfinished_crash_expires_without_consuming_extra_budget(self):
        self.enable();run=self.claim();self.clock=run['deadline_ms']+1
        self.assertEqual(self.claim()['reason'],'topic_search_cooldown')
        self.assertEqual(producer.inspect(self.store,'main',self.clock)['runs'][0]['status'],'expired')
        self.clock+=6*flow.HOUR
        self.assertEqual(self.claim()['attempts_today'],2)

    def test_staged_material_not_visible_until_publication(self):
        run,fact,material=self.ready();before=self.snapshot()['revision']
        self.assertEqual(legacy.materials(self.store,'main',self.clock)['materials'],[])
        self.assertEqual(self.store.recall('main',[fact['id']])['facts'],[])
        self.assertEqual(producer.publish_pending(self.store,'main',self.clock)['published'],1)
        self.assertEqual(self.snapshot()['revision'],before+1)
        self.assertEqual(len(legacy.materials(self.store,'main',self.clock)['materials']),1)
        self.assertEqual(legacy.materials(self.store,'main',self.clock)['materials'][0]['units'],[])
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM conversation_uses').fetchone()[0],0)
        self.assertEqual(producer.publish_pending(self.store,'main',self.clock)['runs'],0)

    def test_invalid_candidate_does_not_lose_valid_candidate_or_reply(self):
        self.enable();run=self.claim();self.clock+=1000
        f,m=self.note();m['subject_key']=run['task']['subject_key']
        bad_f,bad_m=self.note(url='https://example.test/bad',canonical='event:bad')
        bad_f['details']['body_verified']=False;bad_m['subject_key']=m['subject_key']
        self.finish(run,'success',facts=[f,bad_f],materials=[bad_m,m])
        self.assertEqual(producer.publish_pending(self.store,'main',self.clock)['published'],1)
        self.assertEqual(self.store.recall('main',[bad_f['id']])['facts'],[])
        report=producer.inspect(self.store,'main',self.clock)
        self.assertEqual(report['runs'][0]['publication']['excluded'][0]['reason'],'topic_source_body_unverified')
        self.store.apply('main',self.decision(),self.clock)

    def test_bad_candidate_does_not_consume_a_valid_material_slot(self):
        self.enable();run=self.claim();self.clock+=1000
        facts=[];materials=[]
        for i in range(4):
            fact,material=self.note(url='https://example.test/candidate/'+str(i),canonical='event:'+str(i))
            material['subject_key']=run['task']['subject_key']
            if i==0:fact['details']['body_verified']=False
            facts.append(fact);materials.append(material)
        self.finish(run,'success',facts=facts,materials=materials)
        self.assertEqual(producer.publish_pending(self.store,'main',self.clock)['published'],3)

    def test_off_results_discard_without_reply_revision_change(self):
        run,f,m=self.ready();flow.configure(self.store,'main',{'search_enabled':False},self.clock)
        before=self.snapshot()['revision']
        self.assertEqual(producer.publish_pending(self.store,'main',self.clock)['published'],0)
        self.assertEqual(self.snapshot()['revision'],before)
        self.store.apply('main',self.decision(),self.clock)

    def test_expired_material_not_revived_by_late_publication(self):
        run,f,m=self.ready();self.clock+=25*flow.HOUR;before=self.snapshot()['revision']
        self.assertEqual(producer.publish_pending(self.store,'main',self.clock)['published'],0)
        self.assertEqual(self.snapshot()['revision'],before)

    def test_old_ready_backlog_does_not_starve_new_fresh_material(self):
        self.enable()
        flow.configure(self.store,'main',{'search_interval_ms':1,'search_daily_limit':10},self.clock)
        for i in range(3):
            run=self.claim();self.clock+=1000
            fact,material=self.note(url='https://example.test/old/'+str(i),canonical='old:'+str(i))
            material['subject_key']=run['task']['subject_key']
            self.finish(run,'success',facts=[fact],materials=[material])
        self.clock+=25*flow.HOUR
        run=self.claim();self.clock+=1000
        fact,material=self.note(url='https://example.test/fresh',canonical='fresh')
        material['subject_key']=run['task']['subject_key']
        self.finish(run,'success',facts=[fact],materials=[material])
        self.assertEqual(producer.publish_pending(self.store,'main',self.clock)['published'],1)
        self.assertEqual(len(legacy.materials(self.store,'main',self.clock)['materials']),1)

    def test_cross_database_ack_retry_does_not_import_twice(self):
        run,f,m=self.ready();before=self.snapshot()['revision']
        original=producer.inbox;calls=0
        def flaky(path):
            nonlocal calls
            calls+=1
            if calls==2:raise OSError('ack failure')
            return original(path)
        with patch.object(producer,'inbox',side_effect=flaky):
            self.assertTrue(producer.publish_pending(self.store,'main',self.clock)['deferred'])
        self.assertEqual(self.snapshot()['revision'],before+1)
        self.assertEqual(producer.publish_pending(self.store,'main',self.clock)['published'],1)
        self.assertEqual(self.snapshot()['revision'],before+1)
        self.assertEqual(len(legacy.materials(self.store,'main',self.clock)['materials']),1)

    def test_publication_does_not_wait_on_main_database_lock(self):
        self.ready()
        with self.store.connect(True) as held:
            result=producer.publish_pending(self.store,'main',self.clock)
            self.assertTrue(result['deferred'])
        self.assertEqual(producer.publish_pending(self.store,'main',self.clock)['published'],1)

    def test_rotation_never_exports_private_topic_bodies(self):
        self.enable()
        self.store.apply('main',self.decision(facts=[{'id':'private','source':'simulated',
            'summary':'topic_state:diary private','details':{'text':'do not export this'}}]),self.clock)
        run=self.claim()
        self.assertNotIn('private',json.dumps(run['task']))
        self.finish(run);self.clock+=6*flow.HOUR
        next_run=self.claim()
        self.assertNotEqual(run['task']['subject_key'],next_run['task']['subject_key'])

    def test_inspect_and_events_never_expose_capability_token(self):
        self.enable();run=self.claim()
        self.assertNotIn(run['token'],json.dumps(producer.inspect(self.store,'main',self.clock)))

    def test_worker_adapter_missing_or_failure_does_not_change_reply(self):
        self.enable();before=self.snapshot()['revision'];cfg={'db':str(self.path)}
        self.assertFalse(worker.run(cfg,lambda:self.clock)['claimed'])
        cfg['topic_search']={'worker_argv':['fake']}
        with patch.object(worker,'invoke',side_effect=OSError('no adapter')):
            self.assertEqual(worker.run(cfg,lambda:self.clock)['status'],'failed')
        self.assertEqual(self.snapshot()['revision'],before)

    def test_worker_protocol_and_timeout_use_mock_commands(self):
        for status in ('no_candidates','timeout'):
            code='import sys,json;json.load(sys.stdin);print(json.dumps({"status":"no_candidates"}))' if status=='no_candidates' else 'import time;time.sleep(1)'
            self.assertEqual(worker.invoke([sys.executable,'-c',code],{},.05 if status=='timeout' else 1)['status'],status)

    def test_two_real_producer_processes_reserve_only_one_run(self):
        self.enable();before=self.snapshot()['revision']
        command=[sys.executable,str(baseline.baseline.ROOT/'ctl.py'),'--db',str(self.path),'topic-producer-claim']
        processes=[subprocess.Popen(command,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True) for _ in range(2)]
        results=[]
        for proc in processes:
            stdout,stderr=proc.communicate(timeout=10)
            self.assertEqual(proc.returncode,0,stdout+stderr)
            results.append(json.loads(stdout))
        self.assertEqual(sum(r['claimed'] for r in results),1)
        self.assertEqual(len(producer.inspect(self.store,'main',self.clock)['runs']),1)
        self.assertEqual(self.snapshot()['revision'],before)

    def test_worker_round_trip_valid_source_to_ready_then_publication(self):
        self.enable();before=self.snapshot()['revision']
        adapter=Path(self.tmp.name)/'mock_search.py'
        adapter.write_text("""import json,sys
r=json.load(sys.stdin)
assert 'token' not in r
fact={'id':'adapter-fact','source':'observed','summary':'ref:adapter',
      'details':{'evidence':'mock body verified','url':'https://example.test/adapter',
                 'publisher':'Mock','body_verified':True,'checked_ms':r['started_ms'],
                 'published_ms':None,'event_ms':None,'point_keys':['announcement']}}
material={'canonical_key':'adapter:event','subject_key':r['task']['subject_key'],'kind':'news',
          'fact_ids':['adapter-fact'],'checked_ms':r['started_ms'],'refresh_after_ms':r['started_ms']+3600000}
print(json.dumps({'status':'success','facts':[fact],'materials':[material]}))
""")
        output=worker.run({'db':str(self.path),'topic_search':{'worker_argv':[sys.executable,str(adapter)]}},lambda:self.clock)
        self.assertEqual(output['status'],'ready')
        self.assertNotIn('token',output)
        self.assertEqual(self.snapshot()['revision'],before)
        self.assertEqual(producer.publish_pending(self.store,'main',self.clock)['published'],1)

    def test_refresh_cannot_monopolize_every_search(self):
        run,f,m=self.ready();producer.publish_pending(self.store,'main',self.clock)
        self.clock+=25*flow.HOUR
        refresh=self.claim();self.assertEqual(refresh['task']['strategy'],'refresh')
        self.finish(refresh);self.clock+=6*flow.HOUR
        rotate=self.claim();self.assertEqual(rotate['task']['strategy'],'rotate')
        self.assertNotEqual(rotate['task']['subject_key'],refresh['task']['subject_key'])

    def test_topic_rotation_is_not_a_rejection_gate_for_valid_other_news(self):
        self.enable();run=self.claim();self.clock+=1000
        fact,material=self.note();material['subject_key']='public:new-interest'
        self.finish(run,'success',facts=[fact],materials=[material])
        self.assertEqual(producer.publish_pending(self.store,'main',self.clock)['published'],1)

    def test_broken_inbox_diagnostics_do_not_hide_published_materials(self):
        self.ready();producer.publish_pending(self.store,'main',self.clock)
        producer.inbox_path(self.store.path).write_bytes(b'broken optional inbox')
        result=legacy.materials(self.store,'main',self.clock)
        self.assertEqual(len(result['materials']),1)
        self.assertTrue(result['search']['diagnostics_unavailable'])

    def test_cli_claim_result_and_inspect(self):
        self.enable()
        command=[sys.executable,str(baseline.baseline.ROOT/'ctl.py'),'--db',str(self.path)]
        def call(*args):
            output=subprocess.run(command+list(args),capture_output=True,text=True)
            self.assertEqual(output.returncode,0,output.stdout+output.stderr)
            return json.loads(output.stdout)
        run=call('topic-producer-claim');self.assertTrue(run['claimed'])
        doc={'run_id':run['run_id'],'token':run['token'],'status':'no_candidates'}
        self.assertEqual(call('topic-producer-result','--result',json.dumps(doc))['status'],'no_candidates')
        self.assertEqual(call('topic-producer-inspect')['runs'][0]['status'],'no_candidates')


if __name__=='__main__':unittest.main()
