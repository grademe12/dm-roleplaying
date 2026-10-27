"""Offline integration tests against real SQLite and the normal apply/send ledgers."""
import json
import sqlite3
import subprocess
import sys
import unittest
from contextlib import closing
from pathlib import Path

import test_controller as baseline
from common import ControlError
from dispatch import MockTransport
from store import Store

T = baseline.T


class ActionIntentTests(unittest.TestCase):
    setUp = baseline.ControllerTests.setUp
    tearDown = baseline.ControllerTests.tearDown
    snapshot = baseline.ControllerTests.snapshot
    decision = baseline.ControllerTests.decision
    review = baseline.ControllerTests.review
    ingest = baseline.ControllerTests.ingest
    activity = baseline.ControllerTests.activity

    def create(self, iid='watch', **values):
        return {'action':'create','id':iid,'target_key':'series:severance:s1:e5',
                'summary':'세브란스 시즌1 5화 시청','self_chosen':True,
                'review_at_ms':self.clock+10000,'expires_ms':T+3600000,
                'reason':'유진이 다음 행동으로 선택함',**values}

    def apply(self, ops=None, **values):
        if ops is not None:
            values['action_intents'] = ops
        d = self.decision(**values)
        self.store.apply('main',d,self.clock)
        return d

    def view(self, iid='watch'):
        return self.store.intent_inspect('main',iid,self.clock)['intent']

    def start(self, iid='watch', aid='ep5', create=True):
        a = self.activity(aid,attention='focused',details={'reason':'선택한 시청', 'action_intent_id':iid})
        ops = ([self.create(iid)] if create else []) + [
            {'action':'start','id':iid,'activity_id':aid,'reason':'지금 시청 시작'}]
        self.apply(ops,activities=[a])

    def finish(self, action='complete', iid='watch', aid='ep5', fid='watched', **extra):
        terminal = 'completed' if action=='complete' else 'interrupted'
        outcome = 'completed' if action=='complete' else 'partial'
        fact = {'id':fid,'source':'simulated','summary':'시청 결과 확인',
                'occurred_ms':self.clock,'details':{'activity_id':aid,'action_intent_id':iid,
                'intent_outcome':outcome,'progress':'episode completed' if action=='complete' else 'stopped early'}}
        op = {'action':action,'id':iid,'result_fact_ids':[fid],'reason':'활동 결과 판단',**extra}
        return self.apply([op],activities=[{'id':aid,'status':terminal,
                          'actual_end_ms':self.clock,'reason':'end'}],facts=[fact])

    def test_intent_persists_and_due_uses_existing_decision_reason(self):
        self.apply([self.create()])
        self.assertEqual(self.snapshot()['state']['next_decision_ms'],T+10000)
        self.assertNotIn('decision_due',self.snapshot()['needs_decision'])
        self.store = Store(self.path)
        self.clock += 10000
        snap = self.snapshot()
        self.assertEqual(snap['action_intent_due_ids'],['watch'])
        self.assertIn('decision_due',snap['needs_decision'])
        self.assertEqual(snap['action_intents'][0]['status'],'pending')

    def test_no_intent_is_inferred_from_chat_or_topic_facts(self):
        self.ingest(text='5화 보고 와')
        self.apply(facts=[{'id':'topic','source':'simulated','summary':'볼까 고민 중'}])
        self.assertEqual(self.snapshot()['action_intents'],[])

    def test_self_choice_required_even_with_user_source(self):
        self.ingest()
        with self.assertRaisesRegex(ControlError,'intent_requires_self_choice'):
            self.apply([self.create(self_chosen=False,sources={'message_ids':['m1']})])
        self.assertEqual(self.snapshot()['action_intents'],[])

    def test_exact_decision_replay_and_conflicting_revision(self):
        d = self.apply([self.create()])
        self.assertEqual(self.store.apply('main',d,self.clock)['reason'],'already_applied')
        stale = {**d,'decision_id':'other','action_intents':[self.create('other')]}
        with self.assertRaisesRegex(ControlError,'revision_conflict'):
            self.store.apply('main',stale,self.clock)
        self.assertEqual(len(self.view()['events']),1)

    def test_target_deduplication_and_cross_channel_sources(self):
        self.ingest('m1')
        self.ingest('s1',observation={'identity':'platform','channel':'slack'})
        self.apply([self.create(sources={'message_ids':['m1']})])
        with self.assertRaisesRegex(ControlError,'intent_target_already_open'):
            self.apply([self.create('duplicate')])
        self.apply([{'action':'link_sources','id':'watch','sources':{'message_ids':['s1']},'reason':'same plan'}])
        self.assertEqual({s['source_id'] for s in self.view()['sources']},{'m1','s1'})

    def test_source_failure_rolls_back_activity_and_intent(self):
        revision = self.snapshot()['revision']
        with self.assertRaisesRegex(ControlError,'intent_source_not_found'):
            self.apply([self.create(sources={'message_ids':['missing']})],activities=[self.activity()])
        self.assertIsNone(self.snapshot()['activity'])
        self.assertEqual(self.snapshot()['revision'],revision)
        self.assertEqual(self.snapshot()['action_intents'],[])

    def test_tick_time_and_later_cli_persistence_time_are_distinct(self):
        d=self.decision(action_intents=[self.create(),
            {'action':'start','id':'watch','activity_id':'ep5','reason':'start'}],
            activities=[self.activity('ep5',details={'reason':'watch','action_intent_id':'watch'})])
        self.store.apply('main',d,self.clock+2000)
        self.clock += 2000
        self.assertEqual(self.view()['status'],'in_progress')
        self.assertEqual(self.snapshot()['activity']['start_ms'],T)

    def test_real_cli_tick_then_apply_starts_intent(self):
        command=[sys.executable,str(Path(baseline.ROOT)/'ctl.py'),'--db',str(self.path)]
        tick=json.loads(subprocess.run(command+['tick'],capture_output=True,text=True,check=True).stdout)
        now=tick['now_ms']
        d={'decision_id':'cli-start','expected_revision':tick['revision'],'advance_to_ms':now,
           'next_decision_ms':now+3600000,
           'activities':[self.activity('cli-watch',start=now,end=now+3600000,
                         details={'reason':'watch','action_intent_id':'watch'})],
           'action_intents':[self.create(earliest_ms=now,review_at_ms=now+60000,expires_ms=now+3600000),
                            {'action':'start','id':'watch','activity_id':'cli-watch','reason':'chosen'}]}
        result=subprocess.run(command+['apply','--decision',json.dumps(d)],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertEqual(self.view()['status'],'in_progress')

    def test_result_freshness_uses_write_time_not_tick_time(self):
        self.start(); self.clock+=1000
        fact={'id':'delayed-result','source':'simulated','summary':'result','occurred_ms':self.clock,
              'details':{'activity_id':'ep5','action_intent_id':'watch','intent_outcome':'completed'}}
        d=self.decision(action_intents=[{'action':'complete','id':'watch',
                        'result_fact_ids':['delayed-result'],'reason':'observed simulated result'}],facts=[fact],
                        activities=[{'id':'ep5','status':'completed','actual_end_ms':self.clock,'reason':'done'}])
        self.store.apply('main',d,self.clock+2000)
        self.assertEqual(self.view()['status'],'completed')

    def test_create_and_start_does_not_advance_progress(self):
        self.start()
        self.assertEqual(self.view()['status'],'in_progress')
        self.assertEqual(self.view()['activity_ids'],['ep5'])
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM facts WHERE id='watched'").fetchone()[0],0)

    def test_free_rest_can_transition_during_dm_decision(self):
        self.apply(activities=[self.activity('rest')])
        self.clock += 1000
        self.ingest()
        dm={'action':'reply','message_ids':['m1'],'group_id':'bye','due_ms':self.clock,
            'parts':['응, 이제 5화 보고 올게'],'reason':'conversation ended'}
        new=self.activity('ep5',details={'reason':'next choice','action_intent_id':'watch'})
        self.apply([self.create(sources={'message_ids':['m1'],'job_ids':['bye:0']}),
                    {'action':'start','id':'watch','activity_id':'ep5','reason':'next action'}],
                   activities=[{'id':'rest','status':'interrupted','actual_end_ms':self.clock,'reason':'chosen next action'},new],
                   dm_decisions=[dm],processed_event_ids=['dm:m1'])
        self.assertEqual(self.snapshot()['activity']['id'],'ep5')
        self.assertFalse(next(s for s in self.view()['sources'] if s['kind']=='job')['delivered'])

    def test_complete_requires_ended_activity_and_fresh_result(self):
        self.start()
        with self.assertRaisesRegex(ControlError,'intent_activity_not_ended'):
            self.apply([{'action':'complete','id':'watch','reason':'guess','result_fact_ids':['none']}])
        self.clock += 1000
        with self.assertRaisesRegex(ControlError,'intent_result_required'):
            self.apply([{'action':'complete','id':'watch','reason':'guess'}],
                       activities=[{'id':'ep5','status':'completed','actual_end_ms':self.clock,'reason':'done'}])
        self.assertEqual(self.snapshot()['activity']['status'],'in_progress')
        self.finish()
        self.assertEqual(self.view()['status'],'completed')
        self.assertEqual(self.snapshot()['action_intents'],[])

    def test_ended_execution_cannot_be_left_open(self):
        self.start(); self.clock += 1000
        with self.assertRaisesRegex(ControlError,'intent_activity_resolution_required'):
            self.apply(activities=[{'id':'ep5','status':'interrupted','actual_end_ms':self.clock,'reason':'stop'}])
        self.assertEqual(self.snapshot()['activity']['status'],'in_progress')

    def test_partial_progress_defer_and_resume_links_both_segments(self):
        self.start(); self.clock += 1000
        self.finish('defer',review_at_ms=self.clock+10000)
        self.assertEqual(self.view()['status'],'pending')
        self.clock += 10000
        self.start(create=False,aid='ep5-resumed')
        self.assertEqual(self.view()['activity_ids'],['ep5','ep5-resumed'])
        self.clock += 1000
        self.finish(aid='ep5-resumed',fid='watched-final')
        self.assertEqual(self.view()['status'],'completed')

    def test_interrupted_activity_cannot_complete_intent(self):
        self.start(); self.clock += 1000
        fact={'id':'r','source':'simulated','summary':'r','occurred_ms':self.clock,
              'details':{'activity_id':'ep5','action_intent_id':'watch','intent_outcome':'completed'}}
        with self.assertRaisesRegex(ControlError,'intent_completion_requires_completed_activity'):
            self.apply([{'action':'complete','id':'watch','result_fact_ids':['r'],'reason':'r'}],facts=[fact],
                       activities=[{'id':'ep5','status':'interrupted','actual_end_ms':self.clock,'reason':'r'}])

    def test_wrong_result_link_and_future_time_rejected(self):
        self.start(); self.clock += 1000
        for details, occurred, error in [
            ({'activity_id':'wrong','action_intent_id':'watch','intent_outcome':'completed'},self.clock,'intent_result_link_mismatch'),
            ({'activity_id':'ep5','action_intent_id':'watch','intent_outcome':'completed'},T-1,'intent_result_time_mismatch')]:
            with self.subTest(error=error), self.assertRaisesRegex(ControlError,error):
                self.apply([{'action':'complete','id':'watch','result_fact_ids':['r'],'reason':'r'}],
                    facts=[{'id':'r','source':'simulated','summary':'r','occurred_ms':occurred,'details':details}],
                    activities=[{'id':'ep5','status':'completed','actual_end_ms':self.clock,'reason':'r'}])

    def test_old_fact_cannot_be_reused_as_new_result(self):
        self.start(); self.clock += 1000
        fact={'id':'r','source':'simulated','summary':'r','occurred_ms':self.clock,
              'details':{'activity_id':'ep5','action_intent_id':'watch','intent_outcome':'completed'}}
        self.apply(facts=[fact])
        with self.assertRaisesRegex(ControlError,'intent_result_reused'):
            self.apply([{'action':'complete','id':'watch','result_fact_ids':['r'],'reason':'r'}],facts=[fact],
                activities=[{'id':'ep5','status':'completed','actual_end_ms':self.clock,'reason':'r'}])

    def test_sleep_suppresses_intent_due_until_existing_end(self):
        self.apply([self.create()],activities=[self.activity('sleep',category='sleep',attention='unavailable',end=T+60000)])
        self.assertEqual(self.snapshot()['state']['next_decision_ms'],T+60000)
        self.clock += 20000
        self.assertEqual(self.snapshot()['action_intent_due_ids'],[])
        self.assertEqual(self.view()['effective_review_ms'],T+60000)
        self.clock = T+60000
        self.assertEqual(self.snapshot()['action_intent_due_ids'],['watch'])

    def test_intent_start_cannot_interrupt_unavailable_even_with_end_payload(self):
        self.apply([self.create()],activities=[self.activity('sleep',attention='unavailable')])
        self.clock += 1000
        with self.assertRaisesRegex(ControlError,'intent_cannot_interrupt_unavailable'):
            self.apply([{'action':'start','id':'watch','activity_id':'ep5','reason':'watch'}],
                activities=[{'id':'sleep','status':'interrupted','actual_end_ms':self.clock,'reason':'watch'},
                    self.activity('ep5',details={'reason':'watch','action_intent_id':'watch'})])
        self.assertEqual(self.snapshot()['activity']['id'],'sleep')

    def test_expiry_requires_explicit_resolution_and_is_not_completion(self):
        self.apply([self.create(expires_ms=T+20000)])
        self.clock += 20000
        self.assertTrue(self.view()['expired_by_clock'])
        with self.assertRaisesRegex(ControlError,'intent_review_required'):
            self.apply()
        self.apply([{'action':'expire','id':'watch','reason':'window passed'}])
        self.assertEqual(self.view()['status'],'expired')
        with self.assertRaisesRegex(ControlError,'intent_not_pending'):
            self.apply([{'action':'start','id':'watch','activity_id':'ep5','reason':'old plan'}])

    def test_expiry_during_sleep_does_not_wake_or_mutate_on_read(self):
        self.apply([self.create(expires_ms=T+20000)],
                   activities=[self.activity('sleep',attention='unavailable',end=T+60000)])
        self.clock += 30000
        self.assertEqual(self.snapshot()['action_intent_due_ids'],[])
        self.assertEqual(self.view()['status'],'pending')
        self.apply(facts=[{'id':'other','source':'simulated','summary':'unrelated'}])
        self.assertEqual(self.view()['status'],'pending')

    def test_defer_preserves_hard_expiry_and_requires_future_review(self):
        self.apply([self.create()]); self.clock += 10000
        for when in (self.clock,T+3600000):
            with self.subTest(when=when), self.assertRaisesRegex(ControlError,'invalid_intent_window'):
                self.apply([{'action':'defer','id':'watch','review_at_ms':when,'reason':'later'}])
        self.apply([{'action':'defer','id':'watch','review_at_ms':self.clock+30000,'reason':'after meal'}])
        self.assertEqual(self.view()['expires_ms'],T+3600000)

    def test_cancel_and_explicit_replacement_preserve_history(self):
        self.apply([self.create()])
        self.apply([{'action':'cancel','id':'watch','reason':'different choice'},
                    self.create('replacement',supersedes_id='watch')])
        self.assertEqual(self.view()['status'],'cancelled')
        self.assertEqual(self.view('replacement')['supersedes_id'],'watch')
        self.assertEqual(len(self.view()['events']),2)

    def test_active_cancel_requires_ended_segment_result(self):
        self.start(); self.clock += 1000
        with self.assertRaisesRegex(ControlError,'intent_activity_not_ended'):
            self.apply([{'action':'cancel','id':'watch','reason':'stop'}])
        self.finish('cancel')
        self.assertEqual(self.view()['status'],'cancelled')

    def test_start_before_earliest_and_history_rewrites_rejected(self):
        self.apply([self.create(earliest_ms=T+5000)])
        with self.assertRaisesRegex(ControlError,'intent_not_executable'):
            self.apply([{'action':'start','id':'watch','activity_id':'x','reason':'too early'}])
        self.clock += 1000
        with self.assertRaisesRegex(ControlError,'invalid_advance_time'):
            self.apply([{'action':'cancel','id':'watch','reason':'past edit'}],advance_to_ms=T-1)

    def test_started_activity_requires_matching_identity(self):
        with self.assertRaisesRegex(ControlError,'intent_activity_link_mismatch'):
            self.apply([self.create(),{'action':'start','id':'watch','activity_id':'wrong','reason':'start'}],
                       activities=[self.activity('wrong')])

    def test_activity_annotation_alone_cannot_replace_the_intent_contract(self):
        with self.assertRaisesRegex(ControlError,'intent_activity_not_registered'):
            self.apply(activities=[self.activity('missing',
                       details={'reason':'watch','action_intent_id':'nonexistent'})])
        self.assertIsNone(self.snapshot()['activity'])

    def test_invalid_fields_ttl_and_missing_sources_are_rejected(self):
        for changes, error in [({'unknown':1},'unknown_intent_field'),
                               ({'expires_ms':T+8*86400000},'invalid_intent_window'),
                               ({'review_at_ms':T},'invalid_intent_window'),
                               ({'sources':{'text':'raw quote'}},'unknown_intent_source_field')]:
            with self.subTest(changes=changes), self.assertRaisesRegex(ControlError,error):
                self.apply([self.create(**changes)])

    def test_intent_due_wakes_existing_live_wait(self):
        self.apply([self.create(review_at_ms=T+2000)])
        # Keep weekly-plan/no-current-activity reasons from waking first.
        self.apply(activities=[self.activity()],weekly_plans=[{
            'week_start':'2026-10-12','remote_date':None,'seed':'s','reason':'test'}])
        result=self.store.wait_dm('main',10,lambda:self.clock,
                                  lambda seconds:setattr(self,'clock',self.clock+int(seconds*1000)))
        self.assertEqual(result['reason'],'decision_needed')
        self.assertEqual(result['waited_ms'],2000)
        self.assertIn('decision_due',result['needs_decision'])

    def test_plan_delivery_reads_unknown_and_later_confirmation(self):
        self.ingest()
        dm={'action':'reply','message_ids':['m1'],'group_id':'g1','due_ms':self.clock,
            'parts':['이따 5화 볼게'],'reason':'choice'}
        self.apply([self.create(sources={'job_ids':['g1:0']})],dm_decisions=[dm],processed_event_ids=['dm:m1'])
        self.dispatch.send_due('main',MockTransport('unknown'))
        source=self.view()['sources'][0]
        self.assertEqual(source['delivery_status'],'unknown'); self.assertFalse(source['delivered'])
        with self.store.connect() as db:
            job=db.execute("SELECT * FROM dm_jobs WHERE id='g1:0'").fetchone()
        self.dispatch.reconcile('main',{'job_id':'g1:0','attempt_token':job['attempt_token'],
            'expected_revision':self.snapshot()['revision'],
            'result':{'status':'confirmed','provider_message_id':'slack-ts',
                      'evidence':'actual post verified'}})
        self.assertTrue(self.view()['sources'][0]['delivered'])
        self.assertEqual(self.view()['status'],'pending')

    def test_unshared_result_is_context_only_and_shared_after_confirmation(self):
        self.start(); self.clock += 1000
        self.finish()
        self.assertEqual(self.snapshot()['action_intent_followups'],[])
        # Separate run with an explicit followup selected at creation.
        self.clock += 1000
        self.apply([self.create('again',followup_text='보고 감상을 전하기'),
                    {'action':'start','id':'again','activity_id':'second','reason':'rewatch'}],
                   activities=[self.activity('second',details={'reason':'rewatch','action_intent_id':'again'})])
        self.clock += 1000; self.finish(iid='again',aid='second',fid='second-result')
        self.assertEqual(len(self.snapshot()['action_intent_followups']),1)
        self.assertEqual(self.snapshot()['jobs'],[])
        self.ingest()
        dm={'action':'reply','message_ids':['m1'],'group_id':'result','due_ms':self.clock,
            'parts':['다 봤어. 재밌었어'],'reason':'share result'}
        self.apply([{'action':'share_result','id':'again','sources':{'job_ids':['result:0']},'reason':'share actual result'}],
                   dm_decisions=[dm],processed_event_ids=['dm:m1'])
        self.assertEqual(len(self.snapshot()['action_intent_followups']),1)
        self.dispatch.send_due('main',MockTransport('confirmed'))
        self.assertEqual(self.snapshot()['action_intent_followups'],[])

    def test_extension_initializes_existing_db_without_reseeding_or_deleting_data(self):
        self.ingest()
        # Simulate an existing v0.3 database with no intent extension.
        with closing(sqlite3.connect(self.path)) as db:
            for name in ('action_intent_events','action_intent_activities','action_intent_sources','action_intents'):
                db.execute('DROP TABLE '+name)
        self.store=Store(self.path)
        self.assertEqual(self.snapshot()['messages'][0]['id'],'m1')
        self.apply([self.create()])
        with self.store.connect() as db:
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0],'ok')
            self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(),[])

    def test_cli_inspection_includes_terminal_history(self):
        self.apply([self.create()])
        self.apply([{'action':'cancel','id':'watch','reason':'changed'}])
        result=subprocess.run([sys.executable,str(Path(baseline.ROOT)/'ctl.py'),
                               '--db',str(self.path),'intent-inspect','--id','watch'],
                              text=True,capture_output=True,check=True)
        document=json.loads(result.stdout)
        self.assertEqual(document['intent']['status'],'cancelled')
        self.assertEqual(document['intent']['events'][-1]['reason'],'changed')

    def test_cancelling_a_draft_does_not_cancel_the_self_chosen_action(self):
        self.ingest()
        dm={'action':'reply','message_ids':['m1'],'group_id':'draft','due_ms':self.clock+60000,
            'parts':['이따 볼게'],'reason':'plan'}
        self.apply([self.create(sources={'job_ids':['draft:0']})],dm_decisions=[dm],processed_event_ids=['dm:m1'])
        self.apply(dm_decisions=[{'action':'cancel_group','group_id':'draft','reason':'rewrite'}])
        self.assertEqual(self.view()['status'],'pending')
        self.assertEqual(self.view()['sources'][0]['delivery_status'],'cancelled')
        self.assertFalse(self.view()['sources'][0]['delivered'])

    def test_multiple_result_parts_must_all_be_confirmed(self):
        self.apply([self.create(followup_text='시청 후 감상 공유'),
                    {'action':'start','id':'watch','activity_id':'ep5','reason':'start'}],
                   activities=[self.activity('ep5',details={'reason':'watch','action_intent_id':'watch'})])
        self.clock+=1000; self.finish(); self.clock+=1000
        self.ingest()
        dm={'action':'reply','message_ids':['m1'],'group_id':'result','due_ms':self.clock,
            'parts':['다 봤어','이 부분이 재밌었어'],'reason':'share'}
        self.apply([{'action':'share_result','id':'watch','sources':{'job_ids':['result:0','result:1']},'reason':'share'}],
                   dm_decisions=[dm],processed_event_ids=['dm:m1'])
        self.dispatch.send_due('main',MockTransport('confirmed'),limit=1)
        self.assertFalse(self.view()['result_shared'])
        self.assertEqual(len(self.snapshot()['action_intent_followups']),1)
        self.clock += 60000
        self.dispatch.send_due('main',MockTransport('confirmed'),limit=1)
        self.assertTrue(self.view()['result_shared'])
        self.assertEqual(self.snapshot()['action_intent_followups'],[])

    def test_old_confirmed_plan_cannot_be_reused_as_shared_result(self):
        self.ingest()
        dm={'action':'reply','message_ids':['m1'],'group_id':'plan','due_ms':self.clock,
            'parts':['볼게'],'reason':'plan'}
        self.apply(dm_decisions=[dm],processed_event_ids=['dm:m1'])
        self.dispatch.send_due('main',MockTransport('confirmed'))
        self.clock += 1000
        self.start(); self.clock += 1000; self.finish()
        with self.assertRaisesRegex(ControlError,'intent_result_job_predates_completion'):
            self.apply([{'action':'share_result','id':'watch','sources':{'job_ids':['plan:0']},'reason':'wrong evidence'}])

    def test_confirmed_appointment_still_rejects_overlapping_intent_activity(self):
        self.apply(appointments=[{'id':'meeting','start_ms':T,'end_ms':T+60000,
                    'status':'confirmed','fixed':True,'location':'office','details':{'reason':'fixed'}}])
        with self.assertRaisesRegex(ControlError,'fixed_appointment_conflict'):
            self.start()
        self.assertEqual(self.snapshot()['action_intents'],[])

    def test_full_fact_identity_is_checked_even_when_summary_matches(self):
        self.start(); self.clock += 1000
        old={'id':'r','source':'simulated','summary':'same summary','occurred_ms':self.clock,
             'details':{'activity_id':'other'}}
        self.apply(facts=[old])
        with self.assertRaisesRegex(ControlError,'intent_result_identity_collision'):
            self.apply([{'action':'complete','id':'watch','result_fact_ids':['r'],'reason':'wrong overwrite'}],
                facts=[{**old,'details':{'activity_id':'ep5','action_intent_id':'watch','intent_outcome':'completed'}}],
                activities=[{'id':'ep5','status':'completed','actual_end_ms':self.clock,'reason':'done'}])

    def test_open_intents_have_a_bounded_tick_backlog(self):
        with self.assertRaisesRegex(ControlError,'too_many_open_intents'):
            self.apply([self.create(str(i),target_key='target:'+str(i)) for i in range(33)])
        self.assertEqual(self.snapshot()['action_intents'],[])

    def test_completed_followup_context_does_not_wake_or_send(self):
        self.apply([self.create(followup_text='감상 공유'),
                    {'action':'start','id':'watch','activity_id':'ep5','reason':'start'}],
                   activities=[self.activity('ep5',details={'reason':'watch','action_intent_id':'watch'})])
        self.clock += 1000; self.finish()
        self.apply(activities=[self.activity('rest')],weekly_plans=[{
            'week_start':'2026-10-12','remote_date':None,'seed':'s','reason':'test'}])
        snap=self.snapshot()
        self.assertEqual(len(snap['action_intent_followups']),1)
        self.assertEqual(snap['needs_decision'],[])
        self.assertEqual(snap['jobs'],[])

    def test_in_progress_intent_may_complete_after_its_start_window_expires(self):
        self.apply([self.create(expires_ms=T+20000),
                    {'action':'start','id':'watch','activity_id':'ep5','reason':'start'}],
                   activities=[self.activity('ep5',details={'reason':'watch','action_intent_id':'watch'})])
        self.clock += 30000
        self.assertEqual(self.view()['status'],'in_progress')
        self.finish()
        self.assertEqual(self.view()['status'],'completed')


if __name__ == '__main__':
    unittest.main()
