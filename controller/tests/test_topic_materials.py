"""No HTTP: provider outputs, budgets and freshness use a fake clock and SQLite."""
import unittest
import json
import subprocess
import sys
from pathlib import Path

import test_controller as baseline
from common import ControlError, now_ms
from store import Store
import conversation_flow as flow
import topic_materials as topics


class TopicMaterialsTests(unittest.TestCase):
    tearDown = baseline.ControllerTests.tearDown
    snapshot = baseline.ControllerTests.snapshot
    decision = baseline.ControllerTests.decision
    review = baseline.ControllerTests.review
    ingest = baseline.ControllerTests.ingest
    activity = baseline.ControllerTests.activity

    def setUp(self):
        baseline.ControllerTests.setUp(self)
        self.store.apply('main',self.decision(activities=[self.activity('rest',attention='free',
            end=self.clock+3*24*flow.HOUR)]),self.clock)
        self.serial=0

    def enable(self):
        flow.configure(self.store,'main',{'search_enabled':True,'search_tool_verified':True},self.clock)

    def note(self, url='https://example.test/news/1', canonical='event:demo', developing=False):
        self.serial+=1; fid='news-'+str(self.serial)
        fact={'id':fid,'source':'observed','summary':'ref:test-news-'+str(self.serial),
              'occurred_ms':self.clock,'details':{'evidence':'mock source inspection; not a real news report',
                'url':url,'publisher':'Mock source','published_ms':self.clock-1000,'event_ms':None,
                'checked_ms':self.clock,'body_verified':True,'developing':developing,'point_keys':['announcement']}}
        material={'canonical_key':canonical,'subject_key':'games:korea','kind':'news','fact_ids':[fid],
                  'checked_ms':self.clock,'refresh_after_ms':self.clock+(flow.HOUR if developing else 24*flow.HOUR)}
        return fact,material

    def test_search_default_off_and_verification_is_required(self):
        with self.assertRaisesRegex(ControlError,'topic_search_disabled'):
            topics.claim(self.store,'main',self.clock)
        with self.assertRaisesRegex(ControlError,'topic_search_tool_unverified'):
            flow.configure(self.store,'main',{'search_enabled':True},self.clock)

    def test_claim_does_not_add_decision_wakeup_or_change_life_time(self):
        self.enable(); before=self.snapshot()
        claim=topics.claim(self.store,'main',self.clock)
        after=self.snapshot()
        self.assertEqual(after['needs_decision'],before['needs_decision'])
        self.assertEqual(after['state']['next_decision_ms'],before['state']['next_decision_ms'])
        self.assertEqual(after['state']['advanced_to_ms'],before['state']['advanced_to_ms'])
        self.assertEqual(claim['deadline_ms'],self.clock+180000)

    def test_search_budget_accepts_180_seconds_and_rejects_larger_without_mutation(self):
        self.enable()
        configured=flow.configure(self.store,'main',{'search_limit_ms':180000},self.clock)
        self.assertEqual(configured['settings']['search_limit_ms'],180000)
        before=self.snapshot()['revision']
        with self.assertRaisesRegex(ControlError,'invalid_conversation_budget'):
            flow.configure(self.store,'main',{'search_limit_ms':180001},self.clock)
        self.assertEqual(self.snapshot()['revision'],before)
        claim=topics.claim(self.store,'main',self.clock)
        self.assertEqual(claim['deadline_ms']-self.clock,180000)

    def test_explicit_shorter_budget_is_preserved_until_operator_changes_it(self):
        self.enable()
        flow.configure(self.store,'main',{'search_limit_ms':60000},self.clock)
        updated=flow.configure(self.store,'main',{'mode':'shadow'},self.clock)
        self.assertEqual(updated['settings']['search_limit_ms'],60000)
        claim=topics.claim(self.store,'main',self.clock)
        self.assertEqual(claim['deadline_ms']-self.clock,60000)

    def test_claim_rejects_pending_incoming_and_unavailable_activity(self):
        self.enable(); self.ingest()
        with self.assertRaisesRegex(ControlError,'topic_search_incoming_pending'):
            topics.claim(self.store,'main',self.clock)
        self.store.apply('main',self.decision(dm_decisions=[{
            'action':'no_reply','message_ids':['m1'],'reason':'test disposition'}]),self.clock)
        self.clock+=1000
        self.store.apply('main',self.decision(activities=[
            {'id':'rest','status':'interrupted','actual_end_ms':self.clock,'reason':'end'},
            self.activity('sleep',attention='unavailable')]),self.clock)
        with self.assertRaisesRegex(ControlError,'topic_search_activity_unavailable'):
            topics.claim(self.store,'main',self.clock)

    def test_claim_rejects_live_conversation_lease(self):
        self.enable()
        with self.store.connect(True) as db:
            db.execute('INSERT INTO conversation_leases(session_id,until_ms,started_ms) VALUES (?,?,?)',
                       ('main',self.clock+60000,self.clock))
        with self.assertRaisesRegex(ControlError,'topic_search_conversation_active'):
            topics.claim(self.store,'main',self.clock)

    def test_failure_and_empty_results_consume_attempts_and_cooldown(self):
        self.enable(); first=topics.claim(self.store,'main',self.clock)
        topics.result(self.store,'main',{'run_id':first['run_id'],'token':first['token'],'status':'failed'},self.clock)
        with self.assertRaisesRegex(ControlError,'topic_search_cooldown'):
            topics.claim(self.store,'main',self.clock+1000)
        self.clock+=6*flow.HOUR
        second=topics.claim(self.store,'main',self.clock)
        self.assertEqual(second['attempts_today'],2)
        topics.result(self.store,'main',{'run_id':second['run_id'],'token':second['token'],'status':'no_candidates'},self.clock)
        with self.store.connect() as db:
            day,used,_=topics.search_state(db,'main',self.clock)
        self.assertEqual(used,2)

    def test_simultaneous_claim_and_late_result_cannot_duplicate_work(self):
        self.enable(); run=topics.claim(self.store,'main',self.clock)
        with self.assertRaisesRegex(ControlError,'topic_search_already_active'):
            topics.claim(self.store,'main',self.clock)
        self.clock=run['deadline_ms']+1
        with self.assertRaisesRegex(ControlError,'topic_search_result_expired'):
            topics.result(self.store,'main',{'run_id':run['run_id'],'token':run['token'],'status':'failed'},self.clock)
        with self.assertRaisesRegex(ControlError,'topic_search_cooldown'):
            topics.claim(self.store,'main',self.clock)

    def test_success_creates_only_observed_candidates_and_is_idempotent(self):
        self.enable(); run=topics.claim(self.store,'main',self.clock)
        self.clock+=1000; fact,material=self.note()
        doc={'run_id':run['run_id'],'token':run['token'],'status':'success','facts':[fact],'materials':[material]}
        first=topics.result(self.store,'main',doc,self.clock)
        again=topics.result(self.store,'main',doc,self.clock)
        self.assertEqual(first['material_ids'],again['material_ids'])
        self.assertEqual(len(topics.materials(self.store,'main',self.clock)['materials']),1)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM conversation_uses').fetchone()[0],0)
        doc['status']='failed'
        with self.assertRaisesRegex(ControlError,'topic_search_result_collision'):
            topics.result(self.store,'main',doc,self.clock)

    def test_title_and_tracking_url_changes_resolve_to_same_material(self):
        one,first=self.note('https://example.test/news/1?article=1&utm_source=a','title-a')
        result=self.store.apply('main',self.decision(facts=[one],conversation_updates=[
            {'op':'register_material','ref':'m',**first}]),self.clock)
        two,second=self.note('https://example.test/news/1?utm_source=b&article=1#top','title-b')
        same=self.store.apply('main',self.decision(facts=[two],conversation_updates=[
            {'op':'register_material','ref':'m',**second}]),self.clock)
        self.assertEqual(result['conversation_refs']['m'],same['conversation_refs']['m'])

    def test_body_and_source_times_are_required(self):
        fact,material=self.note(); fact['details']['body_verified']=False
        with self.assertRaisesRegex(ControlError,'topic_source_body_unverified'):
            self.store.apply('main',self.decision(facts=[fact],conversation_updates=[
                {'op':'register_material',**material}]),self.clock)
        with self.store.connect() as db:
            self.assertIsNone(db.execute('SELECT 1 FROM facts WHERE id=?',(fact['id'],)).fetchone())

    def test_source_url_paths_remain_case_sensitive(self):
        one,first=self.note('https://EXAMPLE.test/News/A','upper-path')
        a=self.store.apply('main',self.decision(facts=[one],conversation_updates=[
            {'op':'register_material','ref':'m',**first}]),self.clock)
        two,second=self.note('https://example.test/news/a','lower-path')
        b=self.store.apply('main',self.decision(facts=[two],conversation_updates=[
            {'op':'register_material','ref':'m',**second}]),self.clock)
        self.assertNotEqual(a['conversation_refs']['m'],b['conversation_refs']['m'])

    def test_corrected_source_does_not_refresh_an_obsolete_point(self):
        flow.configure(self.store,'main',{'mode':'enforce'},self.clock)
        fact,material=self.note()
        initial=self.store.apply('main',self.decision(facts=[fact],conversation_updates=[
            {'op':'register_material','ref':'m',**material},
            {'op':'register_unit','ref':'u','material_id':'@m','subject_key':'games:korea',
             'kind':'statement','facet_key':'announcement','scope_key':'stable','summary':'old point',
             'fact_ids':[fact['id']],'point_keys':['announcement']}]),self.clock)
        self.clock+=1000
        corrected,renewed=self.note(); corrected['details']['point_keys']=['correction']
        self.store.apply('main',self.decision(facts=[corrected],conversation_updates=[
            {'op':'register_material',**renewed}]),self.clock)
        self.ingest()
        dm={'action':'reply','message_ids':['m1'],'group_id':'g','parts':['옛 소식'],
            'due_ms':self.clock,'reason':'r','conversation_plan':{'version':1,'units':[
                {'unit_id':initial['conversation_refs']['u'],'mode':'introduce','basis':{},
                 'spans':[{'part_index':0,'excerpt':'옛 소식'}]}]}}
        with self.assertRaisesRegex(ControlError,'conversation_point_superseded'):
            self.store.apply('main',self.decision(dm_decisions=[dm]),self.clock)

    def test_developing_news_requires_short_refresh_and_expired_is_filtered(self):
        fact,material=self.note(developing=True)
        material['refresh_after_ms']=self.clock+24*flow.HOUR
        with self.assertRaisesRegex(ControlError,'topic_developing_refresh_required'):
            self.store.apply('main',self.decision(facts=[fact],conversation_updates=[
                {'op':'register_material',**material}]),self.clock)
        material['refresh_after_ms']=self.clock+flow.HOUR
        self.store.apply('main',self.decision(facts=[fact],conversation_updates=[
            {'op':'register_material',**material}]),self.clock)
        self.clock+=flow.HOUR
        self.assertTrue(topics.materials(self.store,'main',self.clock)['materials'][0]['needs_refresh'])

    def test_stale_news_cannot_be_introduced_via_direct_apply(self):
        flow.configure(self.store,'main',{'mode':'enforce'},self.clock)
        fact,material=self.note()
        result=self.store.apply('main',self.decision(facts=[fact],conversation_updates=[
            {'op':'register_material','ref':'m',**material},
            {'op':'register_unit','ref':'u','material_id':'@m','subject_key':'games:korea',
             'kind':'statement','facet_key':'announcement','scope_key':'stable','summary':'announcement',
             'fact_ids':[fact['id']],'point_keys':['announcement']}]),self.clock)
        self.clock+=24*flow.HOUR; self.ingest()
        dm={'action':'reply','message_ids':['m1'],'group_id':'g','parts':['소식'],
            'due_ms':self.clock,'reason':'r','conversation_plan':{'version':1,'units':[
                {'unit_id':result['conversation_refs']['u'],'mode':'introduce','basis':{},
                 'spans':[{'part_index':0,'excerpt':'소식'}]}]}}
        with self.assertRaisesRegex(ControlError,'conversation_material_stale'):
            self.store.apply('main',self.decision(dm_decisions=[dm]),self.clock)

    def test_failure_cannot_be_recorded_as_fictional_material(self):
        self.enable(); run=topics.claim(self.store,'main',self.clock)
        fact,material=self.note()
        with self.assertRaisesRegex(ControlError,'topic_search_failure_has_materials'):
            topics.result(self.store,'main',{'run_id':run['run_id'],'token':run['token'],'status':'failed',
                'facts':[fact],'materials':[material]},self.clock)

    def test_kst_day_budget_and_interval_are_both_enforced(self):
        self.enable()
        # A one-attempt budget is deterministic regardless of baseline's hour.
        flow.configure(self.store,'main',{'search_daily_limit':1},self.clock)
        run=topics.claim(self.store,'main',self.clock)
        topics.result(self.store,'main',{'run_id':run['run_id'],'token':run['token'],'status':'no_candidates'},self.clock)
        with self.assertRaisesRegex(ControlError,'topic_search_daily_budget'):
            topics.claim(self.store,'main',self.clock+1000)
        with self.store.connect() as db:
            day,used,_=topics.search_state(db,'main',self.clock+24*flow.HOUR)
        self.assertEqual(used,0)

    def test_public_cli_configure_context_claim_and_empty_result(self):
        now=now_ms(); path=self.path.parent/'cli.db'; store=Store(path); store.seed({},now)
        store.apply('main',{'decision_id':'rest','expected_revision':0,'advance_to_ms':now,
            'next_decision_ms':now+3600000,'activities':[self.activity('cli-rest',start=now,end=now+3600000)]},now)
        command=[sys.executable,str(Path(baseline.ROOT)/'ctl.py'),'--db',str(path)]
        def run(*args):
            out=subprocess.run(command+list(args),capture_output=True,text=True)
            self.assertEqual(out.returncode,0,out.stdout+out.stderr)
            return json.loads(out.stdout)
        run('conversation-configure','--settings',json.dumps({'mode':'shadow','search_enabled':True,'search_tool_verified':True}))
        self.assertEqual(run('conversation-context')['settings']['mode'],'shadow')
        self.assertEqual(run('topic-materials')['materials'],[])
        claim=run('topic-search-claim')
        doc={'run_id':claim['run_id'],'token':claim['token'],'status':'no_candidates'}
        saved=run('topic-search-result','--result',json.dumps(doc))
        again=run('topic-search-result','--result',json.dumps(doc))
        self.assertEqual(again['revision'],saved['revision'])


if __name__ == '__main__':
    unittest.main()
