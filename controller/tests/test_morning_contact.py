"""Shared offline coverage for the daily greeting; no persona or transport credentials."""
from datetime import datetime, timedelta
import unittest

import test_controller as baseline
from common import ControlError, encode
from dispatch import MockTransport
import morning_contact
import gate


class MorningContactTests(unittest.TestCase):
    tearDown = baseline.ControllerTests.tearDown
    snapshot = baseline.ControllerTests.snapshot
    decision = baseline.ControllerTests.decision
    review = baseline.ControllerTests.review
    activity = baseline.ControllerTests.activity
    ingest = baseline.ControllerTests.ingest

    def setUp(self):
        baseline.ControllerTests.setUp(self)
        self.clock = baseline.T + 19 * morning_contact.HOUR  # next day, 07:00 KST
        self.wake = self.clock
        monday = datetime.fromtimestamp(self.clock / 1000, morning_contact.KST).date()
        monday -= timedelta(days=monday.weekday())
        self.store.apply('main', self.decision(activities=[self.sleep_activity(),
            self.activity('prep', attention='free', end=self.clock+24*morning_contact.HOUR)],
            weekly_plans=[{'week_start':monday.isoformat(),'remote_date':None,'seed':'test','reason':'fixture'}]), self.clock)
        self.ingest('history',direction='outgoing')

    def sleep_activity(self, aid='night'):
        return self.activity(aid, start=self.clock-8*morning_contact.HOUR, end=self.clock,
            category='sleep', label='밤잠', status='completed', actual_end_ms=self.clock,
            attention='unavailable', details={'reason':'night sleep','sleep_kind':'main'})

    def morning(self):
        return self.snapshot()['morning_contact']

    def draft(self, gid='morning', **kwargs):
        dm={'action':'initiate','channel':'instagram','group_id':gid,'parts':['잘 잤어요? 나 일어났어요.'],
            'due_ms':self.clock,'reason':'after main sleep','routine':'morning_contact'}
        dm.update(kwargs)
        return self.decision(dm_decisions=[dm])

    def queue(self, **kwargs):
        draft=self.draft(**kwargs)
        self.store.apply('main', draft, self.clock)
        return draft

    def boundary(self, at):
        with self.store.connect(True) as db:
            db.execute("INSERT INTO appointments VALUES (?,?,?,?,?,?,?,?)",
                ('main','day-start',at,at+morning_contact.HOUR,'confirmed',0,'office','{}'))

    def test_due_follows_recorded_wake_and_persists_without_new_incoming(self):
        first=self.morning()
        self.assertTrue(first['due'])
        self.assertEqual(first['wake_activity_id'],'night')
        end=datetime.fromtimestamp(self.clock/1000,morning_contact.KST).replace(hour=0,minute=0)+timedelta(days=1)
        self.assertEqual(first['expires_ms'],int(end.timestamp()*1000))
        self.clock+=60000
        self.assertTrue(self.morning()['due'])
        self.assertIn('morning_contact_due',self.snapshot()['needs_decision'])

    def test_no_recorded_main_sleep_does_not_invent_waking(self):
        with self.store.connect(True) as db:db.execute("DELETE FROM activities WHERE id='night'")
        self.assertEqual(self.morning()['status'],'no_recorded_wake')
        self.assertFalse(self.morning()['due'])

    def test_nap_is_not_a_second_morning(self):
        with self.store.connect(True) as db:
            db.execute("UPDATE activities SET details_json=? WHERE id='night'",(encode({'sleep_kind':'nap'}),))
        self.assertFalse(self.morning()['due'])

    def test_legacy_overnight_sleep_without_new_metadata_is_recognized(self):
        with self.store.connect(True) as db:
            db.execute("UPDATE activities SET details_json='{}' WHERE id='night'")
        self.assertTrue(self.morning()['due'])

    def test_sleep_overdue_is_wake_pending_until_real_completion(self):
        with self.store.connect(True) as db:
            db.execute("DELETE FROM activities WHERE id='prep'")
            db.execute("UPDATE activities SET status='in_progress',actual_end_ms=NULL WHERE id='night'")
        self.assertEqual(self.morning()['status'],'wake_pending')
        self.assertFalse(self.morning()['due'])
        changes=[{'id':'night','status':'completed','actual_end_ms':self.clock,'reason':'awake'},
                 self.activity('prep2',attention='free')]
        draft=self.draft();draft['activities']=changes
        self.store.apply('main',draft,self.clock)
        self.assertEqual(self.morning()['status'],'queued')

    def test_night_notification_interruption_does_not_trigger_morning(self):
        with self.store.connect(True) as db:
            db.execute("UPDATE activities SET actual_end_ms=?,status='interrupted' WHERE id='night'",
                       (self.clock-4*morning_contact.HOUR,))
        self.assertFalse(self.morning()['due'])

    def test_later_waking_has_its_own_window_not_a_fixed_office_clock(self):
        with self.store.connect(True) as db:
            db.execute("UPDATE activities SET actual_end_ms=?,planned_end_ms=? WHERE id='night'",
                       (self.clock+4*morning_contact.HOUR,self.clock+4*morning_contact.HOUR))
        self.clock+=4*morning_contact.HOUR
        self.assertTrue(self.morning()['due'])
        self.assertEqual(self.morning()['wake_ms'],self.clock)

    def test_queued_is_not_confirmed_and_cannot_be_queued_twice(self):
        self.queue()
        self.assertEqual(self.morning()['status'],'queued')
        self.assertEqual(self.morning()['delivery_status'],'pending')
        self.assertFalse(self.morning()['due'])
        with self.assertRaises(ControlError):self.queue(gid='duplicate')
        self.assertEqual(len(self.snapshot()['jobs']),1)

    def test_confirmation_satisfies_daily_proactive_contact_across_channels(self):
        draft=self.queue()
        self.dispatch.send_due('main',MockTransport())
        self.assertEqual(self.morning()['status'],'confirmed')
        with self.assertRaisesRegex(ControlError,'morning_contact_already_registered'):
            self.queue(gid='duplicate',channel='slack')
        self.assertEqual(self.store.apply('main',draft,self.clock)['reason'],'already_applied')

    def test_unknown_receipt_never_counts_as_confirmed_or_triggers_duplicate(self):
        self.queue();self.dispatch.send_due('main',MockTransport('unknown'))
        self.assertEqual(self.morning()['status'],'awaiting_receipt')
        self.assertEqual(self.morning()['delivery_status'],'uncertain')
        self.assertFalse(self.morning()['due'])
        with self.assertRaisesRegex(ControlError,'morning_contact_already_registered'):
            self.queue(gid='duplicate')

    def test_proven_not_sent_can_be_cancelled_and_replaced_in_same_window(self):
        self.queue();self.dispatch.send_due('main',MockTransport('failed'))
        self.assertTrue(self.morning()['due'])
        draft=self.draft(gid='replacement')
        draft['dm_decisions'].insert(0,{'action':'cancel_group','group_id':'morning','reason':'proven not sent'})
        self.store.apply('main',draft,self.clock)
        self.assertEqual(self.morning()['group_id'],'replacement')

    def test_unavailable_waits_without_blocking_other_life_saves(self):
        with self.store.connect(True) as db:
            db.execute("UPDATE activities SET attention='unavailable' WHERE id='prep'")
        self.assertEqual(self.morning()['status'],'waiting_for_availability')
        self.store.apply('main',self.decision(),self.clock)
        self.assertFalse(self.morning()['due'])

    def test_incoming_reply_does_not_count_as_proactive_greeting(self):
        self.ingest()
        self.assertEqual(self.morning()['status'],'waiting_for_incoming')
        reply=self.decision(dm_decisions=[{'action':'reply','message_ids':['m1'],'group_id':'reply',
            'parts':['잘 잤어요?'],'due_ms':self.clock,'reason':'answer'}],processed_event_ids=['dm:m1'])
        self.store.apply('main',reply,self.clock);self.dispatch.send_due('main',MockTransport())
        self.assertTrue(self.morning()['due'])
        self.assertIsNone(self.morning()['group_id'])

    def test_plain_initiate_also_does_not_implicitly_claim_morning(self):
        draft=self.draft();draft['dm_decisions'][0].pop('routine')
        self.store.apply('main',draft,self.clock);self.dispatch.send_due('main',MockTransport())
        self.assertTrue(self.morning()['due'])

    def test_work_or_first_class_is_the_deadline(self):
        with self.store.connect(True) as db:
            db.execute("INSERT INTO appointments VALUES (?,?,?,?,?,?,?,?)",
                ('main','class',self.clock+morning_contact.HOUR,self.clock+2*morning_contact.HOUR,
                 'confirmed',1,'school',encode({'category':'class'})))
        self.assertEqual(self.morning()['expires_ms'],self.clock+morning_contact.HOUR)

    def test_actual_arrival_closes_window_before_work_starts(self):
        with self.store.connect(True) as db:
            db.execute("UPDATE activities SET location='in_transit',destination='office',status='completed',actual_end_ms=? WHERE id='prep'",
                       (self.clock+60000,))
        self.clock+=60000
        self.assertEqual(self.morning()['status'],'expired')
        self.assertFalse(self.morning()['due'])

    def test_late_greeting_is_rejected_without_creating_queue_entries(self):
        with self.store.connect(True) as db:db.execute("UPDATE activities SET category='work' WHERE id='prep'")
        with self.assertRaisesRegex(ControlError,'morning_contact_outside_window'):self.queue()
        self.assertEqual(self.snapshot()['jobs'],[])

    def test_due_after_window_cannot_be_scheduled(self):
        self.boundary(self.clock+2*morning_contact.HOUR)
        with self.assertRaisesRegex(ControlError,'morning_contact_due_outside_window'):
            self.queue(due_ms=self.clock+2*morning_contact.HOUR)
        self.assertEqual(self.snapshot()['jobs'],[])

    def test_expired_draft_is_cancelled_and_does_not_block_regular_contact(self):
        self.queue();self.clock+=3*morning_contact.HOUR
        with self.store.connect(True) as db:
            db.execute("UPDATE activities SET category='work',start_ms=? WHERE id='prep'",(self.clock,))
        self.assertEqual(self.morning()['status'],'expired')
        self.dispatch.send_due('main',MockTransport())
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT status FROM dm_jobs WHERE group_id='morning'").fetchone()[0],'cancelled')
        draft=self.draft(gid='ordinary');draft['dm_decisions'][0].pop('routine')
        self.store.apply('main',draft,self.clock)
        self.dispatch.send_due('main',MockTransport())
        self.assertEqual(self.snapshot()['jobs'],[])

    def test_new_earlier_boundary_expires_an_already_queued_greeting(self):
        self.queue()
        with self.store.connect(True) as db:
            db.execute("UPDATE activities SET category='work' WHERE id='prep'")
        self.dispatch.send_due('main',MockTransport())
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT status FROM dm_jobs WHERE group_id='morning'").fetchone()[0],'cancelled')

    def test_routine_requires_text_and_cannot_bypass_normal_review(self):
        for overrides in ({'routine':'unknown'}, {'parts':[]}, {'parts':[{}]}, {'conversation_review':None}):
            with self.subTest(overrides=overrides),self.assertRaises((ControlError,TypeError)):
                self.queue(**overrides)
        self.assertEqual(self.snapshot()['jobs'],[])

    def test_next_day_needs_another_greeting_only_after_new_main_sleep(self):
        self.queue();self.dispatch.send_due('main',MockTransport())
        self.clock+=24*morning_contact.HOUR
        self.assertEqual(self.morning()['status'],'no_recorded_wake')
        changes=[{'id':'prep','status':'completed','actual_end_ms':self.wake+2*morning_contact.HOUR,'reason':'ended'},
                 self.sleep_activity('night2'),self.activity('prep2',attention='free')]
        self.store.apply('main',self.decision(activities=changes),self.clock)
        self.assertTrue(self.morning()['due'])
        self.queue(gid='morning2')

    def test_gate_wakes_and_uses_bounded_dm_retry_policy(self):
        snapshot=self.snapshot()
        reasons,detail=gate.evaluate(snapshot,{'last_run_ms':self.clock,'alerts':{}},self.clock)
        self.assertIn('morning_contact_due',reasons)
        self.assertEqual(detail['fingerprint_basis']['morning_contact']['wake_activity_id'],'night')
        self.assertEqual(gate.retry_delays(['morning_contact_due']),gate.DM_RETRY_DELAYS_MS)

    def test_read_only_status_does_not_change_revision(self):
        before=self.snapshot()['revision']
        self.morning();self.morning()
        self.assertEqual(before,self.snapshot()['revision'])

    def test_wait_session_also_notices_required_morning_contact(self):
        result=self.store.wait_dm('main',1,lambda:self.clock,lambda _:None)
        self.assertEqual(result['reason'],'decision_needed')
        self.assertIn('morning_contact_due',result['needs_decision'])

    def test_optional_first_contact_guard_is_preserved_for_student_adapter(self):
        with self.store.connect(True) as db:db.execute('DELETE FROM dm_messages')
        with self.store.connect() as db:
            self.assertEqual(morning_contact.snapshot(db,'main',self.clock,require_prior_contact=True)['status'],
                             'awaiting_first_contact')
            with self.assertRaisesRegex(ControlError,'morning_contact_first_contact_required'):
                morning_contact.attach(db,'main',self.draft()['dm_decisions'][0],self.clock,require_prior_contact=True)

    def test_natural_multi_bubble_greeting_is_accepted(self):
        self.queue(parts=['나 일어났어요.','오늘은 좀 일찍 나가려고요.'])
        self.assertEqual(len(self.snapshot()['jobs']),2)
        self.dispatch.send_due('main',MockTransport())
        self.assertEqual(self.morning()['status'],'confirmed')
        self.assertEqual(self.morning()['delivery_status'],'confirmed')

    def test_explicit_main_sleep_can_finish_at_four_even_if_short(self):
        self.clock-=3*morning_contact.HOUR
        with self.store.connect(True) as db:
            db.execute("UPDATE activities SET actual_end_ms=?,planned_end_ms=?,start_ms=? WHERE id='night'",
                       (self.clock,self.clock,self.clock-2*morning_contact.HOUR))
            db.execute("UPDATE activities SET start_ms=? WHERE id='prep'",(self.clock,))
            db.execute('UPDATE sessions SET advanced_to_ms=?',(self.clock,))
        self.assertTrue(self.morning()['due'])
        self.queue()

    def test_known_eleven_work_start_keeps_contact_eligible_after_ten(self):
        self.boundary(self.clock+4*morning_contact.HOUR)
        self.assertEqual(self.morning()['expires_ms'],self.wake+4*morning_contact.HOUR)
        self.clock+=3*morning_contact.HOUR+60000
        self.assertTrue(self.morning()['due'])
        self.queue()

    def test_old_stored_three_hour_cutoff_does_not_override_current_plan(self):
        self.boundary(self.clock+4*morning_contact.HOUR);self.queue()
        with self.store.connect(True) as db:
            db.execute('UPDATE morning_contacts SET expires_ms=?',(self.wake+3*morning_contact.HOUR,))
        self.clock+=3*morning_contact.HOUR+60000
        self.assertEqual(self.morning()['status'],'queued')
        self.dispatch.send_due('main',MockTransport())
        self.assertEqual(self.morning()['status'],'confirmed')

    def test_old_failed_proactive_wakes_and_can_be_replaced_without_manual_cleanup(self):
        draft=self.draft(gid='older');draft['dm_decisions'][0].pop('routine')
        self.store.apply('main',draft,self.clock);self.dispatch.send_due('main',MockTransport('failed'))
        state=self.snapshot()
        self.assertTrue(state['morning_contact']['due'])
        self.assertEqual(state['morning_contact']['status'],'review_existing_group')
        self.assertEqual(state['morning_contact']['review_groups'][0]['group_id'],'older')
        self.assertIn('morning_contact_due',state['needs_decision'])
        self.queue()
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT status FROM reply_groups WHERE id='older'").fetchone()[0],'cancelled')

    def test_live_queued_group_is_exposed_for_review_but_not_silently_cancelled(self):
        draft=self.draft(gid='older');draft['dm_decisions'][0].pop('routine')
        self.store.apply('main',draft,self.clock)
        self.assertTrue(self.morning()['due'])
        with self.assertRaisesRegex(ControlError,'initiate_already_active'):self.queue()
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT status FROM reply_groups WHERE id='older'").fetchone()[0],'active')
            self.assertEqual(db.execute("SELECT status FROM dm_jobs WHERE group_id='older'").fetchone()[0],'pending')

    def test_auto_cleanup_rolls_back_when_new_draft_fails_review(self):
        draft=self.draft(gid='older');draft['dm_decisions'][0].pop('routine')
        self.store.apply('main',draft,self.clock);self.dispatch.send_due('main',MockTransport('failed'))
        with self.assertRaises((ControlError,TypeError)):self.queue(conversation_review=None)
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT status FROM reply_groups WHERE id='older'").fetchone()[0],'active')

    def test_partial_greeting_keeps_delivered_part_and_closes_only_remaining_parts(self):
        self.boundary(self.clock+morning_contact.HOUR)
        self.queue(parts=['나 일어났어요.','이제 준비할게요.'])
        self.dispatch.send_due('main',MockTransport(),limit=1)
        self.assertEqual(self.morning()['delivery_status'],'partial')
        self.clock=self.wake+morning_contact.HOUR
        self.dispatch.send_due('main',MockTransport())
        with self.store.connect() as db:
            self.assertEqual([r[0] for r in db.execute("SELECT status FROM dm_jobs WHERE group_id='morning' ORDER BY part_index")],
                             ['confirmed','cancelled'])
            self.assertEqual(db.execute("SELECT status FROM reply_groups WHERE id='morning'").fetchone()[0],'completed')

    def test_group_review_changes_participate_in_gate_fingerprint(self):
        draft=self.draft(gid='older');draft['dm_decisions'][0].pop('routine')
        self.store.apply('main',draft,self.clock)
        _,detail=gate.evaluate(self.snapshot(),{'last_run_ms':self.clock,'alerts':{}},self.clock)
        self.assertEqual(detail['fingerprint_basis']['morning_contact']['review_groups'],
                         [{'group_id':'older','statuses':['pending']}])


if __name__=='__main__':unittest.main()
