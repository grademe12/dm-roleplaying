"""A failing history page cannot hold unrelated Slack sends indefinitely."""
import importlib.util
import io
import json
import sys
import unittest
from contextlib import redirect_stdout
from http.client import IncompleteRead
from pathlib import Path
from unittest.mock import patch

import test_controller as baseline
import ctl
from common import ControlError
from dispatch import MockTransport
import slack_delivery as delivery

spec = importlib.util.spec_from_file_location('history_progress_listener',
    Path(__file__).resolve().parents[1] / 'slack/slack_listener.py')
listener = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = listener
spec.loader.exec_module(listener)
worker_spec = importlib.util.spec_from_file_location('receive_only_delivery_worker',
    Path(__file__).resolve().parents[1] / 'slack/delivery_worker.py')
worker_module = importlib.util.module_from_spec(worker_spec)
worker_spec.loader.exec_module(worker_module)

SETTINGS = {'correlation_enabled': True, 'auto_confirm_enabled': True, 'metadata_verified': True,
            'history_verified': True, 'events_verified': True, 'mode': 'event',
            'workspace_id': 'T1', 'channel_id': 'D1', 'self_user_id': 'USELF', 'app_id': 'A1',
            'event_type': 'persona.delivery', 'history_min_interval_ms': 1000}


class SlackHistoryProgressTests(unittest.TestCase):
    def setUp(self):
        self.case = baseline.ControllerTests()
        self.case.setUp()
        self.addCleanup(self.case.tearDown)
        self.commands = []
        delivery.configure(self.case.store, 'main', SETTINGS, self.case.clock)

    def run_ctl(self, *args):
        self.commands.append(args)
        output = io.StringIO()
        with patch.object(ctl, 'now_ms', return_value=self.case.clock), redirect_stdout(output):
            code = ctl.main(['--db', str(self.case.path), *args])
        return code, json.loads(output.getvalue())

    def bridge(self, history):
        c = self.case
        cfg = {'session': 'main', 'state_dir': str(c.path.parent / 'state'),
               'slack': {'channel': 'D1', 'peer_user_id': 'UPEER', 'self_user_id': 'USELF',
                         'ingest_after_ts': f'{(baseline.T - 86400000)//1000}.000000',
                         'delivery_receipts_enabled': True}}
        bridge = listener.SlackBridge(cfg, history, self.run_ctl, clock=lambda: c.clock)
        self.assertTrue(bridge.set_delivery_identity({'ok': True, 'team_id': 'T1', 'user_id': 'USELF'}))
        return bridge

    def queue(self, parts=None):
        c = self.case
        dm = {'action': 'initiate', 'channel': 'slack', 'group_id': 'g1', 'due_ms': c.clock,
              'parts': ['first', 'second'] if parts is None else parts, 'reason': 'fixture'}
        c.store.apply('main', c.decision(dm_decisions=[dm]), c.clock)

    def event(self, user='USELF', fraction='000001', **extra):
        return {'type': 'message', 'channel': 'D1', 'user': user,
                'ts': f'{baseline.T//1000}.{fraction}', 'text': 'fixture', **extra}

    def incomplete_history(self, messages):
        calls = []
        def history(**kwargs):
            calls.append(kwargs)
            if kwargs.get('cursor'):
                raise IncompleteRead(b'')
            self.case.clock += 1000  # The next page has a valid shared history budget slot.
            return {'messages': messages, 'has_more': True,
                    'response_metadata': {'next_cursor': 'broken-page'}}
        return history, calls

    def assert_incomplete(self, bridge):
        with self.assertLogs(listener.__name__, level='WARNING'):
            result = bridge.backfill()
        self.assertFalse(result.ok)
        self.assertIn('IncompleteRead', bridge.health['alerts']['backfill_failed']['detail'])
        return result

    def test_legacy_persistent_block_does_not_prevent_normal_dispatch(self):
        c = self.case
        self.queue()
        delivery.history_sync(c.store, 'main', True, c.clock)
        c.clock += 60 * 60000
        sent = c.dispatch.send_due('main', MockTransport(), channel='slack')
        self.assertEqual([r['job_id'] for r in sent['results']], ['g1:0', 'g1:1'])
        with c.store.connect() as db:
            self.assertEqual(db.execute('SELECT blocked FROM slack_history_sync').fetchone()[0], 1)

    def test_repeated_second_page_failure_and_restart_do_not_block_new_send(self):
        c = self.case
        self.queue()
        delivery.history_sync(c.store, 'main', True, c.clock)  # State left by an old listener.
        history, calls = self.incomplete_history([self.event()])
        bridge = self.bridge(history)
        watermark = bridge.backfill_cursor
        self.assert_incomplete(bridge)
        self.assertTrue(bridge.backfill_scan_path.is_file())
        self.assertEqual(bridge.backfill_cursor, watermark)
        for _ in range(2):
            c.clock += 60000
            bridge = self.bridge(history)
            self.assert_incomplete(bridge)
            self.assertEqual(bridge.backfill_cursor, watermark)
        self.assertEqual([call.get('cursor') for call in calls], [None, 'broken-page', 'broken-page', 'broken-page'])
        self.assertFalse(any(command[0] == 'slack-history-sync' for command in self.commands))
        sent = c.dispatch.send_due('main', MockTransport(), channel='slack')
        self.assertEqual(len(sent['results']), 2)

    def test_known_incoming_is_ingested_before_second_page_failure_and_stales_draft(self):
        c = self.case
        self.queue()
        incoming = self.event(user='UPEER')
        history, _ = self.incomplete_history([incoming])
        bridge = self.bridge(history)
        watermark = bridge.backfill_cursor
        self.assert_incomplete(bridge)
        snap = c.snapshot()
        self.assertEqual([m['id'] for m in snap['messages']], ['slack:D1:' + incoming['ts']])
        self.assertEqual(bridge.backfill_cursor, watermark)
        self.assertEqual(c.dispatch.send_due('main', MockTransport(), channel='slack')['reason'], 'draft_stale')
        c.clock += 1000
        self.assert_incomplete(bridge)
        self.assertEqual(len(c.snapshot()['messages']), 1)

    def test_legacy_buffer_is_ingested_without_resuming_delivery_cursor(self):
        c = self.case
        self.queue()
        bridge = self.bridge(lambda **kwargs: (_ for _ in ()).throw(IncompleteRead(b'')))
        incoming = self.event(user='UPEER')
        listener.write_json(bridge.legacy_delivery_scan_path, {'channel': 'D1', 'oldest': bridge.backfill_cursor,
            'inclusive': False, 'cursor': 'broken-page', 'messages': [incoming], 'highwater': incoming['ts']})
        self.assert_incomplete(bridge)
        self.assertFalse(bridge.legacy_delivery_scan_path.exists())
        self.assertEqual(c.snapshot()['messages'][0]['id'], 'slack:D1:' + incoming['ts'])
        self.assertEqual(c.dispatch.send_due('main', MockTransport(), channel='slack')['reason'], 'draft_stale')

    def test_receive_budget_wait_preserves_unknown_and_never_collects_history_receipts(self):
        c = self.case
        self.queue()
        c.dispatch.send_due('main', MockTransport('unknown'), channel='slack', limit=1)
        with c.store.connect() as db:
            attempt = dict(db.execute('SELECT * FROM slack_delivery_attempts').fetchone())
            text = db.execute("SELECT text FROM dm_jobs WHERE id='g1:0'").fetchone()[0]
        posted = self.event(app_id='A1', text=delivery.escaped(text), metadata={
            'event_type': SETTINGS['event_type'], 'event_payload': {'delivery_id': attempt['nonce']}})
        pages = [{'messages': [posted], 'has_more': True, 'response_metadata': {'next_cursor': 'page2'}},
                 {'messages': [], 'has_more': False}]
        bridge = self.bridge(lambda **kwargs: pages.pop(0))
        watermark = bridge.backfill_cursor
        result = bridge.backfill()
        self.assertTrue(result.deferred)
        self.assertEqual(bridge.backfill_cursor, watermark)
        with self.assertRaisesRegex(ControlError, 'delivery_evidence_missing'):
            c.dispatch.reconcile_slack_delivery('main', 'g1:0')
        c.clock += 60000
        sent = c.dispatch.send_due('main', MockTransport(), channel='slack')
        self.assertEqual([r['job_id'] for r in sent['results']], ['g1:1'])
        self.assertTrue(bridge.backfill().ok)
        self.assertFalse(bridge.backfill_scan_path.exists())
        with self.assertRaisesRegex(ControlError, 'delivery_evidence_missing'):
            c.dispatch.reconcile_slack_delivery('main', 'g1:0')
        delivery.observe(c.store, 'main', {'source': 'socket', 'workspace_id': 'T1', 'message': posted}, c.clock)
        self.assertEqual(c.dispatch.reconcile_slack_delivery('main', 'g1:0')['reason'], 'delivery_confirmed')
        c.clock += 15000
        sent = c.dispatch.send_due('main', MockTransport(), channel='slack')
        self.assertEqual(sent['results'], [])

    def test_failed_ingest_keeps_alert_retry_and_recovery_watermark(self):
        c = self.case
        incoming = self.event(user='UPEER')
        outgoing = self.event(fraction='000000')
        bridge = self.bridge(lambda **kwargs: {'messages': [incoming, outgoing], 'has_more': False})
        watermark = bridge.backfill_cursor
        run = bridge.run_ctl
        def failing(*args):
            return (1, {'ok': False, 'reason': 'fixture_failure'}) if args[0] == 'ingest' else run(*args)
        bridge.run_ctl = failing
        with self.assertLogs(listener.__name__, level='WARNING'):
            result = bridge.backfill()
        self.assertFalse(result.ok)
        self.assertEqual(result.handled, 1)
        self.assertIn('ingest_failed', bridge.health['alerts'])
        self.assertEqual(bridge.retry_ts, incoming['ts'])
        self.assertEqual(bridge.backfill_cursor, watermark)
        self.assertEqual(c.snapshot()['messages'], [])
        c.clock += 1000
        bridge.run_ctl = run
        self.assertTrue(bridge.backfill().ok)
        self.assertIsNone(bridge.retry_ts)
        self.assertNotIn('ingest_failed', bridge.health['alerts'])
        self.assertEqual(len(c.snapshot()['messages']), 1)

    def test_socket_receipt_recovers_unknown_while_history_cursor_keeps_failing(self):
        c = self.case
        self.queue()
        c.dispatch.send_due('main', MockTransport('unknown'), channel='slack', limit=1)
        with c.store.connect() as db:
            attempt = dict(db.execute('SELECT * FROM slack_delivery_attempts').fetchone())
            text = db.execute("SELECT text FROM dm_jobs WHERE id='g1:0'").fetchone()[0]
        posted = self.event(app_id='A1', text=delivery.escaped(text), metadata={
            'event_type': SETTINGS['event_type'], 'event_payload': {'delivery_id': attempt['nonce']}})
        history, _ = self.incomplete_history([posted])
        bridge = self.bridge(history)
        delivery.history_sync(c.store, 'main', True, c.clock)
        self.assert_incomplete(bridge)
        with self.assertRaisesRegex(ControlError, 'delivery_evidence_missing'):
            c.dispatch.reconcile_slack_delivery('main', 'g1:0')
        c.clock += 60000
        delivery.observe(c.store, 'main', {'source': 'socket', 'workspace_id': 'T1', 'message': posted}, c.clock)
        self.assertEqual(c.dispatch.reconcile_slack_delivery('main', 'g1:0')['reason'], 'delivery_confirmed')
        self.assertTrue(bridge.backfill_scan_path.exists())
        sent = c.dispatch.send_due('main', MockTransport(), channel='slack')
        self.assertEqual([r['job_id'] for r in sent['results']], ['g1:1'])

    def test_late_socket_receipt_does_not_rewrite_pacing_of_later_part(self):
        c = self.case
        self.queue()
        c.dispatch.send_due('main', MockTransport('unknown'), channel='slack', limit=1)
        with c.store.connect() as db:
            attempt = dict(db.execute('SELECT * FROM slack_delivery_attempts').fetchone())
            text = db.execute("SELECT text FROM dm_jobs WHERE id='g1:0'").fetchone()[0]
        posted = self.event(app_id='A1', text=delivery.escaped(text), metadata={
            'event_type': SETTINGS['event_type'], 'event_payload': {'delivery_id': attempt['nonce']}})
        c.clock += 12000
        sent = c.dispatch.send_due('main', MockTransport(), channel='slack')
        self.assertEqual([r['job_id'] for r in sent['results']], ['g1:1'])
        with c.store.connect() as db:
            before = db.execute("SELECT next_send_after_ms FROM channel_pacing WHERE channel='slack'").fetchone()[0]
        c.clock += 1000
        delivery.observe(c.store, 'main', {'source':'socket','workspace_id':'T1','message':posted}, c.clock)
        c.dispatch.reconcile_slack_delivery('main','g1:0')
        with c.store.connect() as db:
            after = db.execute("SELECT next_send_after_ms FROM channel_pacing WHERE channel='slack'").fetchone()[0]
        self.assertEqual(before,after)

    def test_complete_receive_scan_never_publishes_delivery_receipts(self):
        c = self.case
        incoming = self.event(user='UPEER', fraction='000002')
        pages = [{'messages': [self.event()], 'has_more': True,
                  'response_metadata': {'next_cursor': 'page2'}},
                 {'messages': [incoming], 'has_more': False}]
        def history(**kwargs):
            c.clock += 1000
            return pages.pop(0)
        bridge = self.bridge(history)
        with patch.object(bridge, '_observe_delivery', side_effect=AssertionError('history receipt collection removed')):
            self.assertTrue(bridge.backfill().ok)
        self.assertEqual(c.snapshot()['messages'][0]['id'], 'slack:D1:' + incoming['ts'])
        self.assertEqual(bridge.backfill_cursor, incoming['ts'])
        self.assertFalse(bridge.backfill_scan_path.exists())

    def test_doorbell_receive_scan_can_confirm_exact_receipt_without_delivery_rewind(self):
        c = self.case
        self.queue(parts=['first'])
        c.dispatch.send_due('main',MockTransport('unknown'),channel='slack',limit=1)
        with c.store.connect() as db:
            attempt = dict(db.execute('SELECT * FROM slack_delivery_attempts').fetchone())
        posted = self.event(app_id='A1',text=delivery.escaped('first'),metadata={
            'event_type':SETTINGS['event_type'],'event_payload':{'delivery_id':attempt['nonce']}})
        calls = []
        def history(**kwargs):
            calls.append(kwargs)
            return {'messages':[posted],'has_more':False}
        bridge = self.bridge(history)
        boundary = bridge.backfill_cursor
        bridge.s['backfill_receipts_enabled'] = True
        self.assertTrue(bridge.backfill().ok)
        self.assertEqual(calls[0]['oldest'],boundary)
        self.assertEqual(len(calls),1)
        self.assertEqual(c.dispatch.reconcile_slack_delivery('main','g1:0')['reason'],'delivery_confirmed')

    def test_doorbell_receipt_storage_failure_preserves_receive_cursor(self):
        bridge = self.bridge(lambda **_:{'messages':[self.event(metadata={'event_type':'test'})],
                                        'has_more':False})
        bridge.s['backfill_receipts_enabled'] = True
        boundary = bridge.backfill_cursor
        original = bridge.run_ctl
        bridge.run_ctl = lambda *args: (1,{'ok':False}) if args[0]=='slack-delivery-observe' else original(*args)
        with self.assertLogs(listener.__name__,level='WARNING'):
            self.assertFalse(bridge.backfill().ok)
        self.assertEqual(bridge.backfill_cursor,boundary)

    def test_first_page_failure_does_not_advance_watermark_or_create_block(self):
        c = self.case
        self.queue(parts=['reply'])
        bridge = self.bridge(lambda **kwargs: (_ for _ in ()).throw(IncompleteRead(b'')))
        watermark = bridge.backfill_cursor
        self.assert_incomplete(bridge)
        self.assertEqual(bridge.backfill_cursor, watermark)
        self.assertFalse(bridge.backfill_scan_path.exists())
        with c.store.connect() as db:
            self.assertIsNone(db.execute('SELECT * FROM slack_history_sync').fetchone())
        self.assertEqual(len(c.dispatch.send_due('main', MockTransport(), channel='slack')['results']), 1)

    def test_worker_never_requests_history_and_still_uses_socket_receipts(self):
        c = self.case
        self.queue()
        c.dispatch.send_due('main', MockTransport('unknown'), channel='slack')
        cfg = {'session': 'main', 'state_dir': str(c.path.parent / 'state'),
               'slack': {'transport_config': 'unused'}}
        worker = worker_module.RecoveryWorker(cfg, self.run_ctl, lambda: c.clock)
        for _ in range(3):
            result = worker.cycle()
            self.assertEqual(result['state'], 'waiting_for_receipts')
            c.clock += 60000
        request_path = Path(cfg['state_dir']) / 'slack-delivery-history-request.json'
        self.assertFalse(request_path.exists())
        with c.store.connect() as db:
            attempt = dict(db.execute('SELECT * FROM slack_delivery_attempts').fetchone())
            text = db.execute("SELECT text FROM dm_jobs WHERE id='g1:0'").fetchone()[0]
        posted = self.event(app_id='A1', text=delivery.escaped(text), metadata={
            'event_type': SETTINGS['event_type'], 'event_payload': {'delivery_id': attempt['nonce']}})
        delivery.observe(c.store, 'main', {'source': 'socket', 'workspace_id': 'T1', 'message': posted}, c.clock)
        result = worker.cycle()
        self.assertEqual(result['confirmed'], ['g1:0'])
        self.assertEqual(result['dispatch']['reason'], 'listener_unhealthy')
        self.assertFalse(request_path.exists())
        self.assertFalse(any(args[0] in ('slack-history-claim', 'slack-history-backoff', 'send-due')
                             for args in self.commands))

    def test_old_delivery_request_does_not_rewind_receive_range(self):
        c = self.case
        calls = []
        bridge = self.bridge(lambda **kwargs: calls.append(kwargs) or {'messages': [], 'has_more': False})
        receive_boundary = f'{baseline.T//1000 - 60}.000000'
        bridge.backfill_cursor = receive_boundary
        request_path = bridge.state_dir / 'slack-delivery-history-request.json'
        listener.write_json(request_path, {'request_id': 'orphan', 'oldest': '1700000000.000000'})
        original = request_path.read_bytes()
        self.assertTrue(bridge.backfill().ok)
        self.assertEqual(calls[0]['oldest'], receive_boundary)
        self.assertNotIn('include_all_metadata', calls[0])
        self.assertNotIn('cursor', calls[0])
        worker = worker_module.RecoveryWorker(bridge.cfg, self.run_ctl, lambda: c.clock)
        worker.cycle()
        self.assertEqual(request_path.read_bytes(), original)

    def test_legacy_delivery_cursor_is_abandoned_but_received_messages_are_preserved(self):
        c = self.case
        calls = []
        bridge = self.bridge(lambda **kwargs: calls.append(kwargs) or {'messages': [], 'has_more': False})
        boundary = f'{baseline.T//1000 - 60}.000000'
        bridge.backfill_cursor = boundary
        incoming = self.event(user='UPEER')
        listener.write_json(bridge.legacy_delivery_scan_path, {'channel': 'D1', 'oldest': '1700000000.000000',
            'inclusive': False, 'cursor': 'old-broken-delivery-cursor', 'messages': [incoming],
            'highwater': incoming['ts']})
        self.assertTrue(bridge.backfill().ok)
        self.assertEqual(calls[0]['oldest'], boundary)
        self.assertNotIn('cursor', calls[0])
        self.assertEqual(bridge.backfill_cursor, boundary)  # Partial old data cannot advance the receive boundary.
        self.assertFalse(bridge.legacy_delivery_scan_path.exists())
        self.assertEqual(c.snapshot()['messages'][0]['id'], 'slack:D1:' + incoming['ts'])

    def test_legacy_buffer_survives_ingest_failure_without_starting_any_scan(self):
        c = self.case
        calls = []
        bridge = self.bridge(lambda **kwargs: calls.append(kwargs) or {'messages': [], 'has_more': False})
        incoming = self.event(user='UPEER')
        listener.write_json(bridge.legacy_delivery_scan_path, {'channel': 'D1', 'messages': [incoming],
            'oldest': '1700000000.000000', 'cursor': 'abandoned'})
        run = bridge.run_ctl
        bridge.run_ctl = lambda *args: ((1, {'ok': False, 'reason': 'ingest_failure'})
                                      if args[0] == 'ingest' else run(*args))
        with self.assertLogs(listener.__name__, level='WARNING'):
            self.assertFalse(bridge.backfill().ok)
        self.assertTrue(bridge.legacy_delivery_scan_path.exists())
        self.assertEqual(calls, [])
        self.assertIn('ingest_failed', bridge.health['alerts'])

    def test_receive_pagination_checkpoint_contains_no_message_bodies(self):
        c = self.case
        page = {'messages': [self.event(user='UPEER', text='private_incoming_marker')],
                'has_more': True, 'response_metadata': {'next_cursor': 'receive-page2'}}
        bridge = self.bridge(lambda **kwargs: page)
        result = bridge.backfill()
        self.assertTrue(result.deferred)
        checkpoint = json.loads(bridge.backfill_scan_path.read_text())
        self.assertEqual(checkpoint['cursor'], 'receive-page2')
        self.assertNotIn('messages', checkpoint)
        self.assertNotIn('private_incoming_marker', bridge.backfill_scan_path.read_text())
        self.assertEqual(c.snapshot()['messages'][0]['text'], 'private_incoming_marker')
