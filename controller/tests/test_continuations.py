"""Offline continuation scheduling and interruption races against real SQLite."""
import unittest

import test_controller as baseline
import test_reply_latency as latency
from common import ControlError
from dispatch import MockTransport
import gate


class ContinuationTests(unittest.TestCase):
    tearDown = baseline.ControllerTests.tearDown
    snapshot = baseline.ControllerTests.snapshot
    decision = baseline.ControllerTests.decision
    review = baseline.ControllerTests.review
    ingest = baseline.ControllerTests.ingest
    activity = baseline.ControllerTests.activity
    initiate = baseline.ControllerTests.initiate
    free = latency.ReplyLatencyTests.free

    def setUp(self):
        baseline.ControllerTests.setUp(self)
        self.free()

    def group(self, gid, *, ids=None, generation=1, **kw):
        d = {'action':'reply' if ids else 'initiate', 'group_id':gid,
             'parts':[gid], 'due_ms':self.clock, 'reason':'requested continuation',
             'continuation':{'id':'run1','generation':generation}}
        if ids: d['message_ids'] = ids
        d.update(kw)
        return d

    def start(self, size=3, **kw):
        self.ingest(text='계속 이야기해 줘. 멈추라고 할 때까지.')
        op = {'action':'start','id':'run1','source_message_id':'m1','reason':'explicit user request'}
        op.update(kw)
        groups = [self.group('g1',ids=['m1'])] + [self.group('g'+str(i)) for i in range(2,size+1)]
        self.store.apply('main',self.decision(continuation_ops=[op],dm_decisions=groups,
                         processed_event_ids=['dm:m1']),self.clock)

    def run_state(self):
        return self.snapshot()['continuations'][0]

    def op(self, action, **kw):
        op = {'action':action,'id':'run1','expected_generation':self.run_state()['generation'],'reason':'test '+action}
        op.update(kw)
        return op

    def send(self, scenario='confirmed'):
        return self.dispatch.send_due('main',MockTransport(scenario))

    def jobs(self):
        with self.store.connect() as db:
            return {r['id']:r['status'] for r in db.execute('SELECT id,status FROM dm_jobs')}

    def test_three_groups_use_ordered_cadence_and_refill_with_existing_wake(self):
        self.start()
        self.assertEqual(self.run_state()['buffered'],3)
        self.assertEqual(self.send()['results'][0]['job_id'],'g1:0')
        self.assertEqual(self.send()['results'],[])
        self.clock += 60000
        self.assertEqual(self.send()['results'][0]['job_id'],'g2:0')
        snap = self.snapshot()
        self.assertEqual(snap['continuation_due_ids'],['run1'])
        self.assertIn('continuation_due',snap['work_manifest']['life_required'])
        reasons, detail = gate.evaluate(self.store.tick('main',self.clock,view='gate'),{'last_run_ms':self.clock},self.clock)
        self.assertIn('decision_due',reasons)
        self.assertEqual(detail['fingerprint_basis']['continuations'],[('run1',1)])
        self.store.apply('main',self.decision(dm_decisions=[self.group('g4'),self.group('g5')]),self.clock)
        self.assertEqual(self.run_state()['buffered'],3)
        self.assertEqual(self.snapshot()['continuation_due_ids'],[])
        self.clock += 60000
        self.assertEqual(self.send()['results'][0]['job_id'],'g3:0')

    def test_buffer_overflow_rolls_back_entire_decision(self):
        self.start()
        rev = self.snapshot()['revision']
        with self.assertRaisesRegex(ControlError,'continuation_buffer_full'):
            self.store.apply('main',self.decision(dm_decisions=[self.group('g4')]),self.clock)
        self.assertEqual(self.snapshot()['revision'],rev)
        self.assertEqual(set(self.jobs()),{'g1:0','g2:0','g3:0'})

    def test_general_initiate_still_has_single_active_guard(self):
        self.start(); self.send()
        with self.assertRaisesRegex(ControlError,'initiate_already_active'):
            self.initiate(gid='ordinary')

    def test_new_input_immediately_cancels_lookahead_and_duplicate_does_not_bump_generation(self):
        self.start()
        self.ingest('m2',text='멈춰')
        run = self.run_state()
        self.assertEqual((run['status'],run['generation'],run['buffered']),('paused',2,0))
        self.assertEqual(set(self.jobs().values()),{'cancelled'})
        self.assertEqual(self.send()['results'],[])
        self.ingest('m2',text='멈춰')
        self.assertEqual(self.run_state()['generation'],2)
        self.store.apply('main',self.decision(continuation_ops=[self.op('stop')]),self.clock)
        self.assertEqual(self.run_state()['status'],'stopped')

    def test_resume_requires_fresh_generation_and_fresh_groups(self):
        self.start(); self.send()
        self.ingest('m2',text='그 이야기 이어서 해')
        self.store.apply('main',self.decision(continuation_ops=[self.op('resume',source_message_id='m2')],
            dm_decisions=[self.group('fresh',ids=['m2'],generation=3),self.group('fresh2',generation=3)],
            processed_event_ids=['dm:m2']),self.clock)
        self.assertEqual((self.run_state()['status'],self.run_state()['generation']),('active',3))
        self.assertEqual(self.jobs()['g2:0'],'cancelled')
        self.clock += 6000
        self.assertEqual(self.send()['results'][0]['job_id'],'fresh:0')
        with self.assertRaisesRegex(ControlError,'continuation_generation_conflict'):
            self.store.apply('main',self.decision(dm_decisions=[self.group('stale',generation=1)]),self.clock)

    def test_incoming_during_transport_preserves_confirmed_part_but_sends_nothing_else(self):
        self.ingest()
        self.store.apply('main',self.decision(continuation_ops=[{'action':'start','id':'run1',
            'source_message_id':'m1','reason':'request'}],dm_decisions=[
            self.group('g1',ids=['m1'],parts=['first','second']),self.group('g2')]),self.clock)
        original_sleep = self.dispatch.sleep
        def incoming_during_gap(seconds):
            original_sleep(seconds)
            self.ingest('m2',text='멈춰')
        self.dispatch.sleep = incoming_during_gap
        result = self.send()
        self.assertEqual(len(result['results']),1)
        self.assertEqual(self.jobs(),{'g1:0':'confirmed','g1:1':'cancelled','g2:0':'cancelled'})

    def test_stop_while_attempting_preserves_unresolved_evidence(self):
        self.start()
        test = self
        class InterruptingTransport(MockTransport):
            def send(self, job):
                test.ingest('m2',text='그만')
                return super().send(job)
        result = self.dispatch.send_due('main',InterruptingTransport())
        self.assertEqual(len(result['results']),1)
        self.assertEqual(self.jobs(),{'g1:0':'confirmed','g2:0':'cancelled','g3:0':'cancelled'})
        self.assertEqual(self.run_state()['status'],'paused')
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT status FROM dm_messages WHERE id='m1'").fetchone()[0],'handled')

    def test_expiry_is_checked_before_transport_without_model_turn(self):
        self.start(expires_ms=self.clock+1000)
        self.clock += 1000
        self.assertEqual(self.send()['results'],[])
        self.assertEqual(self.run_state()['status'],'expired')
        self.assertEqual(set(self.jobs().values()),{'cancelled'})

    def test_only_actual_failure_pauses_run_while_uncertainty_continues(self):
        for scenario in ('failed','unknown','submitted'):
            with self.subTest(scenario=scenario):
                if scenario != 'failed':
                    self.tearDown(); self.setUp()
                self.start()
                self.send(scenario)
                self.assertEqual(self.run_state()['status'],'paused' if scenario=='failed' else 'active')
                self.assertEqual(self.snapshot()['continuation_due_ids'],[])
                self.assertEqual(self.jobs()['g2:0'],'cancelled' if scenario=='failed' else 'pending')
                if scenario != 'failed':
                    self.assertEqual(self.jobs()['g1:0'],scenario)
                    self.clock += 60000
                    self.assertEqual(self.send()['results'][0]['job_id'],'g2:0')

    def test_reconcile_unknown_finalizes_cancelled_tail_without_resuming(self):
        self.start(); self.send('unknown')
        self.store.apply('main',self.decision(continuation_ops=[self.op('pause')]),self.clock)
        with self.store.connect() as db:
            job = dict(db.execute("SELECT * FROM dm_jobs WHERE id='g1:0'").fetchone())
        self.dispatch.reconcile('main',{'job_id':'g1:0','attempt_token':job['attempt_token'],
            'expected_revision':self.snapshot()['revision'], 'result':{'status':'confirmed',
            'provider_message_id':'proof','evidence':'exact test receipt'}})
        self.assertEqual(self.run_state()['buffered'],0)
        self.assertEqual(self.run_state()['status'],'paused')

    def test_due_continuation_must_be_refilled_deferred_or_closed(self):
        self.start(size=1)
        self.clock += 60000
        with self.assertRaisesRegex(ControlError,'continuation_review_required'):
            self.store.apply('main',self.decision(),self.clock)
        self.store.apply('main',self.decision(continuation_ops=[self.op('defer',review_at_ms=self.clock+120000)]),self.clock)
        self.assertEqual(self.snapshot()['continuation_due_ids'],[])
        self.clock += 120000
        self.assertEqual(self.snapshot()['continuation_due_ids'],['run1'])

    def test_finite_run_completes_at_group_limit(self):
        self.start(size=2,max_groups=2)
        self.send(); self.clock += 60000; self.send()
        self.assertEqual(self.run_state()['status'],'completed')
        self.assertEqual(self.snapshot()['continuation_due_ids'],[])

    def test_source_and_channel_must_match(self):
        with self.assertRaisesRegex(ControlError,'invalid_continuation_source'):
            self.start(channel='slack')
        self.assertEqual(self.snapshot()['continuations'],[])

    def test_old_generation_cannot_be_revalidated_or_retried(self):
        self.start()
        d = self.decision(dm_decisions=[{'action':'revalidate_group','group_id':'g2','reason':'old draft'}])
        with self.assertRaisesRegex(ControlError,'continuation_group_owned'):
            self.store.apply('main',d,self.clock)

    def test_later_due_group_cannot_be_overtaken(self):
        self.ingest()
        self.store.apply('main',self.decision(continuation_ops=[{'action':'start','id':'run1',
            'source_message_id':'m1','reason':'request'}],dm_decisions=[
            self.group('g1',ids=['m1'],due_ms=self.clock+30000),self.group('g2')]),self.clock)
        self.assertEqual(self.send()['results'],[])
        self.clock += 30000
        self.assertEqual(self.send()['results'][0]['job_id'],'g1:0')

    def test_dry_run_has_no_run_or_queue_side_effects(self):
        self.ingest()
        d = self.decision(continuation_ops=[{'action':'start','id':'run1','source_message_id':'m1','reason':'request'}],
                          dm_decisions=[self.group('g1',ids=['m1']),self.group('g2')])
        self.store.apply('main',d,self.clock,dry_run=True)
        self.assertEqual(self.snapshot()['continuations'],[])
        self.assertEqual(self.jobs(),{})

    def test_other_messenger_input_also_invalidates_shared_session_drafts(self):
        self.start()
        self.ingest('s1',observation={'identity':'platform','channel':'slack'})
        self.assertEqual(self.run_state()['status'],'paused')
        self.assertEqual(set(self.jobs().values()),{'cancelled'})

    def test_outgoing_observation_is_not_input_but_stale_drafts_pause_before_claim(self):
        self.start(); self.send()
        self.ingest('own',direction='outgoing')
        self.assertEqual(self.run_state()['status'],'active')
        self.clock += 60000
        self.assertEqual(self.send()['results'],[])
        self.assertEqual(self.run_state()['reason'],'context_changed')

    def test_unavailable_stops_queue_even_before_send_interval_expires(self):
        self.start(); self.send()
        self.clock += 1000
        with self.store.connect(True) as db:
            db.execute("UPDATE activities SET attention='unavailable' WHERE status='in_progress'")
        self.assertEqual(self.send()['results'],[])
        self.assertEqual(self.run_state()['reason'],'activity_unavailable')
        self.assertEqual(self.jobs()['g2:0'],'cancelled')

    def test_quiet_gap_is_not_shortened_by_requested_interval(self):
        self.start(interval_ms=5000)
        with self.store.connect(True) as db:
            db.execute("UPDATE activities SET attention='focused' WHERE status='in_progress'")
        self.send()
        self.clock += 5000
        self.assertEqual(self.send()['results'],[])
        self.clock += 595000
        self.assertEqual(self.send()['results'][0]['job_id'],'g2:0')

    def test_crashed_attempt_recovery_keeps_run_active_and_unknown_recorded(self):
        self.start()
        claimed, reason = self.dispatch._claim('main','mock',self.clock)
        self.assertEqual(reason,'claimed')
        self.clock += 120001
        self.dispatch.recover('main')
        self.assertEqual(self.run_state()['status'],'active')
        self.assertEqual(self.jobs()[claimed['id']],'unknown')
        self.assertEqual(self.jobs()['g2:0'],'pending')
        self.clock += 60000
        self.assertEqual(self.send()['results'][0]['job_id'],'g2:0')

    def test_late_negative_proof_does_not_pause_run_or_replay_old_draft(self):
        self.start(); self.send('unknown')
        with self.store.connect() as db:
            job = dict(db.execute("SELECT * FROM dm_jobs WHERE id='g1:0'").fetchone())
        self.dispatch.reconcile('main',{'job_id':job['id'],'attempt_token':job['attempt_token'],
            'expected_revision':self.snapshot()['revision'], 'result':{'status':'failed',
            'not_sent':True,'evidence':'exact proof of non delivery'}})
        self.assertEqual(self.jobs()['g1:0'],'failed')
        self.assertEqual(self.run_state()['status'],'active')
        self.clock += 60000
        self.assertEqual(self.send()['results'][0]['job_id'],'g2:0')

    def test_slack_uses_same_ordered_run_contract(self):
        self.ingest('s1',observation={'identity':'platform','channel':'slack'})
        self.store.apply('main',self.decision(continuation_ops=[{'action':'start','id':'run1',
            'channel':'slack','source_message_id':'s1','reason':'request'}],dm_decisions=[
            self.group('slack1',ids=['s1'],channel='slack'),self.group('slack2',channel='slack')]),self.clock)
        result = self.dispatch.send_due('main',MockTransport(),channel='slack')
        self.assertEqual(result['results'][0]['job_id'],'slack1:0')
        self.clock += 20000
        self.assertEqual(self.dispatch.send_due('main',MockTransport(),channel='slack')['results'],[])
        self.clock += 40000
        self.assertEqual(self.dispatch.send_due('main',MockTransport(),channel='slack')['results'][0]['job_id'],'slack2:0')

    def test_existing_database_adds_only_empty_continuation_tables(self):
        rev = self.snapshot()['revision']
        with self.store.connect(True) as db:
            db.execute('DROP TABLE continuation_groups')
            db.execute('DROP TABLE continuation_runs')
        self.assertEqual(self.snapshot()['revision'],rev)
        self.assertEqual(self.snapshot()['continuations'],[])

    def test_replay_does_not_duplicate_run_or_groups(self):
        self.ingest()
        d = self.decision(continuation_ops=[{'action':'start','id':'run1','source_message_id':'m1','reason':'request'}],
            dm_decisions=[self.group('g1',ids=['m1']),self.group('g2')])
        self.store.apply('main',d,self.clock)
        self.assertEqual(self.store.apply('main',d,self.clock)['reason'],'already_applied')
        self.assertEqual(self.run_state()['total'],2)

    def test_explicit_stop_with_attempt_in_flight_never_reopens_on_late_receipt(self):
        self.start()
        job, reason = self.dispatch._claim('main','mock',self.clock)
        self.assertEqual(reason,'claimed')
        self.store.apply('main',self.decision(continuation_ops=[self.op('stop')]),self.clock)
        self.dispatch._record('main',job,{'status':'unknown','evidence':'network interrupted'},self.clock)
        self.assertEqual(self.run_state()['status'],'stopped')
        self.assertEqual(self.jobs()['g1:0'],'unknown')
        self.dispatch.reconcile('main',{'job_id':'g1:0','attempt_token':job['attempt_token'],
            'expected_revision':self.snapshot()['revision'],'result':{'status':'confirmed',
            'provider_message_id':'late-proof','evidence':'late exact receipt'}})
        self.assertEqual(self.run_state()['status'],'stopped')
        self.clock += 60000
        self.assertEqual(self.send()['results'],[])

    def test_long_continuation_interval_does_not_delay_reply_to_new_input(self):
        self.start(interval_ms=3600000)
        self.send()
        self.clock += 6000
        self.ingest('m2',text='다른 얘기 하자')
        reply = {'action':'reply','message_ids':['m2'],'group_id':'new-reply','parts':['answer'],
                 'due_ms':self.clock,'reason':'answer new topic'}
        self.store.apply('main',self.decision(continuation_ops=[self.op('stop')],dm_decisions=[reply]),self.clock)
        self.assertEqual(self.send()['results'][0]['job_id'],'new-reply:0')


if __name__ == '__main__':
    unittest.main()
