"""Shared-scene lifecycle with real SQLite and mock sends; no operating data."""
from datetime import datetime,timedelta
from unittest.mock import patch
import json
import unittest

from common import ControlError
from dispatch import MockTransport
import gate
import in_person
import prepared_inputs
import reply_generator
from store import Store,KST,INCOMING_SETTLE_MS
import test_controller as baseline
import test_decision_session as lifecycle


class InPersonTests(unittest.TestCase):
    def setUp(self):
        self.c=baseline.ControllerTests()
        self.c.setUp(); self.addCleanup(self.c.tearDown)
        c=self.c
        day=datetime.fromtimestamp(c.clock/1000,KST).date()
        monday=(day-timedelta(days=day.weekday())).isoformat()
        self.begin=c.clock
        self.appointment={'id':'meetup','start_ms':c.clock,'end_ms':c.clock+6*3600000,
            'status':'confirmed','fixed':True,'location':'station','details':{'with_user':True}}
        self.meeting=c.activity(aid='meeting',category='game',label='shared scene',location='station',
            end=c.clock+7*3600000,attention='light',details={'reason':'meetup','appointment_id':'meetup'})
        c.store.apply('main',c.decision(appointments=[self.appointment],activities=[self.meeting],
            weekly_plans=[{'week_start':monday,'remote_date':None,'seed':'test','reason':'test'}]),c.clock)

    def join(self,**changes):
        c=self.c
        c.ingest('s-arrival',text='지금 앞에 있어',observation={'identity':'platform','channel':'slack'})
        op={'action':'start','appointment_id':'meetup','channel':'slack',
            'evidence_message_ids':['s-arrival'],'reason':'actual shared scene',**changes}
        c.store.apply('main',c.decision(in_person_ops=[op],dm_decisions=[{
            'action':'no_reply','message_ids':['s-arrival'],'reason':'scene entry recorded'}],
            processed_event_ids=['dm:s-arrival']),c.clock)

    def test_confirmed_with_user_appointment_never_implicitly_starts_presence(self):
        snap=self.c.snapshot()
        self.assertFalse(snap['interaction']['active'])
        self.assertEqual(snap['interaction']['mode'],'remote')
        self.assertNotIn('appointment_due',snap['needs_decision'])
        self.assertEqual(snap['interaction']['candidate_appointment_id'],'meetup')

    def test_linked_activity_suppresses_repeated_due_but_unlinked_work_still_needs_review(self):
        c=self.c
        for delta in (0,1000,60000):
            self.assertNotIn('appointment_due',c.store.tick('main',c.clock+delta)['needs_decision'])
        with c.store.connect(True) as db:
            db.execute("UPDATE activities SET details_json=? WHERE id='meeting'",(json.dumps({'reason':'unlinked'}),))
        self.assertIn('appointment_due',c.snapshot()['needs_decision'])

    def test_missing_wrong_or_unreadable_start_evidence_is_rejected_atomically(self):
        c=self.c
        c.ingest('s-out',direction='outgoing',observation={'identity':'platform','channel':'slack'})
        c.ingest('s-uncertain',observation={'identity':'uncertain','channel':'slack'})
        for ids in ([],['missing'],['s-out'],['s-uncertain']):
            with self.subTest(ids=ids):
                before=c.snapshot()['revision']
                with self.assertRaises(ControlError):
                    c.store.apply('main',c.decision(in_person_ops=[{'action':'start',
                        'appointment_id':'meetup','channel':'slack','evidence_message_ids':ids,'reason':'test'}]),c.clock)
                self.assertEqual(c.snapshot()['revision'],before)
                self.assertFalse(c.snapshot()['interaction']['active'])

    def test_arrival_is_required_even_with_confirmed_appointment_and_user_message(self):
        c=self.c
        with c.store.connect(True) as db:
            db.execute("UPDATE activities SET location='in_transit',destination='station' WHERE id='meeting'")
        with self.assertRaisesRegex(ControlError,'in_person_arrival_required'):
            self.join()

    def test_shared_game_stays_focused_but_conversation_is_live_without_new_messages(self):
        c=self.c; self.join()
        with c.store.connect(True) as db:
            db.execute("UPDATE activities SET attention='focused' WHERE id='meeting'")
        snap=c.store.tick('main',c.clock+600000)
        self.assertEqual(snap['activity']['attention'],'focused')
        self.assertEqual(snap['conversation_mode'],'live')
        self.assertTrue(snap['interaction']['available'])
        with c.store.connect() as db:
            from store import conversation_mode
            self.assertEqual(conversation_mode(db,'main',c.clock+600000,'instagram'),'quiet')

    def test_unavailable_and_sleep_do_not_become_available_due_to_presence(self):
        c=self.c; self.join()
        with c.store.connect(True) as db:
            db.execute("UPDATE activities SET attention='unavailable' WHERE id='meeting'")
        self.assertFalse(c.snapshot()['interaction']['available'])
        self.assertEqual(c.snapshot()['conversation_mode'],'quiet')
        c.ingest('s2',observation={'identity':'platform','channel':'slack'})
        with self.assertRaisesRegex(ControlError,'activity_unavailable'):
            c.store.apply('main',c.decision(dm_decisions=[{'action':'reply','message_ids':['s2'],
                'channel':'slack','group_id':'g','parts':['answer'],'due_ms':c.clock,'reason':'reply'}]),c.clock)
        with c.store.connect(True) as db:
            db.execute("UPDATE activities SET attention='free',category='sleep' WHERE id='meeting'")
        self.assertFalse(c.snapshot()['interaction']['available'])
        self.assertNotEqual(c.snapshot()['conversation_mode'],'live')

    def test_shared_move_to_lab_and_reply_commit_without_relaxing_travel_continuity(self):
        c=self.c; self.join(); c.clock+=600000
        travel=c.activity(aid='travel',category='travel',location='in_transit',destination='school',
            end=c.clock+900000,details={'reason':'move together','appointment_id':'meetup'})
        c.store.apply('main',c.decision(activities=[{'id':'meeting','status':'completed',
            'actual_end_ms':c.clock,'reason':'move'},travel]),c.clock)
        c.clock+=900000
        lab=c.activity(aid='lab',category='game',location='school',attention='focused',
            details={'reason':'shared lab visit','appointment_id':'meetup'})
        c.ingest('s2',observation={'identity':'platform','channel':'slack'})
        result=c.store.apply('main',c.decision(activities=[{'id':'travel','status':'completed',
            'actual_end_ms':c.clock,'reason':'arrived'},lab],dm_decisions=[{'action':'reply',
            'channel':'slack','message_ids':['s2'],'group_id':'lab-reply','parts':['here we are'],
            'due_ms':c.clock,'reason':'answer in scene'}]),c.clock)
        self.assertTrue(result['ok'])
        self.assertEqual(c.snapshot()['activity']['location'],'school')
        self.assertEqual(c.snapshot()['appointments'][0]['location'],'station')
        self.assertNotIn('appointment_due',c.snapshot()['needs_decision'])
        self.assertEqual(c.dispatch.send_due('main',MockTransport(),channel='slack')['results'][0]['status'],'confirmed')

    def test_missing_travel_and_ordinary_fixed_appointment_conflicts_remain(self):
        c=self.c; self.join(); c.clock+=600000
        with self.assertRaisesRegex(ControlError,'missing_travel'):
            c.store.apply('main',c.decision(activities=[{'id':'meeting','status':'completed',
                'actual_end_ms':c.clock,'reason':'move'},c.activity(aid='lab',location='school',
                details={'reason':'no travel','appointment_id':'meetup'})]),c.clock)
        with self.assertRaisesRegex(ControlError,'fixed_appointment_conflict'):
            c.store.apply('main',c.decision(activities=[{'id':'meeting','status':'completed',
                'actual_end_ms':c.clock,'reason':'move'},c.activity(aid='travel',category='travel',
                location='in_transit',destination='school',details={'reason':'unlinked'})]),c.clock)

    def test_plan_end_is_review_not_automatic_departure_and_review_can_be_delayed(self):
        c=self.c; self.join(); c.clock=self.begin+6*3600000
        snap=c.snapshot()
        self.assertTrue(snap['interaction']['active'])
        self.assertIn('appointment_review_due',snap['needs_decision'])
        self.assertEqual(snap['appointments'][0]['id'],'meetup')
        at=c.clock+900000
        c.store.apply('main',c.decision(in_person_ops=[{'action':'review_later','review_at_ms':at,
            'reason':'still together; check at next boundary'}]),c.clock)
        self.assertNotIn('appointment_review_due',c.snapshot()['needs_decision'])
        self.assertEqual(c.snapshot()['state']['next_decision_ms'],at)
        self.assertTrue(c.snapshot()['interaction']['active'])
        c.clock=at
        self.assertIn('appointment_review_due',c.snapshot()['needs_decision'])

    def test_end_completes_appointment_without_score_or_clock_invention(self):
        c=self.c; self.join(); c.clock+=1000
        c.store.apply('main',c.decision(in_person_ops=[{'action':'end','reason':'said goodbye and departed'}]),c.clock)
        self.assertFalse(c.snapshot()['interaction']['active'])
        with c.store.connect() as db:
            row=db.execute('SELECT * FROM in_person_state').fetchone()
            self.assertEqual(row['ended_ms'],c.clock)
            self.assertEqual(db.execute("SELECT status FROM appointments WHERE id='meetup'").fetchone()[0],'completed')
            self.assertEqual(db.execute('SELECT count(*) FROM relationships').fetchone()[0],0)

    def test_ended_unmet_appointment_is_visible_for_review_and_can_be_deferred(self):
        c=self.c; c.clock=self.begin+6*3600000
        self.assertIn('appointment_review_due',c.snapshot()['needs_decision'])
        self.assertEqual(c.snapshot()['appointments'][0]['id'],'meetup')
        updated={**self.appointment,'details':{'with_user':True,'review_at_ms':c.clock+900000}}
        c.store.apply('main',c.decision(appointments=[updated]),c.clock)
        self.assertNotIn('appointment_review_due',c.snapshot()['needs_decision'])
        self.assertFalse(c.snapshot()['interaction']['active'])

    def test_old_remote_draft_needs_scene_review_and_can_be_revalidated(self):
        c=self.c
        c.ingest('s-arrival',observation={'identity':'platform','channel':'slack'})
        dm={'action':'reply','channel':'slack','message_ids':['s-arrival'],'group_id':'g',
            'parts':['여기야'],'due_ms':c.clock,'reason':'old remote draft'}
        c.store.apply('main',c.decision(dm_decisions=[dm]),c.clock)
        c.store.apply('main',c.decision(in_person_ops=[{'action':'start','appointment_id':'meetup',
            'channel':'slack','evidence_message_ids':['s-arrival'],'reason':'actually together now'}]),c.clock)
        self.assertIn('g',c.snapshot()['draft_stale_group_ids'])
        report=c.dispatch.send_due('main',MockTransport(),channel='slack')
        self.assertEqual(report['reason'],'draft_stale_scene')
        c.store.apply('main',c.decision(dm_decisions=[{'action':'revalidate_group','group_id':'g',
            'reason':'same line is appropriate in shared scene'}]),c.clock)
        self.assertEqual(c.dispatch.send_due('main',MockTransport(),channel='slack')['results'][0]['status'],'confirmed')

    def test_own_in_person_initiate_remains_allowed(self):
        c=self.c; self.join()
        c.store.apply('main',c.decision(dm_decisions=[{'action':'initiate','channel':'slack',
            'group_id':'own-line','parts':['다음엔 뭐 할래?'],'due_ms':c.clock,'reason':'speak first in scene'}]),c.clock)
        self.assertEqual(c.dispatch.send_due('main',MockTransport(),channel='slack')['results'][0]['status'],'confirmed')

    def test_active_presence_requires_explicit_end_before_appointment_completion(self):
        c=self.c; self.join()
        with self.assertRaisesRegex(ControlError,'in_person_close_required'):
            c.store.apply('main',c.decision(appointments=[{**self.appointment,'status':'completed'}]),c.clock)

    def test_queued_in_person_reply_is_reviewed_after_departure_without_new_incoming(self):
        c=self.c; self.join()
        c.ingest('s2',observation={'identity':'platform','channel':'slack'})
        c.store.apply('main',c.decision(dm_decisions=[{'action':'reply','message_ids':['s2'],
            'channel':'slack','group_id':'old-scene','parts':['scene line'],'due_ms':c.clock,'reason':'reply'}]),c.clock)
        c.store.apply('main',c.decision(in_person_ops=[{'action':'end','reason':'departed'}]),c.clock)
        self.assertIn('old-scene',c.snapshot()['draft_stale_group_ids'])
        self.assertEqual(c.dispatch.send_due('main',MockTransport(),channel='slack')['reason'],'draft_stale_scene')

    def test_old_remote_draft_cannot_revive_automatically_after_a_whole_meeting(self):
        c=self.c
        c.ingest('s-arrival',observation={'identity':'platform','channel':'slack'})
        c.store.apply('main',c.decision(dm_decisions=[{'action':'reply','channel':'slack',
            'message_ids':['s-arrival'],'group_id':'old-remote','parts':['old remote line'],
            'due_ms':c.clock,'reason':'before meeting'}]),c.clock)
        c.store.apply('main',c.decision(in_person_ops=[{'action':'start','appointment_id':'meetup',
            'channel':'slack','evidence_message_ids':['s-arrival'],'reason':'actual arrival'}]),c.clock)
        c.clock+=1000
        c.store.apply('main',c.decision(in_person_ops=[{'action':'end','reason':'departed'}]),c.clock)
        self.assertIn('old-remote',c.snapshot()['draft_stale_group_ids'])
        self.assertEqual(c.dispatch.send_due('main',MockTransport(),channel='slack')['reason'],'draft_stale_scene')

    def test_explicit_extension_keeps_scene_and_moves_review_to_new_plan_end(self):
        c=self.c; self.join(); c.clock+=1000
        extended={**self.appointment,'end_ms':self.appointment['end_ms']+1800000}
        c.store.apply('main',c.decision(appointments=[extended]),c.clock)
        snap=c.store.tick('main',self.appointment['end_ms'])
        self.assertTrue(snap['interaction']['active'])
        self.assertNotIn('appointment_review_due',snap['needs_decision'])
        self.assertEqual(snap['interaction']['review_at_ms'],extended['end_ms'])

    def test_old_v03_database_adds_only_empty_scene_tables(self):
        c=self.c; revision=c.snapshot()['revision']
        with c.store.connect(True) as db:
            db.execute('DROP TABLE group_scene_context')
            db.execute('DROP TABLE in_person_state')
        again=Store(c.path).tick('main',c.clock)
        self.assertFalse(again['interaction']['active'])
        self.assertEqual(again['revision'],revision)
        self.assertEqual(again['appointments'][0]['id'],'meetup')

    def test_read_only_snapshot_does_not_change_revision_or_start_a_scene(self):
        c=self.c; before=c.snapshot()['revision']
        self.assertEqual(c.snapshot()['revision'],before)
        self.assertFalse(c.snapshot()['interaction']['active'])


class InPersonInputTests(unittest.TestCase):
    def setUp(self):
        self.c=lifecycle.DecisionSessionTests(); self.c.setUp(); self.addCleanup(self.c.tearDown)
        c=self.c
        c.cfg['slack']={'channel':'D1'}
        self.appointment={'id':'home-meeting','start_ms':c.clock,'end_ms':c.clock+1800000,
            'fixed':True,'status':'confirmed','location':'home','details':{'with_user':True}}
        with c.store.connect(True) as db:
            db.execute("UPDATE activities SET details_json=? WHERE id='a1'",
                (json.dumps({'reason':'together','appointment_id':'home-meeting'}),))
        c.store.apply('main',c.decision(appointments=[self.appointment]),c.clock)
        c.ingest('s1',observation={'identity':'platform','channel':'slack'}); c.recent(('s1',),'slack')

    def start(self):
        return {'action':'start','appointment_id':'home-meeting','channel':'slack',
                'evidence_message_ids':['s1'],'reason':'actual arrival'}

    def enter(self):
        c=self.c
        c.store.apply('main',c.decision(in_person_ops=[self.start()]),c.clock)

    def test_bundled_and_compact_active_scene_keep_context_and_verbatim_guidance(self):
        c=self.c; self.enter()
        with c.store.connect(True) as db:
            db.execute('UPDATE sessions SET next_decision_ms=?',(c.clock+100000,))
        for mode in ('bundled','compact'):
            p=c.runner.prepare(mode=mode,decision_format='compact-v1')
            self.assertEqual(p['snapshot']['interaction']['mode'],'in_person')
            self.assertIn('design/in-person.md',p.get('documents',{}) if mode=='bundled'
                          else p['input_manifest']['documents'])
            self.assertIn('in-person',p['schema_sections'])
            if mode=='compact':
                self.assertIn('interaction',p['input_manifest']['required_state_sections'])

    def test_compact_start_requires_acknowledged_arrival_context(self):
        c=self.c
        p=c.runner.prepare(mode='compact')
        bundle=prepared_inputs.read_bundle(c.runner,p['turn_id'],p['input_id'])
        tokens=bundle['input_receipts']
        without=[]
        state=c.runner.state(p['turn_id'])
        import instrumentation
        for t in tokens:
            if state['input_offers'][instrumentation.digest(t)]['kind'] != 'context': without.append(t)
        d=c.decision(in_person_ops=[self.start()])
        with self.assertRaisesRegex(ControlError,'in_person_context_not_ready'):
            c.runner.commit(p['turn_id'],d,input_receipts=without)
        with patch.object(c.runner,'finish',return_value={'next':'done'}):
            self.assertTrue(c.runner.commit(p['turn_id'],d,input_receipts=tokens)['committed'])

    def test_preparation_and_review_reasons_reach_gate_and_life_routing(self):
        c=self.c; c.clock+=1800000
        snap=c.snapshot()
        reasons,detail=gate.evaluate(snap,None,c.clock)
        self.assertIn('appointment_review_due',reasons)
        self.assertIn('appointment_review_due',detail['work_manifest']['life_required'])

    def test_generator_receives_dynamic_scene_without_changing_fixed_prefix_order(self):
        c=self.c; self.enter()
        p=c.runner.prepare(mode='bundled')
        request={'schema':'reply-gen-v1','turn_id':p['turn_id'],'channel':'slack',
            'situation':'same room','memories':[],'target_message_ids':['s1']}
        dynamic,binding=reply_generator.source(c.runner,c.runner.state(p['turn_id']),request,None)
        self.assertTrue(dynamic['interaction']['active'])
        self.assertIn('먼저 말을',dynamic['interaction_rules'])

    def test_dm_with_life_boundary_receives_scene_guidance(self):
        c=self.c; self.enter()
        with c.store.connect(True) as db:
            db.execute('UPDATE sessions SET next_decision_ms=?',(c.clock,))
        p=c.runner.prepare(mode='bundled')
        self.assertEqual(p['snapshot']['work_manifest']['primary'],'dm')
        self.assertIn('runbooks/life-boundaries.md',p['documents'])
        self.assertIn('design/in-person.md',p['documents'])

    def test_arrival_candidate_receives_start_guidance_even_without_appointment_due(self):
        c=self.c
        p=c.runner.prepare(mode='compact',decision_format='compact-v1')
        self.assertFalse(p['snapshot']['interaction']['active'])
        self.assertEqual(p['snapshot']['interaction']['candidate_appointment_id'],'home-meeting')
        self.assertNotIn('appointment_due',p['snapshot']['needs_decision'] if
                         'needs_decision' in p['snapshot'] else p['snapshot']['work_manifest']['life_required'])
        self.assertIn('design/in-person.md',p['input_manifest']['documents'])
        self.assertIn('in-person',p['schema_sections'])

    def test_join_reply_and_next_scene_message_stay_in_one_warm_session(self):
        c=self.c
        p=c.runner.prepare(mode='bundled')
        received=[]
        def incoming(seconds):
            c.sleep(seconds)
            if not received:
                c.ingest('s2',observation={'identity':'platform','channel':'slack'})
                c.recent(('s1','s2'),'slack'); received.append(c.clock)
        c.runner.sleep=incoming
        d=c.decision(in_person_ops=[self.start()],dm_decisions=[{'action':'reply','channel':'slack',
            'message_ids':['s1'],'group_id':'arrival-line','parts':['왔네, 이쪽이야'],
            'due_ms':c.clock,'reason':'first spoken line'}],processed_event_ids=['dm:s1'])
        result=c.runner.commit(p['turn_id'],d)
        self.assertTrue(result['committed'])
        self.assertEqual(result['after']['next'],'prepare')
        self.assertEqual(result['after']['wait']['reason'],'new_dm')
        self.assertLessEqual(c.clock-received[0],INCOMING_SETTLE_MS+2000)
        self.assertNotIn('appointment_due',c.snapshot()['needs_decision'])
        with c.store.connect() as db:
            self.assertEqual(db.execute("SELECT status FROM dm_jobs WHERE group_id='arrival-line'").fetchone()[0],'confirmed')


class AppointmentPreparationTests(unittest.TestCase):
    def setUp(self):
        self.c=baseline.ControllerTests();self.c.setUp();self.addCleanup(self.c.tearDown)
        self.begin=self.c.clock
        self.appt={'id':'later','start_ms':self.begin+3600000,'end_ms':self.begin+7200000,
            'status':'confirmed','fixed':True,'location':'station','details':{
                'with_user':True,'prepare_at_ms':self.begin+900000,'depart_at_ms':self.begin+1800000}}

    def test_no_implicit_preparation_deadlines_are_invented(self):
        c=self.c; a={**self.appt,'details':{'with_user':True}}
        c.store.apply('main',c.decision(appointments=[a]),c.clock)
        self.assertEqual(c.snapshot()['appointment_lifecycle']['preparations'],[])
        self.assertEqual(c.snapshot()['state']['next_decision_ms'],a['start_ms'])

    def test_explicit_prepare_depart_and_arrival_use_existing_activities(self):
        c=self.c
        c.store.apply('main',c.decision(appointments=[self.appt],activities=[c.activity(
            aid='rest',location='home',end=self.begin+1200000)]),c.clock)
        self.assertEqual(c.snapshot()['state']['next_decision_ms'],self.begin+900000)
        c.clock=self.begin+900000
        snap=c.snapshot()
        self.assertIn('appointment_prepare_due',snap['needs_decision'])
        self.assertEqual(snap['appointment_lifecycle']['preparations'][0]['phase'],'prepare')
        c.store.apply('main',c.decision(activities=[{'id':'rest','status':'completed',
            'actual_end_ms':c.clock,'reason':'prepare'},c.activity(aid='prepare',category='preparation',
            end=self.begin+1800000,location='home',details={'reason':'prepare for actual plan',
                'appointment_id':'later','appointment_phase':'prepare'})]),c.clock)
        self.assertNotIn('appointment_prepare_due',c.snapshot()['needs_decision'])
        c.clock=self.begin+1800000
        self.assertEqual(c.snapshot()['appointment_lifecycle']['preparations'][0]['phase'],'depart')
        c.store.apply('main',c.decision(activities=[{'id':'prepare','status':'completed',
            'actual_end_ms':c.clock,'reason':'depart'},c.activity(aid='travel',category='travel',
            end=self.appt['start_ms'],location='in_transit',destination='station',
            details={'reason':'travel','appointment_id':'later'})]),c.clock)
        self.assertNotIn('appointment_prepare_due',c.snapshot()['needs_decision'])
        c.clock=self.appt['start_ms']
        c.store.apply('main',c.decision(activities=[{'id':'travel','status':'completed',
            'actual_end_ms':c.clock,'reason':'arrive'},c.activity(aid='meeting',category='social',
            location='station',details={'reason':'at meeting point','appointment_id':'later'})]),c.clock)
        self.assertNotIn('appointment_due',c.snapshot()['needs_decision'])
        self.assertFalse(c.snapshot()['interaction']['active'])

    def test_invalid_preparation_order_is_rejected_atomically(self):
        c=self.c
        a={**self.appt,'details':{'prepare_at_ms':self.begin+3000000,'depart_at_ms':self.begin+2000000}}
        with self.assertRaisesRegex(ControlError,'invalid_appointment_milestone_order'):
            c.store.apply('main',c.decision(appointments=[a]),c.clock)
        self.assertEqual(c.snapshot()['appointments'],[])

    def test_wait_wakes_at_appointment_review_boundary(self):
        c=self.c
        c.store.apply('main',c.decision(appointments=[self.appt],activities=[c.activity(
            aid='rest',location='home',end=self.appt['start_ms'])]),c.clock)
        c.clock=self.appt['end_ms']
        result=c.store.wait_dm('main',10,lambda:c.clock,c.dispatch.sleep)
        self.assertEqual(result['reason'],'decision_needed')
        self.assertIn('appointment_review_due',result['needs_decision'])


class LateArrivalTests(unittest.TestCase):
    """Real apply path: linked travel to a fixed meeting point may run past its start."""
    def setUp(self):
        self.c=baseline.ControllerTests();self.c.setUp();self.addCleanup(self.c.tearDown)
        c=self.c; self.begin=c.clock
        self.appt={'id':'meet','start_ms':self.begin+3600000,'end_ms':self.begin+4*3600000,
            'status':'confirmed','fixed':True,'location':'plaza','details':{'with_user':True}}
        c.store.apply('main',c.decision(appointments=[self.appt],activities=[c.activity(
            aid='rest',location='home',end=self.begin+3000000)]),c.clock)
        c.clock=self.begin+3000000

    def depart(self,**changes):
        c=self.c
        travel=c.activity(aid='travel',category='travel',location='in_transit',destination='plaza',
            end=self.appt['start_ms']+900000,details={'reason':'running late','appointment_id':'meet'})
        travel.update(changes)
        return c.store.apply('main',c.decision(activities=[{'id':'rest','status':'completed',
            'actual_end_ms':c.clock,'reason':'leave'},travel]),c.clock)

    def test_late_linked_travel_past_start_then_arrival_and_start(self):
        c=self.c
        self.assertTrue(self.depart()['ok'])
        c.clock=self.appt['start_ms']+300000
        snap=c.snapshot()
        self.assertEqual(snap['activity']['location'],'in_transit')
        self.assertNotIn('appointment_due',snap['needs_decision'])
        self.assertFalse(snap['interaction']['active'])
        self.assertNotIn('candidate_appointment_id',snap['interaction'])
        c.ingest('s-early',text='나 도착',observation={'identity':'platform','channel':'slack'})
        with self.assertRaisesRegex(ControlError,'in_person_arrival_required'):
            c.store.apply('main',c.decision(in_person_ops=[{'action':'start','appointment_id':'meet',
                'channel':'slack','evidence_message_ids':['s-early'],'reason':'still on the way'}]),c.clock)
        c.clock=self.appt['start_ms']+600000
        c.store.apply('main',c.decision(activities=[{'id':'travel','status':'completed',
            'actual_end_ms':c.clock,'reason':'arrived late'},c.activity(aid='meeting',category='social',
            location='plaza',details={'reason':'at meeting point','appointment_id':'meet'})]),c.clock)
        self.assertEqual(c.snapshot()['interaction'].get('candidate_appointment_id'),'meet')
        c.store.apply('main',c.decision(in_person_ops=[{'action':'start','appointment_id':'meet',
            'channel':'slack','evidence_message_ids':['s-early'],'reason':'met after late arrival'}],
            dm_decisions=[{'action':'no_reply','message_ids':['s-early'],'reason':'scene entry recorded'}],
            processed_event_ids=['dm:s-early']),c.clock)
        self.assertTrue(c.snapshot()['interaction']['active'])

    def test_overdue_late_travel_surfaces_activity_due_not_repeated_appointment_due(self):
        c=self.c; self.depart()
        c.clock=self.appt['start_ms']+900000
        needs=c.snapshot()['needs_decision']
        self.assertIn('activity_due',needs)
        self.assertNotIn('appointment_due',needs)

    def test_conflicting_activities_over_fixed_start_are_still_rejected(self):
        cases={'unlinked travel to the meeting point':{'details':{'reason':'unlinked'}},
               'linked travel elsewhere':{'destination':'school'},
               'linked activity at another place':{'category':'rest','location':'home','destination':None},
               'unlinked activity at the meeting point':{'category':'rest','location':'plaza','destination':None,
                                                         'details':{'reason':'unlinked'}}}
        for name,changes in cases.items():
            with self.subTest(name):
                before=self.c.snapshot()['revision']
                with self.assertRaisesRegex(ControlError,'fixed_appointment_conflict|missing_travel'):
                    self.depart(**{k:v for k,v in changes.items()})
                self.assertEqual(self.c.snapshot()['revision'],before)

    def test_linked_travel_elsewhere_is_fixed_conflict_specifically(self):
        with self.assertRaisesRegex(ControlError,'fixed_appointment_conflict'):
            self.depart(destination='school')
        with self.assertRaisesRegex(ControlError,'fixed_appointment_conflict'):
            self.depart(details={'reason':'unlinked'})


if __name__=='__main__': unittest.main()
