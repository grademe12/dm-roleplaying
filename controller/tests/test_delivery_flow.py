"""Conversation progresses without inventing delivery evidence or retrying unknowns."""
import unittest

import test_controller as baseline
import test_continuations as continuation
from common import ControlError
from dispatch import MockTransport
import gate


class DeliveryFlowTests(unittest.TestCase):
    setUp = continuation.ContinuationTests.setUp
    tearDown = baseline.ControllerTests.tearDown
    snapshot = baseline.ControllerTests.snapshot
    decision = baseline.ControllerTests.decision
    review = baseline.ControllerTests.review
    ingest = baseline.ControllerTests.ingest
    activity = baseline.ControllerTests.activity
    reply = baseline.ControllerTests.reply
    initiate = baseline.ControllerTests.initiate
    free = continuation.ContinuationTests.free
    group = continuation.ContinuationTests.group
    start = continuation.ContinuationTests.start
    run_state = continuation.ContinuationTests.run_state
    jobs = continuation.ContinuationTests.jobs
    op = continuation.ContinuationTests.op
    send = continuation.ContinuationTests.send

    def proof(self, jid, status='confirmed'):
        with self.store.connect() as db:
            job = dict(db.execute('SELECT * FROM dm_jobs WHERE id=?',(jid,)).fetchone())
        result = ({'status':'confirmed','provider_message_id':'proof:'+jid,'evidence':'exact test receipt'}
                  if status=='confirmed' else {'status':'failed','not_sent':True,'evidence':'proven not delivered'})
        return self.dispatch.reconcile('main',{'job_id':jid,'attempt_token':job['attempt_token'],
            'expected_revision':self.snapshot()['revision'],'result':result})

    def passes(self):
        with self.store.connect() as db:
            return {r['job_id']:r['passed_ms'] for r in db.execute('SELECT * FROM dm_flow_passes')}

    def test_unknown_burst_finishes_flow_once_without_claiming_confirmed_delivery(self):
        self.ingest(); self.reply(['one','two','three'])
        result = self.send('unknown')
        self.assertEqual([r['job_id'] for r in result['results']],['g1:0','g1:1','g1:2'])
        self.assertEqual(set(self.jobs().values()),{'unknown'})
        snap = self.snapshot()
        self.assertEqual(snap['messages'],[])
        self.assertFalse(snap['dispatch_hold'])
        self.assertEqual(len(snap['delivery_uncertain_job_ids']),3)
        self.assertTrue(all(j['flow_passed_ms'] is not None for j in snap['jobs']))
        self.assertNotIn('dispatch_unresolved',snap['work_manifest']['repair'])
        reasons, _ = gate.evaluate(snap,{'last_run_ms':self.clock},self.clock)
        self.assertNotIn('dispatch_unresolved',reasons)
        self.assertEqual(snap['work_manifest']['primary'],'idle')
        self.clock += 600000
        self.assertEqual(self.send()['results'],[])
        self.assertEqual(set(self.jobs().values()),{'unknown'})

    def test_new_reply_and_new_proactive_group_do_not_require_unknown_resolution(self):
        self.ingest(); self.reply(); self.send('submitted')
        self.clock += 6000
        self.ingest('m2')
        self.store.apply('main',self.decision(dm_decisions=[{'action':'reply','message_ids':['m2'],
            'group_id':'g2','parts':['new answer'],'due_ms':self.clock,'reason':'answer new input'}],
            processed_event_ids=['dm:m2']),self.clock)
        self.assertEqual(self.send()['results'][0]['job_id'],'g2:0')
        self.assertEqual(self.jobs()['g1:0'],'submitted')

    def test_unknown_proactive_releases_single_active_slot(self):
        self.initiate(gid='p1'); self.send('unknown')
        self.clock += 240000
        self.initiate(gid='p2')
        self.assertEqual(self.send()['results'][0]['job_id'],'p2:0')
        self.assertEqual(self.jobs()['p1:0'],'unknown')

    def test_real_failure_still_stops_burst_and_does_not_get_flow_pass(self):
        self.ingest(); self.reply(['one','two'])
        result = self.send('failed')
        self.assertEqual([r['status'] for r in result['results']],['failed'])
        self.assertEqual(self.passes(),{})
        self.clock += 600000
        self.assertEqual(self.send()['results'],[])
        self.assertEqual(self.jobs()['g1:1'],'pending')

    def test_live_attempt_serializes_but_recovered_unknown_allows_later_part(self):
        self.ingest(); self.reply(['one','two'])
        job, _ = self.dispatch._claim('main','mock',self.clock)
        self.assertEqual(self.send()['reason'],'dispatch_unresolved')
        self.clock += 120001
        self.dispatch.recover('main')
        self.clock += 12000
        self.assertEqual(self.send()['results'][0]['job_id'],'g1:1')
        self.assertEqual(self.jobs()[job['id']],'unknown')

    def test_unknown_part_can_be_closed_without_replaying_original_request(self):
        self.ingest(); self.reply(['one','two'])
        self.dispatch.send_due('main',MockTransport('unknown'),limit=1)
        with self.assertRaisesRegex(ControlError,'partially_sent_group'):
            self.store.apply('main',self.decision(dm_decisions=[{'action':'cancel_group','group_id':'g1','reason':'stop'}]),self.clock)
        self.store.apply('main',self.decision(dm_decisions=[{'action':'close_partial_group','group_id':'g1','reason':'stop'}]),self.clock)
        self.assertEqual(self.jobs(),{'g1:0':'unknown','g1:1':'cancelled'})
        self.assertEqual(self.snapshot()['messages'],[])
        self.proof('g1:0')
        self.clock += 600000
        self.assertEqual(self.send()['results'],[])

    def test_new_input_stales_pending_tail_but_unknown_does_not_prevent_revalidation(self):
        self.ingest(); self.reply(['one','two'])
        self.dispatch.send_due('main',MockTransport('unknown'),limit=1)
        self.ingest('m2')
        self.clock += 12000
        self.assertEqual(self.send()['reason'],'draft_stale')
        self.store.apply('main',self.decision(dm_decisions=[
            {'action':'revalidate_group','group_id':'g1','reason':'tail still relevant'},
            {'action':'no_reply','message_ids':['m2'],'reason':'included in current tail'}],
            processed_event_ids=['dm:m2']),self.clock)
        self.assertEqual(self.send()['results'][0]['job_id'],'g1:1')
        self.assertEqual(self.jobs()['g1:0'],'unknown')

    def test_unknown_run_keeps_refilling_and_respects_buffer_limit(self):
        self.start()
        self.send('unknown'); self.clock += 60000; self.send('submitted')
        self.assertEqual(self.run_state()['status'],'active')
        self.assertEqual(self.snapshot()['continuation_due_ids'],['run1'])
        self.store.apply('main',self.decision(dm_decisions=[self.group('g4'),self.group('g5')]),self.clock)
        self.assertEqual(self.run_state()['buffered'],3)
        self.assertEqual(self.jobs()['g1:0'],'unknown')
        self.assertEqual(self.jobs()['g2:0'],'submitted')
        self.clock += 60000
        self.assertEqual(self.send()['results'][0]['job_id'],'g3:0')

    def test_incoming_still_stops_unknown_run_and_resume_needs_no_receipt(self):
        self.start(); self.send('unknown')
        self.ingest('m2',text='이어서 해')
        self.assertEqual(self.run_state()['status'],'paused')
        self.store.apply('main',self.decision(continuation_ops=[self.op('resume',source_message_id='m2')],
            dm_decisions=[self.group('fresh',ids=['m2'],generation=3)],processed_event_ids=['dm:m2']),self.clock)
        self.clock += 6000
        self.assertEqual(self.send()['results'][0]['job_id'],'fresh:0')
        self.assertEqual(self.jobs()['g1:0'],'unknown')

    def test_late_receipt_changes_evidence_without_moving_cadence_or_reopening_group(self):
        self.start(); self.send('unknown')
        self.clock += 60000; self.send('unknown')
        original = self.passes()
        self.clock += 30000; self.proof('g1:0')
        self.assertEqual(self.passes(),original)
        self.clock += 30000
        self.assertEqual(self.send()['results'][0]['job_id'],'g3:0')
        self.assertEqual(self.jobs()['g1:0'],'confirmed')
        self.assertEqual(self.jobs()['g2:0'],'unknown')

    def test_late_negative_proof_does_not_block_pending_tail_or_retry_first_part(self):
        self.ingest(); self.reply(['one','two'])
        self.dispatch.send_due('main',MockTransport('unknown'),limit=1)
        self.proof('g1:0','failed')
        self.clock += 12000
        self.assertEqual(self.send()['results'][0]['job_id'],'g1:1')
        self.assertEqual(self.jobs()['g1:0'],'failed')

    def test_pre_patch_unknown_queue_advances_without_manual_reconcile(self):
        self.ingest(); self.reply(['one','two'])
        self.dispatch.send_due('main',MockTransport('unknown'),limit=1)
        # Drop only the new ledger to represent the pre-patch database.
        with self.store.connect(True) as db:
            db.execute('DROP TABLE dm_flow_passes')
            db.execute('UPDATE channel_pacing SET next_send_after_ms=?',(self.clock+240000,))
        self.clock += 12000
        self.assertEqual(self.send()['results'][0]['job_id'],'g1:1')
        self.assertEqual(self.jobs()['g1:0'],'unknown')


if __name__ == '__main__':
    unittest.main()
