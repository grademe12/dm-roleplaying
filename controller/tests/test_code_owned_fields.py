"""Code-owned clock fields.

Synthetic fixtures only. Clock fields are normalized by Store.apply against
the commit clock.
"""
import unittest

import test_controller as baseline
from common import ControlError, decode
import reply_generator
import role_preflight


class CodeOwnedFieldTests(unittest.TestCase):
    setUp = baseline.ControllerTests.setUp
    tearDown = baseline.ControllerTests.tearDown
    snapshot = baseline.ControllerTests.snapshot
    decision = baseline.ControllerTests.decision
    review = baseline.ControllerTests.review
    ingest = baseline.ControllerTests.ingest
    reply = baseline.ControllerTests.reply

    # -- §2A clock normalization -------------------------------------------

    def session_times(self):
        with self.store.connect() as db:
            row = db.execute("SELECT advanced_to_ms, next_decision_ms FROM sessions "
                             "WHERE id='main'").fetchone()
        return row[0], row[1]

    def test_stale_next_decision_normalized_keeping_gap(self):
        d = self.decision()
        submitted_next = d['next_decision_ms']
        self.clock += 7_200_000  # composed two hours before it is committed
        result = self.store.apply('main', d, self.clock)
        self.assertEqual(result['normalized'],
                         {'next_decision_ms': {'from': submitted_next,
                                               'to': self.clock + 3_600_000}})
        self.assertEqual(self.session_times()[1], self.clock + 3_600_000)
        with self.store.connect() as db:
            row = db.execute("SELECT details_json FROM facts WHERE session_id='main' AND id=?",
                             ('decision:' + d['decision_id'],)).fetchone()
        details = decode(row[0])
        self.assertEqual(details['normalized']['next_decision_ms']['to'],
                         self.clock + 3_600_000)
        self.assertEqual(details['decision']['next_decision_ms'], submitted_next)

    def test_stale_next_decision_gap_floored_at_minimum(self):
        d = self.decision(next_decision_ms=self.clock + 30_000)
        self.clock += 120_000
        result = self.store.apply('main', d, self.clock)
        self.assertEqual(result['normalized']['next_decision_ms']['to'], self.clock + 60_000)

    def test_future_advance_clamped_backwards_still_rejected(self):
        d = self.decision(advance_to_ms=self.clock + 999_999)
        result = self.store.apply('main', d, self.clock)
        self.assertEqual(result['normalized']['advance_to_ms'],
                         {'from': self.clock + 999_999, 'to': self.clock})
        self.assertEqual(self.session_times()[0], self.clock)
        with self.assertRaisesRegex(ControlError, 'invalid_advance_time'):
            self.store.apply('main', self.decision(advance_to_ms=self.clock - 1), self.clock)

    def test_resubmit_after_normalization_is_already_applied(self):
        d = self.decision()
        self.clock += 7_200_000
        self.store.apply('main', d, self.clock)
        again = self.store.apply('main', d, self.clock + 60_000)
        self.assertEqual(again['reason'], 'already_applied')

    def test_warning_receipt_resubmit_still_commits_with_normalization(self):
        self.ingest()
        dm = {'action': 'reply', 'message_ids': ['m1'], 'group_id': 'g1',
              'due_ms': self.clock, 'parts': ['reply'], 'reason': 'r'}
        d = self.decision(dm_decisions=[dm], processed_event_ids=['dm:m1'])
        self.clock += 7_200_000
        extra = {'g1': [{'reason': 'repetition_detected', 'verbatim_max': 30}]}
        first = self.store.apply('main', d, self.clock, guard_warnings=True,
                                 extra_warnings=extra)
        self.assertEqual(first['reason'], 'warning_review_required')
        self.assertIn('normalized', first)
        second = self.store.apply('main', d, self.clock, guard_warnings=True,
                                  warning_review=first['warning_review'],
                                  extra_warnings=extra)
        self.assertEqual(second['reason'], 'applied')
        self.assertIn('normalized', second)

    def test_preflight_accepts_past_next_decision(self):
        role_preflight.future({'next_decision_ms': 1_000}, 2_000)
        with self.assertRaisesRegex(ControlError, 'next_decision_not_future'):
            role_preflight.future({'next_decision_ms': 'soon'}, 2_000)


if __name__ == '__main__':
    unittest.main()
