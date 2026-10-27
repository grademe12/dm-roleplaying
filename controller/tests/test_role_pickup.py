"""Pickup instrumentation: a role step's trace splits session wait from model work.

The driver stamps role_prepared when it writes the input; the session stamps
pickup when it picks the input up (role_response.py pickup); the driver stamps
role_completed when it accepts the response. Without a pickup marker the trace
keeps its old shape, so uninstrumented flows are unaffected.
"""
import json
from pathlib import Path
import unittest

from common import ControlError
import role_response
import test_role_executor as role_fixtures


class RolePickupTests(unittest.TestCase):
    setUp = role_fixtures.RoleExecutorTests.setUp
    start = role_fixtures.RoleExecutorTests.start
    context = role_fixtures.RoleExecutorTests.context
    answer = role_fixtures.RoleExecutorTests.answer

    def traces(self):
        return [json.loads(line) for line in
                (self.worker.folder / 'trace.jsonl').read_text().splitlines()]

    def mark_pickup(self, wait_ms):
        pending = self.worker.load()['pending']
        input_path = self.worker.folder / ('role-model-input-' + pending['id'] + '.json')
        result = role_response.pickup(str(input_path), now=pending['prepared_ms'] + wait_ms)
        self.assertEqual(result['reason'], 'pickup_recorded')
        return pending

    def test_completed_step_trace_splits_wait_and_work(self):
        self.start()
        pending = self.mark_pickup(30_000)
        # The clock runs from the step's start: 30s until pickup, then 60s of work.
        self.c.sleep(90)
        self.answer(self.context())
        entry = next(e for e in self.traces() if e['phase'] == 'role_completed'
                     and e['request_id'] == pending['id'])
        self.assertEqual(entry['pickup_wait_ms'], 30_000)
        self.assertEqual(entry['model_work_ms'], 60_000)
        marker = self.worker.folder / ('role-model-pickup-' + pending['id'] + '.json')
        self.assertFalse(marker.exists())

    def test_step_without_marker_keeps_old_trace_shape(self):
        self.start()
        pending = self.worker.load()['pending']
        self.answer(self.context())
        entry = next(e for e in self.traces() if e['phase'] == 'role_completed'
                     and e['request_id'] == pending['id'])
        self.assertNotIn('pickup_wait_ms', entry)
        self.assertNotIn('model_work_ms', entry)

    def test_rejected_attempt_trace_splits_wait_and_work(self):
        self.start()
        pending = self.mark_pickup(10_000)
        self.c.sleep(30)   # 10s until pickup, then 20s of work
        value = self.context()
        value['decision']['dm_decisions'][0]['parts'] = ['unreviewed']
        self.answer(value)
        entry = next(e for e in self.traces()
                     if e['phase'] in ('role_correction', 'role_format_correction')
                     and e.get('reason') == 'role_context_wrote_draft')
        self.assertEqual(entry['pickup_wait_ms'], 10_000)
        self.assertEqual(entry['model_work_ms'], 20_000)

    def test_pickup_first_stamp_wins(self):
        self.start()
        pending = self.worker.load()['pending']
        input_path = self.worker.folder / ('role-model-input-' + pending['id'] + '.json')
        first = role_response.pickup(str(input_path), now=111_000)
        second = role_response.pickup(str(input_path), now=222_000)
        self.assertEqual(first['reason'], 'pickup_recorded')
        self.assertEqual(second['reason'], 'pickup_already_recorded')
        self.assertEqual(second['picked_up_ms'], 111_000)

    def test_pickup_rejects_invalid_request_id(self):
        path = Path(self.c.tmp.name) / 'bad-input.json'
        path.write_text(json.dumps({'request_id': 'not-hex'}))
        with self.assertRaisesRegex(ControlError, 'invalid_role_request_id'):
            role_response.pickup(str(path))


if __name__ == '__main__':
    unittest.main()
