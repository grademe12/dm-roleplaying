"""Five-part creation/dispatch bounds and interruption behavior, offline only."""
from contextlib import redirect_stdout
import io
import unittest
from unittest.mock import patch

import ctl
from common import ControlError
from dispatch import MockTransport, burst_gap_ms
import test_controller as baseline


class MessageLimitTests(unittest.TestCase):
    setUp = baseline.ControllerTests.setUp
    tearDown = baseline.ControllerTests.tearDown
    snapshot = baseline.ControllerTests.snapshot
    decision = baseline.ControllerTests.decision
    review = baseline.ControllerTests.review
    ingest = baseline.ControllerTests.ingest
    reply = baseline.ControllerTests.reply

    def test_six_or_more_new_parts_roll_back_the_whole_decision(self):
        for action in ('initiate', 'reply'):
            for count in (6,12):
                with self.subTest(action=action,count=count):
                    if action == 'reply':
                        self.ingest()
                    dm = {'action':action,'group_id':'oversized','due_ms':self.clock,
                          'parts':['actual explanation']*count,'reason':'test oversized input'}
                    if action == 'reply':
                        dm['message_ids']=['m1']
                    before = self.snapshot()
                    fact = {'id':'must-roll-back','source':'simulated','summary':'Must not be stored'}
                    decision = self.decision(facts=[fact],dm_decisions=[dm])
                    with self.assertRaisesRegex(ControlError,'parts_limit_exceeded'):
                        self.store.apply('main',decision,self.clock)
                    self.assertEqual(self.snapshot(),before)
                    with self.store.connect() as db:
                        self.assertIsNone(db.execute("SELECT 1 FROM facts WHERE id='must-roll-back'").fetchone())

    def test_send_limit_above_five_is_rejected_without_transport_or_state_change(self):
        self.ingest();self.reply(['one','two','three','four','five'])
        before = self.snapshot()
        transport = MockTransport()
        with patch.object(transport,'send') as send:
            with self.assertRaisesRegex(ControlError,'limit_too_large'):
                self.dispatch.send_due('main',transport,limit=6)
            send.assert_not_called()
        self.assertEqual(self.snapshot(),before)

    def test_new_message_after_second_part_holds_the_remaining_three(self):
        self.ingest();self.reply(['one','two','three','four','five'])
        outer = self
        class IncomingDuringSend(MockTransport):
            def send(self,job):
                result = super().send(job)
                if job['part_index']==1:
                    outer.ingest('m2',text='Wait, a new question')
                return result
        result = self.dispatch.send_due('main',IncomingDuringSend())
        self.assertEqual([r['job_id'] for r in result['results']],['g1:0','g1:1'])
        self.assertEqual(result['reason'],'draft_stale')
        self.assertEqual([j['id'] for j in self.snapshot()['jobs'] if j['status']=='pending'],
                         ['g1:2','g1:3','g1:4'])

    def test_fourth_failure_stops_the_fifth_without_replaying_earlier_parts(self):
        self.ingest();self.reply(['one','two','three','four','five'])
        class FailFourth(MockTransport):
            def send(self,job):
                if job['part_index']==3:
                    return {'status':'failed','not_sent':True,'evidence':'mock definite failure'}
                return super().send(job)
        result = self.dispatch.send_due('main',FailFourth())
        self.assertEqual([r['job_id'] for r in result['results']],[f'g1:{i}' for i in range(4)])
        self.assertEqual(result['reason'],'failed')
        self.assertEqual(self.dispatch.send_due('main',MockTransport())['results'],[])

    def test_cli_default_batch_matches_the_five_part_policy(self):
        with redirect_stdout(io.StringIO()), patch.object(ctl.Dispatcher,'send_due',
                return_value={'ok':True,'reason':'mock','revision':0}) as send:
            self.assertEqual(ctl.main(['--db',str(self.path),'send-due','--dry-run']),0)
        self.assertEqual(send.call_args.args[2],5)

    def test_short_and_long_bubbles_stay_within_three_to_five_seconds(self):
        self.assertEqual(burst_gap_ms('x'),3000)
        self.assertEqual(burst_gap_ms('x'*10000),5000)
        gaps = [burst_gap_ms('x'*length) for length in range(1,40)]
        self.assertTrue(all(3000<=gap<=5000 for gap in gaps))
        self.assertEqual(gaps,sorted(gaps))


if __name__ == '__main__':
    unittest.main()
