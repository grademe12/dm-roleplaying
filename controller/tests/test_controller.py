import json
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from common import ControlError, file_lock
from dispatch import Dispatcher, ExternalTransport, MockTransport
from store import Store
from migrate_parts import migrate
import persona

# Shared-core suites assert the reference persona's switches. test_persona.py covers this
# repository's own values (persona.py) on a fresh module.
REFERENCE_PERSONA = {'NIGHT_QUIET_UNTIL_HOUR': 7, 'MORNING_CONTACT': True, 'DEFAULT_CHANNEL': 'instagram'}
for _name, _value in REFERENCE_PERSONA.items():
    setattr(persona, _name, _value)

T=1791946800000  # daytime KST, deterministic tests


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=ROOT/'tests')
        self.path=Path(self.tmp.name)/'test.db'
        self.store=Store(self.path)
        self.store.seed({},T)
        self.clock=T
        self.sleeps=[]
        def fake_sleep(seconds):
            self.sleeps.append(seconds); self.clock+=int(seconds*1000)
        self.dispatch=Dispatcher(self.store,lambda:self.clock,fake_sleep)

    def tearDown(self): self.tmp.cleanup()

    def snapshot(self): return self.store.tick('main',self.clock)

    def ingest(self,mid='m1',**kwargs):
        m={'id':mid,'text':'hello','observed_ms':self.clock,'direction':'incoming',
           'observation':{'identity':'platform'}}
        m.update(kwargs)
        return self.store.ingest('main',{'read_ok':True,'messages':[m]},self.clock)

    def decision(self,**kwargs):
        snap=self.snapshot()
        d={'decision_id':'d'+str(snap['revision']), 'expected_revision':snap['revision'],
           'advance_to_ms':self.clock,'next_decision_ms':self.clock+3600000}
        d.update(kwargs)
        for dm in d.get('dm_decisions',[]):
            if dm.get('action') in ('reply','initiate') and 'parts' in dm and 'conversation_review' not in dm:
                dm['conversation_review']=self.review(dm)
            elif dm.get('action') in ('revalidate_group','retry_failed_image') and 'conversation_review' not in dm:
                with self.store.connect() as db:
                    jobs=list(db.execute('SELECT text FROM dm_jobs WHERE session_id=? AND group_id=? ORDER BY part_index',
                                         ('main',dm['group_id'])))
                    ids=[r['message_id'] for r in db.execute(
                        'SELECT message_id FROM reply_targets WHERE session_id=? AND group_id=? ORDER BY message_id',
                        ('main',dm['group_id']))]
                    channel=db.execute('SELECT channel FROM group_channels WHERE session_id=? AND group_id=?',
                                       ('main',dm['group_id'])).fetchone()['channel']
                dm['conversation_review']=self.review({'parts':[j['text'] for j in jobs],
                                                        'message_ids':ids,'channel':channel})
        return d

    def review(self, dm):
        ids=dm.get('message_ids',[])
        channel=dm.get('channel','slack' if ids and ids[0].startswith('s') else 'instagram')
        return {'context':{'channel':channel,'recent_message_ids':ids,'target_message_ids':ids,
                           'previous_self':None,'user_uptake':'The user wrote the target message.' if ids else None,
                           'topic':'current exchange'},
                'memory':{'kind':'general','topic_state_key':None,'result':'not_applicable'},
                'draft_parts':dm['parts'],
                'checks':{**{name:{'passed':True,'evidence':'Checked against the current exchange.'}
                             for name in ('self_reset','missed_answer','conflict')},
                          'self_consistency':{'passed':True,'evidence':'No new lasting self claim.',
                                              'claims':[]}}}

    def reply(self,parts=None,**kwargs):
        dm={'action':'reply','message_ids':['m1'],'group_id':'g1','due_ms':self.clock,
            'parts':['reply'] if parts is None else parts,'reason':'reply to observed message'}
        dm.update(kwargs)
        d=self.decision(dm_decisions=[dm],processed_event_ids=['dm:m1'])
        self.store.apply('main',d,self.clock)
        return d

    def activity(self,aid='a1',start=None,end=None,location='home',**kwargs):
        result={'id':aid,'category':'rest','label':'휴식','start_ms':start or self.clock,
                'planned_end_ms':end or self.clock+3600000,'location':location,
                'details':{'reason':'rest','rates_per_hour':{'fatigue':-10}}}
        result.update(kwargs); return result

    def test_duplicate_ingest_decision_and_reply(self):
        self.ingest(); self.ingest()
        self.assertEqual(len(self.snapshot()['events']),1)
        d=self.reply()
        self.assertEqual(self.store.apply('main',d,self.clock)['reason'],'already_applied')
        with self.assertRaises(ControlError): self.reply(group_id='other')
        self.assertEqual(len(self.snapshot()['jobs']),1)

    def test_conversation_review_blocks_unchecked_or_changed_drafts(self):
        self.ingest()
        dm={'action':'reply','message_ids':['m1'],'group_id':'g1','due_ms':self.clock,
            'parts':['reply'],'reason':'r'}
        decision=self.decision(dm_decisions=[dm])
        review=dm.pop('conversation_review')
        with self.assertRaisesRegex(ControlError,'missing_conversation_review'):
            self.store.apply('main',decision,self.clock)
        self.assertEqual(self.snapshot()['jobs'],[])
        dm['conversation_review']=review
        dm['parts']=['changed after review']
        with self.assertRaisesRegex(ControlError,'review_draft_mismatch'):
            self.store.apply('main',decision,self.clock)
        dm['parts']=['reply']
        review['checks']['self_reset']['passed']=False
        with self.assertRaisesRegex(ControlError,'conversation_review_failed'):
            self.store.apply('main',decision,self.clock)
        review['checks']['self_reset']['passed']=True
        review['context']['target_message_ids']=['other']
        with self.assertRaisesRegex(ControlError,'review_target_mismatch'):
            self.store.apply('main',decision,self.clock)
        review['context']['target_message_ids']=['m1']
        self.assertEqual(self.store.apply('main',decision,self.clock)['reason'],'applied')

    def test_conversation_review_requires_topic_lookup_record(self):
        self.ingest()
        dm={'action':'reply','message_ids':['m1'],'group_id':'g1','due_ms':self.clock,
            'parts':['reply'],'reason':'r'}
        d=self.decision(dm_decisions=[dm])
        dm['conversation_review']['memory']={'kind':'hobby','topic_state_key':'topic_state:book','result':'not_applicable'}
        with self.assertRaisesRegex(ControlError,'missing_topic_state_lookup'):
            self.store.apply('main',d,self.clock)
        dm['conversation_review']['memory']['result']='missing'
        self.assertEqual(self.store.apply('main',d,self.clock)['reason'],'applied')

    def test_self_consistency_claim_requires_a_recorded_fact(self):
        self.ingest()
        dm={'action':'reply','message_ids':['m1'],'group_id':'g1','due_ms':self.clock,
            'parts':['저는 고등학교 때 코딩을 시작했어요'],'reason':'r'}
        d=self.decision(dm_decisions=[dm])
        check=dm['conversation_review']['checks']['self_consistency']
        check['claims']=[{'excerpt':'대학에서 코딩을 시작했어요','fact_id':'identity:education'}]
        with self.assertRaisesRegex(ControlError,'self_claim_not_in_draft'):
            self.store.apply('main',d,self.clock)
        check['claims'][0]['excerpt']='고등학교 때 코딩을 시작했어요'
        with self.assertRaisesRegex(ControlError,'self_claim_fact_missing'):
            self.store.apply('main',d,self.clock)
        d['facts']=[{'id':'identity:education','source':'canon','summary':'identity:education 고등학교 코딩',
                     'details':{'evidence':'character profile','path':'high school to bootcamp'}}]
        self.assertEqual(self.store.apply('main',d,self.clock)['reason'],'applied')

    def test_only_confirmed_text_becomes_searchable_self_utterance(self):
        self.ingest()
        dm={'action':'reply','message_ids':['m1'],'group_id':'g1','due_ms':self.clock,
            'parts':['저는 고등학교 때 코딩을 시작했어요'],'reason':'r'}
        d=self.decision(dm_decisions=[dm],processed_event_ids=['dm:m1'],
            facts=[{'id':'identity:education','source':'canon','summary':'identity:education 학력',
                    'details':{'evidence':'character profile'}}])
        dm['conversation_review']['checks']['self_consistency']['claims']=[
            {'excerpt':'고등학교 때 코딩을 시작했어요','fact_id':'identity:education'}]
        self.store.apply('main',d,self.clock)
        self.assertEqual(self.store.recall('main',['고등학교 때 코딩'])['facts'],[])
        self.dispatch.send_due('main',MockTransport())
        utterances=self.store.recall('main',['고등학교 때 코딩'])['facts']
        self.assertEqual(len(utterances),1)
        self.assertEqual(utterances[0]['id'],'self-said:g1:0:0')
        self.assertEqual(utterances[0]['source'],'observed')

    def test_reconciled_text_is_recorded_only_after_confirmation(self):
        self.ingest()
        dm={'action':'reply','message_ids':['m1'],'group_id':'g1','due_ms':self.clock,
            'parts':['부트캠프에서 프론트엔드를 배웠어요'],'reason':'r'}
        d=self.decision(dm_decisions=[dm],processed_event_ids=['dm:m1'],
            facts=[{'id':'identity:education','source':'canon','summary':'identity:education 학력',
                    'details':{'evidence':'character profile'}}])
        dm['conversation_review']['checks']['self_consistency']['claims']=[
            {'excerpt':'부트캠프에서 프론트엔드를 배웠어요','fact_id':'identity:education'}]
        self.store.apply('main',d,self.clock)
        self.dispatch.send_due('main',MockTransport('submitted'))
        self.assertEqual(self.store.recall('main',['부트캠프에서'])['facts'],[])
        job=self.snapshot()['jobs'][0]
        self.dispatch.reconcile('main',{'expected_revision':self.snapshot()['revision'],
            'job_id':job['id'],'attempt_token':job['attempt_token'],
            'result':{'status':'confirmed','provider_message_id':'provider-1',
                      'evidence':'confirmed by provider lookup'}})
        facts=self.store.recall('main',['부트캠프에서'])['facts']
        self.assertEqual([fact['id'] for fact in facts],['self-said:g1:0:0'])

    def test_revision_conflict_and_atomic_rollback(self):
        d=self.decision(activities=[self.activity()])
        self.store.apply('main',self.decision(decision_id='other-change'),self.clock)
        with self.assertRaisesRegex(ControlError,'revision_conflict'): self.store.apply('main',d,self.clock)
        self.assertIsNone(self.snapshot()['activity'])
        d=self.decision(activities=[self.activity()],dm_decisions=[{'action':'reply','message_ids':['missing']}])
        with self.assertRaises(ControlError): self.store.apply('main',d,self.clock)
        self.assertIsNone(self.snapshot()['activity'])

    def test_activity_rates_once_restart_and_overlap(self):
        self.store.apply('main',self.decision(activities=[self.activity()]),self.clock)
        self.clock+=1800000
        d=self.decision()
        self.store.apply('main',d,self.clock)
        self.assertEqual(self.snapshot()['state']['fatigue'],25)
        self.store.apply('main',d,self.clock)
        self.assertEqual(Store(self.path).tick('main',self.clock)['state']['fatigue'],25)
        with self.assertRaises((ControlError,sqlite3.IntegrityError)):
            self.store.apply('main',self.decision(activities=[self.activity('a2')]),self.clock)

    def test_travel_and_time_reversal(self):
        self.store.apply('main',self.decision(activities=[self.activity()]),self.clock)
        self.clock+=3600000
        d=self.decision(activities=[{'id':'a1','status':'completed','actual_end_ms':self.clock,'reason':'done'},self.activity('office',location='office')])
        with self.assertRaisesRegex(ControlError,'missing_travel'): self.store.apply('main',d,self.clock)
        d=self.decision(activities=[{'id':'a1','status':'completed','actual_end_ms':self.clock,'reason':'done'},self.activity('travel',location='in_transit',destination='office')])
        self.store.apply('main',d,self.clock)
        with self.assertRaisesRegex(ControlError,'invalid_advance_time'):
            self.store.apply('main',self.decision(advance_to_ms=T),self.clock)

    def test_split_group_sent_in_one_burst(self):
        self.ingest(); self.reply(['one','two','a much longer third bubble '*3,'four','five'])
        result=self.dispatch.send_due('main',MockTransport())
        self.assertEqual([r['job_id'] for r in result['results']],[f'g1:{i}' for i in range(5)])
        self.assertEqual(result['reason'],'send_interval')
        self.assertEqual(len(self.sleeps),4)
        self.assertTrue(all(3<=s<=5 for s in self.sleeps))
        self.assertLessEqual(sum(self.sleeps),20)
        self.assertGreater(self.sleeps[1],self.sleeps[0])   # longer next bubble, longer pause
        snap=self.snapshot()
        self.assertEqual(snap['messages'],[])
        self.assertGreaterEqual(snap['channel_next_send_after_ms']['instagram']-self.clock,120000)
    def test_burst_stays_within_group(self):
        self.ingest(); self.reply(['one','two'])
        self.ingest('m2')
        self.store.apply('main',self.decision(dm_decisions=[
            {'action':'revalidate_group','group_id':'g1','reason':'still valid'},
            {'action':'reply','message_ids':['m2'],'group_id':'g2','due_ms':self.clock,'parts':['three'],'reason':'reply m2'}]),self.clock)
        result=self.dispatch.send_due('main',MockTransport())
        self.assertEqual([r['job_id'] for r in result['results']],['g1:0','g1:1'])
        self.assertEqual(self.dispatch.send_due('main',MockTransport())['reason'],'send_interval')
        self.clock+=600000
        self.assertEqual([r['job_id'] for r in self.dispatch.send_due('main',MockTransport())['results']],['g2:0'])
    def test_uncertain_part_does_not_stop_burst_or_replay(self):
        class FirstOnly:
            mode='mock'
            def send(self, job):
                if job['part_index']==0:
                    return {'status':'confirmed','provider_message_id':'p0','evidence':'mock'}
                return {'status':'unknown','evidence':'mock'}
        self.ingest(); self.reply(['one','two','three'])
        result=self.dispatch.send_due('main',FirstOnly())
        self.assertEqual([r['status'] for r in result['results']],['confirmed','unknown','unknown'])
        self.assertEqual(self.dispatch.send_due('main',MockTransport())['results'],[])
        self.clock+=600000
        self.assertEqual(self.dispatch.send_due('main',MockTransport())['results'],[])

    def test_submitted_allows_later_parts_and_keeps_receipt_reconciliation(self):
        self.ingest(); self.reply(['one','two'])
        result=self.dispatch.send_due('main',MockTransport('submitted'))
        self.assertEqual(result['results'][0]['status'],'submitted')
        self.clock+=600000
        self.assertEqual(self.dispatch.send_due('main',MockTransport())['results'],[])
        job=self.snapshot()['jobs'][0]
        self.dispatch.reconcile('main',{'expected_revision':self.snapshot()['revision'],
            'job_id':job['id'],'attempt_token':job['attempt_token'],
            'result':{'status':'confirmed','provider_message_id':'p1','evidence':'observed sent bubble'}})
        self.assertEqual(self.dispatch.send_due('main',MockTransport())['results'],[])

    def test_429_blocks(self):
        self.ingest(); self.reply(['one','two'])
        result=self.dispatch.send_due('main',MockTransport('429'))
        self.assertEqual(result['reason'],'rate_limited')
        self.assertEqual(self.snapshot()['state']['send_blocked_until_ms'],self.clock+3600000)
        self.assertEqual(self.dispatch.send_due('main',MockTransport())['results'],[])

    def test_stale_recovery_protects_live_owner(self):
        self.ingest(); self.reply()
        job,_=self.dispatch._claim('main','mock',self.clock)
        self.clock+=120001
        with file_lock(self.path,'send:main'):
            with self.assertRaisesRegex(ControlError,'busy'): self.dispatch.recover('main')
        self.assertEqual(self.dispatch.recover('main')['recovered_ids'],[job['id']])
        self.assertEqual(self.snapshot()['jobs'][0]['status'],'unknown')
        self.assertEqual(self.dispatch.send_due('main',MockTransport())['results'],[])

    def test_unexpired_lease_and_wrong_token(self):
        self.ingest(); self.reply()
        job,_=self.dispatch._claim('main','mock',self.clock)
        self.assertEqual(self.dispatch.recover('main')['recovered_ids'],[])
        job['attempt_token']='wrong'
        with self.assertRaisesRegex(ControlError,'attempt_token_conflict'):
            self.dispatch._record('main',job,MockTransport().send(job),self.clock)

    def test_new_message_invalidates_pending_draft(self):
        self.ingest(); self.reply(); self.ingest('m2')
        self.assertEqual(self.dispatch.send_due('main',MockTransport())['reason'],'draft_stale')
        d=self.decision(dm_decisions=[{'action':'revalidate_group','group_id':'g1','reason':'new message does not change draft'}])
        review=d['dm_decisions'][0].pop('conversation_review')
        with self.assertRaisesRegex(ControlError,'missing_conversation_review'):
            self.store.apply('main',d,self.clock)
        d['dm_decisions'][0]['conversation_review']=review
        self.store.apply('main',d,self.clock)
        self.assertEqual(len(self.dispatch.send_due('main',MockTransport())['results']),1)

    def test_deferred_due_never_advanced(self):
        self.ingest()
        self.store.apply('main',self.decision(dm_decisions=[{'action':'defer','message_ids':['m1'],'due_ms':self.clock+600000,'reason':'focused'}]),self.clock)
        with self.assertRaisesRegex(ControlError,'reply_due_advanced'): self.reply()
        self.reply(due_ms=self.clock+600000)
        self.assertEqual(self.dispatch.send_due('main',MockTransport())['results'],[])

    def initiate(self,gid='p1',parts=None,**kwargs):
        dm={'action':'initiate','group_id':gid,'due_ms':self.clock,'parts':['hi'] if parts is None else parts,'reason':'shareable event'}
        dm.update(kwargs)
        return self.store.apply('main',self.decision(dm_decisions=[dm]),self.clock)
    def test_initiate_sends_without_incoming_target(self):
        self.initiate()
        self.assertEqual(self.snapshot()['proactive_group_ids'],['p1'])
        with self.assertRaisesRegex(ControlError,'initiate_already_active'): self.initiate('p2')
        result=self.dispatch.send_due('main',MockTransport())
        self.assertEqual([r['status'] for r in result['results']],['confirmed'])
        snap=self.snapshot()
        self.assertEqual(snap['proactive_group_ids'],[])
        self.assertEqual(snap['jobs'],[])
        self.initiate('p2')   # previous group completed, a new one is allowed
    def test_reply_and_initiate_accept_one_to_five_parts(self):
        for action in ('reply', 'initiate'):
            for count in (1, 3, 4, 5):
                with self.subTest(action=action, count=count):
                    store = Store(Path(self.tmp.name)/f'{action}-{count}.db')
                    store.seed({}, self.clock)
                    dm = {'action': action, 'group_id': 'long', 'due_ms': self.clock,
                          'parts': [f'설명 {i}\n\n이어지는 문단 {i}' for i in range(count)],
                          'reason': 'Explain the cafe visit in detail'}
                    if action == 'reply':
                        store.ingest('main', {'read_ok': True, 'messages': [{
                            'id': 'm1', 'text': '카페 다녀온 이야기를 자세히 들려줘',
                            'direction': 'incoming', 'observation': {'identity': 'platform'}}]}, self.clock)
                        dm['message_ids'] = ['m1']
                    dm['conversation_review'] = self.review(dm)
                    snap = store.tick('main', self.clock)
                    store.apply('main', {'decision_id': 'long', 'expected_revision': snap['revision'],
                        'advance_to_ms': self.clock, 'next_decision_ms': self.clock+3600000,
                        'dm_decisions': [dm]}, self.clock)
                    with store.connect() as db:
                        rows = db.execute('SELECT part_index,text FROM dm_jobs ORDER BY part_index').fetchall()
                    self.assertEqual([r['part_index'] for r in rows], list(range(count)))
                    self.assertEqual([r['text'] for r in rows], dm['parts'])

    def test_invalid_parts_and_late_invalid_item_roll_back(self):
        for action in ('reply', 'initiate'):
            for index, parts in enumerate(([], None, 'text', {'text': 'text'}, ['valid']*4+[None])):
                with self.subTest(action=action, parts=parts):
                    store = Store(Path(self.tmp.name)/f'invalid-{action}-{index}.db')
                    store.seed({}, self.clock)
                    dm = {'action': action, 'group_id': 'bad', 'due_ms': self.clock,
                          'parts': parts, 'reason': 'invalid input'}
                    if action == 'reply':
                        store.ingest('main', {'read_ok': True, 'messages': [{
                            'id': 'm1', 'text': 'hello', 'direction': 'incoming',
                            'observation': {'identity': 'platform'}}]}, self.clock)
                        dm['message_ids'] = ['m1']
                    dm['conversation_review'] = self.review(dm)
                    before = store.tick('main', self.clock)
                    with self.assertRaisesRegex(ControlError, 'invalid_text' if isinstance(parts, list) and parts else 'invalid_parts'):
                        store.apply('main', {'decision_id': 'invalid', 'expected_revision': before['revision'],
                            'advance_to_ms': self.clock, 'next_decision_ms': self.clock+3600000,
                            'dm_decisions': [dm]}, self.clock)
                    self.assertEqual(store.tick('main', self.clock), before)
                    with store.connect() as db:
                        self.assertEqual(db.execute('SELECT COUNT(*) FROM reply_groups').fetchone()[0], 0)

    def test_long_group_keeps_text_order_and_pacing_across_batches(self):
        self.ingest()
        parts = [f'{i}: 첫 문단\n\n' + '카페의 분위기와 방문 과정을 설명합니다. '*30 for i in range(12)]
        # Simulate a group persisted by the previous uncapped writer. Upgrading
        # must not discard it, truncate it, or force an already-sent replay.
        with patch('store.MAX_PARTS',12):
            self.reply(parts)
        calls = []
        outer = self
        class RecordingTransport(MockTransport):
            def send(self, job):
                calls.append((job['id'], job['text'], outer.clock))
                return super().send(job)
        transport = RecordingTransport()
        for batch in range(3):
            result = self.dispatch.send_due('main', transport)
            self.assertEqual([r['job_id'] for r in result['results']],
                             [f'g1:{i}' for i in range(batch*5, min(batch*5+5,12))])
            if batch < 2:
                snap = self.snapshot()
                self.assertEqual(len(snap['jobs']), 12-(batch+1)*5)
                self.assertEqual(snap['messages'][0]['status'], 'queued')
                self.assertEqual(self.dispatch.send_due('main', transport)['results'], [])
                self.clock = max(snap['state']['next_send_after_ms'],
                                 snap['channel_next_send_after_ms']['instagram'])
        self.assertEqual([call[1] for call in calls], parts)
        self.assertEqual(len({call[0] for call in calls}), 12)
        self.assertTrue(all(a[2] < b[2] for a, b in zip(calls, calls[1:])))
        self.assertEqual(self.snapshot()['jobs'], [])
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT status FROM dm_messages WHERE id='m1'").fetchone()[0], 'handled')

    def test_long_partial_group_revalidates_in_order_without_replay(self):
        self.ingest()
        parts = [f'카페 방문 설명 {i}' for i in range(12)]
        with patch('store.MAX_PARTS',12):
            self.reply(parts)
        first = self.dispatch.send_due('main', MockTransport())
        self.assertEqual([r['job_id'] for r in first['results']], [f'g1:{i}' for i in range(5)])
        self.ingest('m2', text='계속 이야기해 줘')
        self.clock += 600000
        self.assertEqual(self.dispatch.send_due('main', MockTransport())['reason'], 'draft_stale')
        decision = self.decision(dm_decisions=[{'action': 'revalidate_group', 'group_id': 'g1',
                                              'reason': 'User wants the same explanation to continue'}])
        review = decision['dm_decisions'][0]['conversation_review']
        self.assertEqual(review['draft_parts'], parts)
        review['draft_parts'] = parts[:-1] + ['changed']
        with self.assertRaisesRegex(ControlError, 'review_draft_mismatch'):
            self.store.apply('main', decision, self.clock)
        review['draft_parts'] = parts
        self.store.apply('main', decision, self.clock)
        result = self.dispatch.send_due('main', MockTransport())
        self.assertEqual([r['job_id'] for r in result['results']], [f'g1:{i}' for i in range(5,10)])
        self.assertEqual(len(self.sleeps), 8)

    def test_long_partial_group_can_close_without_replaying_sent_parts(self):
        self.ingest()
        with patch('store.MAX_PARTS',7):
            self.reply([f'설명 {i}' for i in range(7)])
        self.dispatch.send_due('main', MockTransport())
        self.ingest('m2', text='설명은 이제 충분해')
        self.clock += 600000
        self.assertEqual(self.dispatch.send_due('main', MockTransport())['reason'], 'draft_stale')
        self.store.apply('main', self.decision(dm_decisions=[{
            'action': 'close_partial_group', 'group_id': 'g1', 'reason': 'User has enough detail'}]), self.clock)
        self.assertEqual(self.dispatch.send_due('main', MockTransport())['results'], [])
        with self.store.connect() as db:
            rows = db.execute('SELECT status FROM dm_jobs ORDER BY part_index').fetchall()
        self.assertEqual([r['status'] for r in rows], ['confirmed']*5 + ['cancelled']*2)

    def legacy_store_with_partial_reply(self):
        schema = Path(self.tmp.name)/'legacy-schema'
        schema.mkdir()
        (schema/'base.sql').write_text((ROOT/'schema/base.sql').read_text())
        (schema/'controller.sql').write_text((ROOT/'schema/controller.sql').read_text().replace(
            'CHECK(part_index >= 0)', 'CHECK(part_index BETWEEN 0 AND 2)'))
        self.store = Store(Path(self.tmp.name)/'legacy.db')
        with patch('store.SCHEMA', schema):
            self.store.seed({}, self.clock)
        self.dispatch = Dispatcher(self.store, lambda: self.clock, lambda _: None)
        self.ingest()
        self.reply(['first', 'second', 'third'])
        self.dispatch.send_due('main', MockTransport(), limit=1)

    def test_legacy_parts_migration_preserves_data_and_constraints(self):
        self.legacy_store_with_partial_reply()
        with self.store.connect() as db:
            tables = [r['name'] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
            before = {name: [tuple(r) for r in db.execute(f'SELECT * FROM "{name}" ORDER BY rowid')]
                      for name in tables}
            objects = [tuple(r) for r in db.execute("SELECT name,sql FROM sqlite_master WHERE "
                                                   "tbl_name='dm_jobs' AND type IN ('index','trigger') ORDER BY name")]
        backup = Path(self.tmp.name)/'before-migration.db'
        result = migrate(self.store.path, backup)
        self.assertEqual(result['preserved_jobs'], 3)
        with self.store.connect() as db:
            for name in tables:
                self.assertEqual([tuple(r) for r in db.execute(f'SELECT * FROM "{name}" ORDER BY rowid')], before[name])
            self.assertEqual([tuple(r) for r in db.execute("SELECT name,sql FROM sqlite_master WHERE "
                "tbl_name='dm_jobs' AND type IN ('index','trigger') ORDER BY name")], objects)
            self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(), [])
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
            self.assertEqual(db.execute('PRAGMA foreign_keys').fetchone()[0], 1)
        with closing(sqlite3.connect(backup)) as db:
            self.assertIn('BETWEEN 0 AND 2', db.execute("SELECT sql FROM sqlite_master WHERE name='dm_jobs'").fetchone()[0])
            self.assertEqual(db.execute('SELECT COUNT(*) FROM dm_jobs').fetchone()[0], 3)
        self.assertEqual(migrate(self.store.path, backup)['reason'], 'already_migrated')
        # Existing immutable identity trigger and unique index still enforce their contracts.
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'immutable_job_identity'):
            with self.store.connect(True) as db:
                db.execute("UPDATE dm_jobs SET part_index=10 WHERE id='g1:1'")
        self.store.apply('main', self.decision(dm_decisions=[{
            'action': 'close_partial_group', 'group_id': 'g1', 'reason': 'replace remaining explanation'}]), self.clock)
        self.initiate(parts=[f'New explanation {i}' for i in range(5)])

    def test_failed_legacy_parts_migration_rolls_back(self):
        self.legacy_store_with_partial_reply()
        before = self.snapshot()
        from common import require as actual_require
        def fail_after_rebuild(condition, reason):
            actual_require(False if reason == 'migration_data_mismatch' else condition, reason)
        with patch('migrate_parts.require', side_effect=fail_after_rebuild):
            with self.assertRaisesRegex(ControlError, 'migration_data_mismatch'):
                migrate(self.store.path, Path(self.tmp.name)/'rollback-backup.db')
        self.assertEqual(self.snapshot(), before)
        with self.store.connect() as db:
            self.assertIn('BETWEEN 0 AND 2', db.execute("SELECT sql FROM sqlite_master WHERE name='dm_jobs'").fetchone()[0])
            self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(), [])
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='dm_jobs_parts_migration'").fetchone())
        with self.assertRaisesRegex(ControlError, 'backup_path_exists'):
            migrate(self.store.path, Path(self.tmp.name)/'rollback-backup.db')

    def test_legacy_database_requires_explicit_migration_for_long_groups(self):
        self.legacy_store_with_partial_reply()
        self.store.apply('main', self.decision(dm_decisions=[{
            'action': 'close_partial_group', 'group_id': 'g1', 'reason': 'finished'}]), self.clock)
        before = self.snapshot()
        with self.assertRaisesRegex(ControlError, 'parts_schema_migration_required'):
            self.initiate(parts=['one', 'two', 'three', 'four'])
        self.assertEqual(self.snapshot(), before)

    def test_initiate_validation(self):
        with self.assertRaisesRegex(ControlError,'initiate_has_targets'): self.initiate(message_ids=['m1'])

        with self.assertRaisesRegex(ControlError,'invalid_parts'): self.initiate(parts=[])
        self.ingest()
        with self.assertRaisesRegex(ControlError,'unanswered_incoming_exists'): self.initiate()
        self.store.apply('main',self.decision(dm_decisions=[{'action':'defer','message_ids':['m1'],'due_ms':self.clock+600000,'reason':'busy'}]),self.clock)
        with self.assertRaisesRegex(ControlError,'unanswered_incoming_exists'): self.initiate()
    def test_initiate_held_by_new_incoming_and_cancel(self):
        self.initiate()
        self.ingest()
        self.assertEqual(self.dispatch.send_due('main',MockTransport())['reason'],'draft_stale')
        self.store.apply('main',self.decision(dm_decisions=[{'action':'cancel_group','group_id':'p1','reason':'user wrote first'}]),self.clock)
        snap=self.snapshot()
        self.assertEqual(snap['proactive_group_ids'],[])
        self.assertEqual([m['status'] for m in snap['messages']],['received'])
        self.reply()
        self.assertEqual([r['status'] for r in self.dispatch.send_due('main',MockTransport())['results']],['confirmed'])
    def test_conversation_mode_and_live_gap(self):
        self.store.apply('main',self.decision(activities=[self.activity(attention='free')]),self.clock)
        self.assertEqual(self.snapshot()['conversation_mode'],'normal')   # no recent message yet
        self.ingest(); self.assertEqual(self.snapshot()['conversation_mode'],'live')
        self.reply(['ok'])
        self.dispatch.send_due('main',MockTransport())
        self.assertEqual(self.snapshot()['channel_next_send_after_ms']['instagram']-self.clock,5000)
        self.clock+=301000
        self.assertEqual(self.snapshot()['conversation_mode'],'normal')   # conversation went quiet
    def test_slow_first_reply_still_opens_live_lunch_conversation(self):
        self.store.apply('main',self.decision(activities=[self.activity(attention='free')]),self.clock)
        self.ingest()
        self.clock+=8*60000
        self.assertEqual(self.snapshot()['conversation_mode'],'normal')
        self.reply(['ok'])
        self.assertEqual(self.snapshot()['conversation_mode'],'live')
        self.dispatch.send_due('main',MockTransport())
        self.assertEqual(self.snapshot()['channel_next_send_after_ms']['instagram']-self.clock,5000)
        self.clock+=301000
        self.assertEqual(self.snapshot()['conversation_mode'],'normal')
    def test_focused_activity_is_quiet(self):
        self.store.apply('main',self.decision(activities=[self.activity(attention='focused')]),self.clock)
        self.ingest()
        self.assertEqual(self.snapshot()['conversation_mode'],'quiet')
    def test_past_due_means_asap_but_never_before_deferred(self):
        self.ingest(); self.reply(due_ms=self.clock-60000)
        self.assertEqual(self.snapshot()['jobs'][0]['due_ms'],self.clock)
        self.ingest('m2')
        self.store.apply('main',self.decision(dm_decisions=[{'action':'defer','message_ids':['m2'],'due_ms':self.clock+600000,'reason':'busy'}]),self.clock)
        with self.assertRaisesRegex(ControlError,'reply_due_advanced'):
            self.store.apply('main',self.decision(dm_decisions=[{'action':'reply','message_ids':['m2'],'group_id':'g2',
                'due_ms':self.clock-1,'parts':['x'],'reason':'r'}]),self.clock)
    def test_wait_dm_timeout_and_new_message(self):
        sleeps=[]
        def sleep(s): sleeps.append(s); self.clock+=int(s*1000)
        result=self.store.wait_dm('main',10,lambda:self.clock,sleep)
        self.assertEqual(result['reason'],'timeout')
        self.assertIsNone(self.snapshot()['conversation_lease_until_ms'])
        self.assertTrue(sleeps and all(s<=2 for s in sleeps))
        def sleep_then_dm(s):
            self.clock+=int(s*1000)
            if not self.snapshot()['messages']: self.ingest()
        result=self.store.wait_dm('main',60,lambda:self.clock,sleep_then_dm)
        self.assertEqual((result['reason'],result['new_message_ids']),('new_dm',['m1']))
        self.assertGreater(self.snapshot()['conversation_lease_until_ms'],self.clock)   # gate stays silent while composing
        self.store.end_conversation('main',self.clock)
        self.assertIsNone(self.snapshot()['conversation_lease_until_ms'])
    def test_wait_dm_renews_short_lease_and_crash_does_not_hold_it_for_minutes(self):
        def stop_wait(seconds):
            self.clock+=int(seconds*1000)
            if self.clock>=T+40000:
                raise RuntimeError('worker stopped')
        with self.assertRaisesRegex(RuntimeError,'worker stopped'):
            self.store.wait_dm('main',180,lambda:self.clock,stop_wait)
        lease_until=self.snapshot()['conversation_lease_until_ms']
        self.assertLessEqual(lease_until-self.clock,30000)
        self.clock+=31000
        self.assertIsNone(self.snapshot()['conversation_lease_until_ms'])
    def test_wait_dm_returns_for_due_life_decision(self):
        self.store.apply('main',self.decision(activities=[self.activity(end=self.clock+5000)]),self.clock)
        def sleep(s): self.clock+=int(s*1000)
        result=self.store.wait_dm('main',60,lambda:self.clock,sleep)
        self.assertEqual(result['reason'],'decision_needed')
        self.assertIn('activity_due',result['needs_decision'])
    def slack_ingest(self,mid='s1'):
        return self.ingest(mid,observation={'identity':'platform','channel':'slack'})
    def slack_reply(self,**kwargs):
        dm={'action':'reply','message_ids':['s1'],'group_id':'g1','due_ms':self.clock,'parts':['x'],'reason':'r'}
        dm.update(kwargs)
        return self.store.apply('main',self.decision(dm_decisions=[dm]),self.clock)
    def test_reply_routes_to_message_channel(self):
        self.slack_ingest(); self.slack_reply()
        self.assertEqual(self.snapshot()['jobs'][0]['channel'],'slack')
        self.assertEqual(self.dispatch.send_due('main',MockTransport())['results'],[])   # instagram sender sees nothing
        self.assertEqual([r['job_id'] for r in self.dispatch.send_due('main',MockTransport(),channel='slack')['results']],['g1:0'])
    def test_slack_turns_are_not_delayed_for_ten_minutes(self):
        self.slack_ingest(); self.slack_reply()
        self.dispatch.send_due('main',MockTransport(),channel='slack')
        self.slack_ingest('s2')
        self.store.apply('main',self.decision(dm_decisions=[
            {'action':'reply','message_ids':['s2'],'group_id':'g2','due_ms':self.clock,
             'parts':['second'],'reason':'r'}]),self.clock)
        self.assertEqual(self.dispatch.send_due('main',MockTransport(),channel='slack')['reason'],'send_interval')
        self.clock+=20000
        sent=self.dispatch.send_due('main',MockTransport(),channel='slack')['results']
        self.assertEqual([r['job_id'] for r in sent],['g2:0'])
    def test_quiet_slack_gap_is_under_one_minute(self):
        self.store.apply('main',self.decision(activities=[self.activity(attention='focused')]),self.clock)
        self.slack_ingest(); self.slack_reply()
        self.dispatch.send_due('main',MockTransport(),channel='slack')
        self.assertEqual(self.snapshot()['channel_next_send_after_ms']['slack']-self.clock,45000)
        self.slack_ingest('s2')
        self.store.apply('main',self.decision(dm_decisions=[
            {'action':'reply','message_ids':['s2'],'group_id':'g2','due_ms':self.clock,
             'parts':['second'],'reason':'r'}]),self.clock)
        self.clock+=45000
        self.assertEqual([r['job_id'] for r in self.dispatch.send_due('main',MockTransport(),channel='slack')['results']],['g2:0'])
    def test_old_shared_slack_gap_is_clamped_on_migration(self):
        self.slack_ingest(); self.slack_reply()
        self.dispatch.send_due('main',MockTransport(),channel='slack')
        with sqlite3.connect(self.path) as db:
            db.execute('DROP TABLE channel_pacing')
            db.execute('UPDATE sessions SET next_send_after_ms=?', (int(time.time()*1000)+600000,))
        snap=self.snapshot()
        self.assertLessEqual(snap['channel_next_send_after_ms']['slack']-int(time.time()*1000),45000)
        self.assertIsNone(snap['state']['next_send_after_ms'])
    def test_instagram_gap_does_not_hold_slack(self):
        self.ingest(); self.reply()
        self.dispatch.send_due('main',MockTransport())
        self.slack_ingest(); self.slack_reply(group_id='gs')
        self.clock+=3000
        self.assertEqual([r['job_id'] for r in self.dispatch.send_due('main',MockTransport(),channel='slack')['results']],['gs:0'])
        self.assertGreater(self.snapshot()['channel_next_send_after_ms']['instagram'],self.clock)
    def test_unavailable_blocks_draft_and_dispatch_then_requires_review(self):
        self.slack_ingest(); self.slack_reply()
        self.clock+=1000
        self.store.apply('main',self.decision(activities=[self.activity('sleep',category='sleep',label='수면',
            attention='unavailable',end=self.clock+3600000)]),self.clock)
        self.assertEqual(self.dispatch.send_due('main',MockTransport(),channel='slack')['reason'],'activity_unavailable')
        self.slack_ingest('s2')
        with self.assertRaisesRegex(ControlError,'activity_unavailable'):
            self.store.apply('main',self.decision(dm_decisions=[
                {'action':'reply','message_ids':['s2'],'group_id':'g2','due_ms':self.clock,
                 'parts':['during sleep'],'reason':'r'}]),self.clock)
        self.clock+=60000
        self.store.apply('main',self.decision(activities=[
            {'id':'sleep','status':'interrupted','actual_end_ms':self.clock,'reason':'woke to Slack'},
            self.activity('phone',start=self.clock,attention='light')],
            dm_decisions=[{'action':'defer','message_ids':['s2'],'due_ms':self.clock+60000,'reason':'later'}]),self.clock)
        self.assertIn('draft_needs_review',self.snapshot()['needs_decision'])
        self.assertEqual(self.dispatch.send_due('main',MockTransport(),channel='slack')['reason'],'draft_stale')
        self.store.apply('main',self.decision(dm_decisions=[
            {'action':'revalidate_group','group_id':'g1','reason':'still fits'}]),self.clock)
        self.assertEqual([r['job_id'] for r in self.dispatch.send_due('main',MockTransport(),channel='slack')['results']],['g1:0'])
    def test_sleep_defer_can_be_replied_early_after_interruption(self):
        self.store.apply('main',self.decision(activities=[self.activity('sleep',category='sleep',label='수면',
            attention='unavailable',end=self.clock+3600000)]),self.clock)
        self.slack_ingest()
        with self.assertRaisesRegex(ControlError,'activity_unavailable'): self.slack_reply()
        sleep_end=self.snapshot()['activity']['planned_end_ms']
        self.store.apply('main',self.decision(dm_decisions=[
            {'action':'defer','message_ids':['s1'],'due_ms':sleep_end,'reason':'asleep'}]),self.clock)
        self.clock+=60000
        self.store.apply('main',self.decision(activities=[
            {'id':'sleep','status':'interrupted','actual_end_ms':self.clock,'reason':'Slack alert'},
            self.activity('phone',start=self.clock,attention='light')],dm_decisions=[
            {'action':'reply','message_ids':['s1'],'group_id':'woke','due_ms':self.clock,
             'parts':['깼어요'],'reason':'woke'}]),self.clock)
        self.assertEqual([r['job_id'] for r in self.dispatch.send_due('main',MockTransport(),channel='slack')['results']],['woke:0'])
    def test_channel_validation(self):
        self.ingest(); self.slack_ingest()
        with self.assertRaisesRegex(ControlError,'mixed_channel_targets'): self.slack_reply(message_ids=['m1','s1'])
        with self.assertRaisesRegex(ControlError,'channel_mismatch'): self.slack_reply(channel='instagram')
        self.slack_reply(channel='slack')
    def test_initiate_channel(self):
        with self.assertRaisesRegex(ControlError,'invalid_channel'): self.initiate('p0',channel='Bad Channel')
        self.initiate(channel='slack')
        self.assertEqual(self.snapshot()['jobs'][0]['channel'],'slack')
    def test_instagram_429_and_unknown_do_not_block_slack(self):
        self.ingest(); self.reply(['ig'])
        self.slack_ingest(); self.store.apply('main',self.decision(dm_decisions=[
            {'action':'revalidate_group','group_id':'g1','reason':'ok'},
            {'action':'reply','message_ids':['s1'],'group_id':'gs','due_ms':self.clock,'parts':['slack'],'reason':'r'}]),self.clock)
        self.assertEqual(self.dispatch.send_due('main',MockTransport('429'))['reason'],'rate_limited')
        self.assertTrue(self.snapshot()['send_blocked'])
        self.assertEqual([r['job_id'] for r in self.dispatch.send_due('main',MockTransport(),channel='slack')['results']],['gs:0'])
        # Unknown is retained without stopping either channel or replaying it.
        self.ingest('m2'); self.slack_ingest('s2')
        self.clock+=4000000
        self.store.apply('main',self.decision(dm_decisions=[
            {'action':'cancel_group','group_id':'g1','reason':'replan'},
            {'action':'reply','message_ids':['m1','m2'],'group_id':'g2','due_ms':self.clock,'parts':['ig2'],'reason':'r'},
            {'action':'reply','message_ids':['s2'],'group_id':'gs2','due_ms':self.clock,'parts':['slack2'],'reason':'r'}]),self.clock)
        self.assertEqual(self.dispatch.send_due('main',MockTransport('unknown'))['results'][0]['status'],'unknown')
        self.clock+=600000
        self.assertEqual(self.dispatch.send_due('main',MockTransport())['results'],[])
        self.assertEqual([r['job_id'] for r in self.dispatch.send_due('main',MockTransport(),channel='slack')['results']],['gs2:0'])
    def test_slack_429_blocks_only_slack(self):
        self.slack_ingest(); self.slack_reply()
        self.assertEqual(self.dispatch.send_due('main',MockTransport('429'),channel='slack')['reason'],'rate_limited')
        self.assertIn('slack',self.snapshot()['channel_blocked_until_ms'])
        self.assertFalse(self.snapshot()['send_blocked'])
        self.assertEqual(self.dispatch.send_due('main',MockTransport(),channel='slack')['reason'],'rate_limited')
    def test_seed_preserves_unanswered_and_uncertain(self):
        target=Store(Path(self.tmp.name)/'seed.db')
        messages=[]
        for disposition in ('handled','unanswered','uncertain'):
            messages.append({'id':disposition,'text':'hello','observation':{'identity':'platform'},
                             'disposition':disposition,'handled_evidence':'sent reply recorded'})
        target.seed({'messages':messages},T)
        snap=target.tick('main',T)
        self.assertEqual(len(snap['events']),2)
        self.assertEqual(len(snap['messages']),2)
        self.assertTrue(next(m for m in snap['messages'] if m['id']=='uncertain')['observation']['review_required'])
        with self.assertRaisesRegex(ControlError,'database_exists'): target.seed({},T)

    def test_seed_failure_does_not_publish(self):
        target=Store(Path(self.tmp.name)/'broken.db')
        with self.assertRaises((KeyError,ControlError)): target.seed({'messages':[{}]},T)
        self.assertFalse(target.path.exists())

    def test_read_failure_preserves_revision(self):
        with self.assertRaisesRegex(ControlError,'read_failed'):
            self.store.ingest('main',{'read_ok':False,'code':429},self.clock)
        self.assertEqual(self.snapshot()['revision'],0)

    def test_backup_wal_and_literal_recall(self):
        self.store.apply('main',self.decision(facts=[{'id':'f','source':'observed','summary':'100%_literal',
            'details':{'evidence':'user statement'}}]),self.clock)
        self.assertEqual(len(self.store.recall('main',['%_'])['facts']),1)
        backup=Path(self.tmp.name)/'backup.db'
        self.store.backup(backup)
        self.assertEqual(Store(backup).tick('main',self.clock)['revision'],1)

    def test_week_plan_and_no_dm_time_due(self):
        p={'week_start':'2026-10-12','remote_date':'2026-10-14','seed':'persisted','reason':'available day'}
        self.store.apply('main',self.decision(weekly_plans=[p],activities=[self.activity()]),self.clock)
        self.assertEqual(self.snapshot()['weekly_plan']['remote_date'],'2026-10-14')
        self.clock+=3600000
        self.assertIn('activity_due',self.snapshot()['needs_decision'])
        p['remote_date']='2026-10-15'
        with self.assertRaises(ControlError): self.store.apply('main',self.decision(weekly_plans=[p]),self.clock)

    def test_nonfinite_and_pagination(self):
        with self.assertRaisesRegex(ControlError,'nonfinite_number'):
            self.store.apply('main',self.decision(metric_deltas={'mood':float('inf')}),self.clock)
        self.ingest('a'); self.ingest('b')
        snap=self.store.tick('main',self.clock,limit=1)
        self.assertTrue(snap['has_more'])
        self.assertEqual(len(self.store.tick('main',self.clock,after=snap['next_cursor'],limit=1)['events']),1)

    def test_appointment_conflict_rolls_back(self):
        a={'id':'meeting','start_ms':self.clock,'end_ms':self.clock+60000,
           'status':'confirmed','fixed':True,'location':'office'}
        with self.assertRaisesRegex(ControlError,'fixed_appointment_conflict'):
            self.store.apply('main',self.decision(appointments=[a],activities=[self.activity()]),self.clock)
        self.assertEqual(self.snapshot()['appointments'],[])

    def test_confirmed_appointments_visible_for_a_week(self):
        day=86400000
        def appt(aid,start,status='confirmed'):
            return {'id':aid,'start_ms':start,'end_ms':start+6*3600000,'status':status,
                    'fixed':False,'location':'cafe'}
        self.store.apply('main',self.decision(appointments=[
            appt('soon',self.clock+3*3600000),
            appt('confirmed-3d',self.clock+3*day),
            appt('confirmed-6d',self.clock+6*day),
            appt('confirmed-8d',self.clock+8*day),
            appt('proposed-2d',self.clock+2*day,'proposed'),
            appt('proposed-10h',self.clock+10*3600000,'proposed')]),self.clock)
        for view in ('full','life','dm','gate'):
            snap=self.store.tick('main',self.clock,view=view)
            self.assertEqual([a['id'] for a in snap['appointments']],
                             ['soon','proposed-10h','confirmed-3d','confirmed-6d'],view)
            self.assertNotIn('appointment_due',snap['needs_decision'],view)
        # appointment_due still fires only once a confirmed appointment has started.
        snap=self.store.tick('main',self.clock+3*3600000)
        self.assertIn('appointment_due',snap['needs_decision'])
        self.assertEqual(snap['appointments'][0]['id'],'soon')

    def test_appointment_list_is_capped(self):
        day=86400000
        appts=[{'id':f'a{i:02}','start_ms':self.clock+day+i*3600000,'end_ms':self.clock+day+i*3600000+1800000,
                'status':'confirmed','fixed':False,'location':'cafe'} for i in range(25)]
        self.store.apply('main',self.decision(appointments=appts),self.clock)
        ids=[a['id'] for a in self.snapshot()['appointments']]
        self.assertEqual(ids,[f'a{i:02}' for i in range(20)])

    def test_external_timeout_and_malformed_result(self):
        config={'contract':'dm-send-v1','enabled':True,'argv':[sys.executable,'-c','import time; time.sleep(2)'],'timeout_seconds':0.05}
        job={'id':'j','text':'mock','dedupe_key':'j'}
        self.assertEqual(ExternalTransport(config).send(job)['status'],'unknown')
        config['argv']=[sys.executable,'-c','print("not JSON")']; config['timeout_seconds']=5
        self.assertEqual(ExternalTransport(config).send(job)['status'],'unknown')
        with self.assertRaisesRegex(ControlError,'transport_mode_mismatch'):
            self.dispatch.send_due('main',ExternalTransport(config))

    def test_cli_json_and_exit_codes(self):
        def run(*args):
            return subprocess.run([sys.executable,str(ROOT/'ctl.py'),'--db',str(self.path),*args],capture_output=True,encoding='utf-8')
        process=run('status')
        self.assertEqual(process.returncode,0,process.stderr+process.stdout)
        self.assertTrue(json.loads(process.stdout)['ok'])
        d=self.decision(expected_revision=99)
        process=run('apply','--decision',json.dumps(d))
        self.assertEqual(process.returncode,2,process.stdout)
        self.assertEqual(json.loads(process.stdout)['reason'],'revision_conflict')
        summary_path = next((Path(self.tmp.name) / 'logs').glob('*.log'))
        summaries = [json.loads(line) for line in summary_path.read_text().splitlines()]
        self.assertEqual([x['command'] for x in summaries],
                         ['status', 'apply'])
        self.assertTrue(all(x['finished_ms'] >= x['started_ms'] and x['elapsed_ms'] >= 0
                            for x in summaries))

    def test_ingest_records_controller_time_separately_from_platform_time(self):
        platform_ms = self.clock - 180_000
        self.ingest(observed_ms=platform_ms,
                    observation={'identity': 'platform', 'ingested_ms': 1})
        message = self.snapshot()['messages'][0]
        self.assertEqual(message['observed_ms'], platform_ms)
        self.assertEqual(message['observation']['ingested_ms'], self.clock)
        self.clock += 1000
        self.ingest(observed_ms=platform_ms,
                    observation={'identity': 'platform', 'ingested_ms': 2})
        self.assertEqual(self.snapshot()['messages'][0]['observation']['ingested_ms'], self.clock - 1000)

    def test_cancel_and_replan_without_duplicate(self):
        self.ingest(); self.reply()
        self.store.apply('main',self.decision(dm_decisions=[{'action':'cancel_group','group_id':'g1','reason':'rewrite unsent draft'}]),self.clock)
        self.reply(group_id='g2')
        result=self.dispatch.send_due('main',MockTransport())
        self.assertEqual([r['job_id'] for r in result['results']],['g2:0'])

    def test_close_partial_group_does_not_replay_sent_part(self):
        class FirstThenNotSent:
            mode='mock'
            def send(self, job):
                if job['part_index']==0:
                    return {'status':'confirmed','provider_message_id':'p0','evidence':'mock'}
                return {'status':'failed','not_sent':True,'evidence':'mock rejection'}
        self.ingest(); self.reply(['one','two'])
        self.dispatch.send_due('main',FirstThenNotSent())
        self.store.apply('main',self.decision(dm_decisions=[{'action':'close_partial_group','group_id':'g1','reason':'first message sufficient'}]),self.clock)
        self.clock+=600000
        self.assertEqual(self.dispatch.send_due('main',MockTransport())['results'],[])
        self.assertEqual(self.snapshot()['messages'],[])

    def test_review_and_processed_event_require_decision(self):
        self.ingest(observation={'identity':'uncertain'})
        with self.assertRaisesRegex(ControlError,'message_requires_review'): self.reply()
        with self.assertRaisesRegex(ControlError,'dm_event_without_decision'):
            self.store.apply('main',self.decision(processed_event_ids=['dm:m1']),self.clock)
        self.store.apply('main',self.decision(dm_decisions=[{'action':'resolve_message',
            'message_id':'m1','identity':'verified_composite','evidence':'checked adjacent message IDs'}]),self.clock)
        self.reply()

    def test_real_os_lock_in_another_process(self):
        code='''import sys,time
sys.path.insert(0,sys.argv[1])
from common import file_lock
with file_lock(sys.argv[2],'send:main'):
 print('locked',flush=True)
 sys.stdin.readline()
'''
        process=subprocess.Popen([sys.executable,'-c',code,str(ROOT),str(self.path)],
                                 stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        try:
            self.assertEqual(process.stdout.readline().strip(),'locked')
            with self.assertRaisesRegex(ControlError,'busy'): self.dispatch.recover('main')
        finally:
            process.communicate('\n',timeout=5)
        self.assertEqual(self.dispatch.recover('main')['recovered_ids'],[])

    def test_maintenance_keeps_unowned_files(self):
        logs=Path(self.tmp.name)/'logs'; logs.mkdir()
        (logs/'2020-01-01.log').write_text('test')
        (logs/'important.log').write_text('keep')
        result=self.store.maintenance(self.clock)
        self.assertTrue(Path(result['backup']).exists())
        self.assertFalse((logs/'2020-01-01.log').exists())
        self.assertTrue((logs/'important.log').exists())
        self.assertIsNone(self.store.maintenance(self.clock)['backup'])

    def test_dry_run_does_not_claim_or_call_transport(self):
        self.ingest(); self.reply()
        before=self.snapshot()['revision']
        result=self.dispatch.send_due('main',MockTransport(),dry_run=True)
        self.assertEqual(result['reason'],'dry_run')
        self.assertEqual(self.snapshot()['revision'],before)
        self.assertEqual(self.snapshot()['jobs'][0]['status'],'pending')

    def test_result_evidence_required_and_new_activity_cannot_rewrite_past(self):
        self.ingest(); self.reply()
        job,_=self.dispatch._claim('main','mock',self.clock)
        with self.assertRaises(ControlError):
            self.dispatch._record('main',job,{'status':'confirmed'},self.clock)
        with self.assertRaisesRegex(ControlError,'activity_rewrites_history'):
            self.store.apply('main',self.decision(activities=[self.activity(start=T-1000)]),self.clock)


if __name__=='__main__': unittest.main()

