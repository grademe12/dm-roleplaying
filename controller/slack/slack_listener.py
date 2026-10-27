#!/usr/bin/env python3
"""Slack Socket Mode listener for the Yujin controller (long-running systemd service).

Receives DM events in real time, ingests the user's messages into the shared session
(observation.channel = "slack"), keeps automation-state/recent_thread_slack.json and
automation-state/slack_health.json. Sending is done by the controller's send-due through
slack_send.py, never here. No model calls.

Missed events (listener down, reconnects) are recovered with conversations.history from the
last ingested ts at start-up and every backfill_seconds.

Requires slack_sdk (pinned in requirements-slack.txt). Tokens are read from files and never
printed or logged.
"""
import argparse
import html
import json
import logging
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

KST = timezone(timedelta(hours=9))
SKIP_SUBTYPES = {'message_changed', 'message_deleted', 'channel_join', 'channel_leave', 'channel_topic',
                 'channel_purpose', 'channel_name', 'pinned_item', 'unpinned_item', 'ekm_access_denied',
                 'thread_broadcast', 'group_join', 'group_leave', 'tombstone'}
BACKFILL_RETRY_SECONDS = (15, 30, 60, 120, 300)
MESSAGE_OUTCOME_HISTORY = 100
# Internal workspace app profile; externally distributed apps may need 60 seconds.
DEFAULT_BACKFILL_SECONDS = 15
NUDGE_TIMEOUT_SECONDS = 5


@dataclass(frozen=True)
class BackfillResult:
    ok: bool
    handled: int
    retry_after_seconds: int = 0
    deferred: bool = False


class HistoryBudgetWait(RuntimeError):
    def __init__(self, seconds):
        self.seconds = max(1, seconds)


class BackfillError(RuntimeError):
    """A local backfill failure code safe to include in health details."""


def backfill_delay_seconds(result, failures, normal_seconds):
    if result.deferred:
        return max(1, result.retry_after_seconds)
    if result.ok:
        return normal_seconds
    retry = BACKFILL_RETRY_SECONDS[min(max(failures, 1) - 1, len(BACKFILL_RETRY_SECONDS) - 1)]
    return max(retry, result.retry_after_seconds)


def retry_after_seconds(exc):
    response = getattr(exc, 'response', None)
    headers = getattr(response, 'headers', None) or {}
    value = next((v for k, v in headers.items() if k.lower() == 'retry-after'), 0)
    try:
        return max(0, int(value))
    except (ValueError, TypeError):
        return 0


def now_ms():
    return time.time_ns() // 1_000_000


def kst_iso(ms):
    return datetime.fromtimestamp(ms / 1000, KST).isoformat(timespec='seconds')


def ts_key(ts):
    """Slack ts '1792040000.123456' as a sortable integer pair."""
    sec, _, frac = str(ts).partition('.')
    return int(sec), int((frac + '000000')[:6])


def load_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return default


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.tmp')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as handle:
        json.dump(data, handle, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def read_secret(path):
    return Path(path).expanduser().read_text(encoding='utf-8').strip()


class SlackBridge:
    """All decisions about Slack messages. Kept free of slack_sdk so it can be tested offline."""

    def __init__(self, cfg, history, run_ctl, clock=now_ms):
        self.cfg, self.s = cfg, cfg['slack']
        self.history, self.run_ctl, self.clock = history, run_ctl, clock
        self.state_dir = Path(cfg['state_dir'])
        self.channel = self.s['channel']
        self.lock = threading.Lock()
        self.seen_path = self.state_dir / 'slack_seen.json'
        self.recent_path = self.state_dir / 'recent_thread_slack.json'
        self.health_path = self.state_dir / 'slack_health.json'
        seen = load_json(self.seen_path, {})
        self.cursor = seen.get('cursor') or self.s['ingest_after_ts']
        # A live Socket Mode event may advance cursor over an unseen gap. Only a completed
        # history scan may advance this separate recovery watermark. Older state has no
        # recovery watermark, so replay from the configured ingestion boundary once.
        self.backfill_cursor = seen.get('backfill_cursor') or self.s['ingest_after_ts']
        self.seen = list(seen.get('seen', []))
        # Oldest incoming ts whose ingest failed; backfill restarts there (inclusive) until it succeeds.
        self.retry_ts = seen.get('retry_ts')
        self.recent = load_json(self.recent_path, {}).get('messages', [])
        self.health = load_json(self.health_path, {})
        self.health['connected'] = False
        self.health.setdefault('backfill_consecutive_failures', 0)
        self.delivery_enabled = self.s.get('delivery_receipts_enabled', False) is True
        self.delivery_workspace = None
        self.delivery_settings = {}
        self.backfill_scan_path = self.state_dir / 'slack-backfill-scan.json'
        self.legacy_delivery_scan_path = self.state_dir / 'slack-delivery-history-scan.json'
        # Optional webhook nudge after a new inbound DM is ingested. Off unless nudge.enabled is true.
        nudge = cfg.get('nudge')
        self.nudge = nudge if isinstance(nudge, dict) and nudge.get('enabled') is True and nudge.get('url_path') else None
        self.nudge_lock = threading.Lock()
        self.last_nudge = None

    def set_delivery_identity(self, auth):
        if not self.delivery_enabled:
            return False
        code, out = self.run_ctl('slack-delivery-pending', '--session', self.cfg['session'])
        cfg = out.get('settings', {})
        if code or not out.get('ok') or not cfg.get('correlation_enabled'):
            self.delivery_settings = {}
            return False
        self.delivery_settings = cfg
        verified = (auth.get('ok') is True and auth.get('team_id') == cfg.get('workspace_id')
                    and auth.get('user_id') == cfg.get('self_user_id') and self.channel == cfg.get('channel_id'))
        self.delivery_workspace = cfg['workspace_id'] if verified else None
        with self.lock:
            self._alert('delivery_identity_mismatch', not verified, 'auth.test binding mismatch' if not verified else '')
            self.health['delivery_identity_verified'] = verified
            self._save()
        return verified

    def record_socket_envelope(self, req_type, payload):
        event = payload.get('event') or {}
        with self.lock:
            self.health['socket_envelopes'] = int(self.health.get('socket_envelopes', 0)) + 1
            self.health['last_socket_envelope_ms'] = self.clock()
            counts = self.health.setdefault('socket_request_types', {})
            counts[str(req_type)[:64]] = counts.get(str(req_type)[:64], 0) + 1
            if event.get('type'):
                counts = self.health.setdefault('socket_event_types', {})
                kind = str(event['type'])[:64]
                counts[kind] = counts.get(kind, 0) + 1
            self._save()

    def _observe_delivery(self, msg, source, workspace_id):
        if not self.delivery_enabled or not workspace_id:
            return
        # Only correlated messages need a controller round trip; user messages remain ordinary ingress.
        if not isinstance(msg.get('metadata'), dict):
            return
        observation = {'source': 'history' if source == 'backfill' else 'socket',
                       'workspace_id': workspace_id, 'message': msg}
        self.state_dir.mkdir(parents=True, exist_ok=True)
        # Do not expose private message text in the controller process command line.
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', prefix='delivery-observe-', suffix='.json',
                                         dir=self.state_dir, delete=False) as handle:
            json.dump(observation, handle, ensure_ascii=False)
            observation_path = Path(handle.name)
        try:
            code, out = self.run_ctl('slack-delivery-observe', '--session', self.cfg['session'],
                                    '--observation', '@'+str(observation_path))
        finally:
            observation_path.unlink(missing_ok=True)
        with self.lock:
            self.health['last_delivery_observation'] = {'at_ms': self.clock(), 'result': out.get('reason')}
            self._alert('delivery_observe_failed', code != 0 or not out.get('ok'), 'receipt controller failure')
            if out.get('reason') == 'delivery_receipt_conflict':
                self._alert('delivery_receipt_conflict', True, 'multiple posts for one delivery')
            self._save()

    def _read_history(self, **kwargs):
        enabled = self.delivery_workspace and self.delivery_settings.get('metadata_verified')
        if enabled:
            code, out = self.run_ctl('slack-history-claim', '--session', self.cfg['session'])
            if code or not out.get('ok'):
                raise BackfillError('delivery_history_budget_failed')
            if not out.get('allowed'):
                raise HistoryBudgetWait((out.get('retry_after_ms', 60000)+999)//1000)
            kwargs['limit'] = self.delivery_settings.get('history_page_size', 15)
        try:
            data = self.history(**kwargs)
        except Exception as exc:
            retry = retry_after_seconds(exc)
            if enabled and retry:
                self.run_ctl('slack-history-backoff', '--session', self.cfg['session'],
                             '--retry-after-ms', str(retry*1000))
            raise
        return data

    # --- classification -------------------------------------------------------------
    def _classify(self, msg):
        if msg.get('type', 'message') != 'message' or msg.get('channel', self.channel) != self.channel:
            return None, 'wrong_type_or_channel'
        if msg.get('subtype') in SKIP_SUBTYPES or msg.get('hidden'):
            return None, 'subtype_or_hidden'
        ts = msg.get('ts')
        if not ts:
            return None, 'missing_ts'
        if msg.get('thread_ts') and msg['thread_ts'] != ts:
            return None, 'thread_reply'   # thread replies are not part of the DM conversation
        user = msg.get('user')
        if msg.get('bot_id') or (user and user == self.s.get('self_user_id')):
            direction = 'outgoing'
        elif user == self.s['peer_user_id']:
            direction = 'incoming'
        else:
            self._alert('unknown_sender', True, 'message from an unexpected user')
            return None, 'unknown_sender'
        text = html.unescape(msg.get('text') or '').strip()
        files = msg.get('files') or []
        images = sum(1 for f in files if str(f.get('mimetype', '')).startswith('image/'))
        others = len(files) - images
        extra = ' '.join(x for x in (f'[사진 {images}장]' if images else '', f'[파일 {others}개]' if others else '') if x)
        text = (text + ' ' + extra).strip() if extra else text
        ts_ms = int(float(ts) * 1000)
        return {'id': f'slack:{self.channel}:{ts}', 'ts': ts, 'ts_ms': ts_ms, 'direction': direction,
                'text': text or '[메시지]', 'images': images}, None

    def classify(self, msg):
        return self._classify(msg)[0]

    def _event_outcome(self, msg, source, outcome, reason='', received_at_ms=None):
        # IDs and routing metadata are enough to diagnose a gap; never log message text or tokens.
        at_ms = self.clock()
        logging.getLogger(__name__).info(
            'Slack message source=%s ts=%s channel=%s user=%s subtype=%s thread_ts=%s outcome=%s reason=%s',
            source, msg.get('ts'), msg.get('channel'), msg.get('user'), msg.get('subtype'),
            msg.get('thread_ts'), outcome, reason)
        entry = {'received_at_ms': received_at_ms, 'at_ms': at_ms,
                 'ts': msg.get('ts'), 'source': source,
                 'result': outcome, 'reason': reason}
        history = self.health.setdefault('recent_message_outcomes', [])
        history.append(entry)
        del history[:-MESSAGE_OUTCOME_HISTORY]
        if source == 'socket':
            self.health['last_socket_message_ms'] = at_ms
            self.health['last_socket_outcome'] = {'at_ms': at_ms, 'ts': msg.get('ts'),
                                                  'result': outcome, 'reason': reason}
        if outcome == 'ingested':
            self.health['last_ingest_success_ms'] = at_ms

    # --- processing -----------------------------------------------------------------
    def handle(self, msg, source='socket', workspace_id=None):
        received_at_ms = self.clock()
        if source == 'socket':
            self._observe_delivery(msg, source, workspace_id)
        record, skip_reason = self._classify(msg)
        if record is None:
            with self.lock:
                self._event_outcome(msg, source, 'skipped', skip_reason, received_at_ms)
                self._save()
            return 'skipped'
        with self.lock:
            if record['ts'] in self.seen:
                self._event_outcome(msg, source, 'duplicate', 'seen_ts', received_at_ms)
                self._save()
                return 'duplicate'
            after_boundary = ts_key(record['ts']) > ts_key(self.s['ingest_after_ts'])
            if record['direction'] == 'incoming' and after_boundary:
                if not self._ingest(record):
                    # Not marked seen; the next backfill restarts from here.
                    if self.retry_ts is None or ts_key(record['ts']) < ts_key(self.retry_ts):
                        self.retry_ts = record['ts']
                    self._event_outcome(msg, source, 'ingest_failed',
                                        str(self.health.get('alerts', {}).get('ingest_failed', {}).get('detail', '')),
                                        received_at_ms)
                    self._save()
                    return 'ingest_failed'
            ingested = record['direction'] == 'incoming' and after_boundary
            self._remember(record)
            self.health['last_event_ms'] = self.clock()
            self._event_outcome(msg, source, 'ingested' if ingested else 'recorded',
                                'before_ingest_boundary' if record['direction'] == 'incoming' and not after_boundary else '',
                                received_at_ms)
            self._save()
        if ingested:
            self._nudge(record['ts'])
        return 'ingested' if ingested else 'recorded'

    def _nudge(self, ts):
        """Fire-and-forget webhook after a successful ingest. Never blocks or fails ingest."""
        if not self.nudge:
            return
        with self.nudge_lock:
            now = time.monotonic()
            interval = float(self.nudge.get('min_interval_seconds', 20))
            if self.last_nudge is not None and now - self.last_nudge < interval:
                return
            self.last_nudge = now
        body = json.dumps({'session': self.cfg['session'], 'reason': 'dm_ingested', 'ts': ts}).encode('utf-8')
        threading.Thread(target=self._post_nudge, args=(body,), daemon=True).start()

    def _post_nudge(self, body):
        try:
            url = read_secret(self.nudge['url_path'])
            headers = {'Content-Type': 'application/json'}
            if self.nudge.get('key_path'):
                key = read_secret(self.nudge['key_path'])
                name = self.nudge.get('key_header', 'Authorization')
                headers[name] = f'Bearer {key}' if name.lower() == 'authorization' else key
            req = urllib.request.Request(url, data=body, headers=headers, method='POST')
            with urllib.request.urlopen(req, timeout=NUDGE_TIMEOUT_SECONDS) as resp:
                resp.read(1024)
        except Exception as exc:  # URL and key are secrets: log only the failure type.
            logging.getLogger(__name__).warning('Webhook nudge failed: %s', type(exc).__name__)

    def _ingest(self, record):
        now = self.clock()
        batch = {'read_ok': True, 'messages': [{
            'id': record['id'], 'text': record['text'], 'direction': 'incoming', 'observed_ms': min(record['ts_ms'], now),
            'observation': {'identity': 'platform', 'channel': 'slack', 'thread_id': self.channel, 'source': 'slack_listener',
                            'platform_timestamp_ms': record['ts_ms'], 'slack_ts': record['ts'],
                            'attachment_count': record['images']}}]}
        path = self.state_dir / 'slack-ingest-batch.json'
        write_json(path, batch)
        try:
            code, out = self.run_ctl('ingest', '--session', self.cfg['session'], '--batch', '@' + str(path))
        finally:
            path.unlink(missing_ok=True)
        ok = code == 0 and out.get('ok') is True
        if not ok:
            self._alert('ingest_failed', True, str(out.get('reason', 'unknown')))
        return ok

    def _remember(self, record):
        self.seen.append(record['ts'])
        self.seen = self.seen[-500:]
        if ts_key(record['ts']) > ts_key(self.cursor):
            self.cursor = record['ts']
        if all(m['id'] != record['id'] for m in self.recent):
            self.recent.append({'id': record['id'], 'direction': record['direction'], 'sent_kst': kst_iso(record['ts_ms']),
                                'timestamp_ms': record['ts_ms'], 'text': record['text'], 'channel': 'slack'})
            self.recent.sort(key=lambda m: m['timestamp_ms'])
            self.recent = self.recent[-self.s.get('recent_thread_size', 30):]

    def _alert(self, code, active, detail=''):
        alerts = self.health.setdefault('alerts', {})
        if active:
            alerts[code] = {'since_ms': alerts.get(code, {}).get('since_ms', self.clock()), 'detail': detail}
        else:
            alerts.pop(code, None)

    def _save(self):
        write_json(self.seen_path, {'cursor': self.cursor, 'backfill_cursor': self.backfill_cursor,
                                    'seen': self.seen, 'retry_ts': self.retry_ts})
        write_json(self.recent_path, {'fetched_ms': self.clock(), 'fetched_kst': kst_iso(self.clock()),
                                      'channel': 'slack', 'thread_id': self.channel, 'messages': self.recent})
        write_json(self.health_path, self.health)

    # --- recovery and liveness ------------------------------------------------------
    def _ingest_history_page(self, messages):
        """Expose known incoming messages before another page can fail or wait."""
        for msg in sorted(messages, key=lambda m: ts_key(m['ts'])):
            msg.setdefault('channel', self.channel)
            if self.s.get('backfill_receipts_enabled') is True and self.delivery_workspace:
                self._observe_delivery(msg, source='backfill', workspace_id=self.delivery_workspace)
                if isinstance(msg.get('metadata'), dict) and self.health.get('alerts', {}).get('delivery_observe_failed'):
                    raise BackfillError('backfill_receipt_not_saved')
            status = self.handle(msg, source='backfill')
            if status == 'ingest_failed':
                raise BackfillError('backfill_ingest_failed')
            yield int(status in ('ingested', 'recorded'))

    def backfill(self, pages=1000):
        """Recover incoming messages only, independently of unresolved delivery attempts."""
        handled, page, cursor = 0, 0, None
        retry = self.retry_ts
        oldest = self.backfill_cursor
        if retry and ts_key(retry) < ts_key(oldest):
            oldest = retry
        inclusive = bool(retry and retry == oldest)
        highwater = self.backfill_cursor
        saved = load_json(self.backfill_scan_path, {})
        if saved.get('channel') == self.channel and saved.get('oldest') == oldest and saved.get('inclusive') == inclusive:
            cursor = saved.get('cursor')
            highwater = saved.get('highwater', highwater)
        result = BackfillResult(ok=False, handled=0)
        try:
            # Preserve already fetched incoming messages, but never resume the old
            # delivery scan's cursor or rewind the receive watermark to its oldest.
            legacy = load_json(self.legacy_delivery_scan_path, {})
            if legacy.get('channel') == self.channel:
                for count in self._ingest_history_page(legacy.get('messages', [])):
                    handled += count
                self.legacy_delivery_scan_path.unlink(missing_ok=True)
            for page in range(1, pages + 1):
                kwargs = {'channel': self.channel, 'oldest': oldest, 'limit': 100, 'inclusive': inclusive}
                if cursor:
                    kwargs['cursor'] = cursor
                data = self._read_history(**kwargs)
                for count in self._ingest_history_page(data.get('messages', [])):
                    handled += count
                for msg in data.get('messages', []):
                    if msg.get('ts') and ts_key(msg['ts']) > ts_key(highwater):
                        highwater = msg['ts']
                if not data.get('has_more'):
                    break
                next_cursor = (data.get('response_metadata') or {}).get('next_cursor')
                if not next_cursor or next_cursor == cursor:
                    raise BackfillError('pagination_cursor_missing_or_repeated')
                cursor = next_cursor
                # Receive-only checkpoint: each page is already ingested, so no
                # conversation body or delivery receipt buffer needs to accumulate.
                write_json(self.backfill_scan_path, {'channel': self.channel, 'oldest': oldest,
                    'inclusive': inclusive, 'cursor': cursor, 'highwater': highwater})
            else:
                raise BackfillError('pagination_page_limit_exceeded')

            with self.lock:
                if self.retry_ts and self.retry_ts != retry:
                    raise BackfillError('pending_ingest_retry')
                if ts_key(highwater) > ts_key(self.backfill_cursor):
                    self.backfill_cursor = highwater
                if retry and self.retry_ts == retry:
                    self.retry_ts = None
                self._alert('backfill_failed', False)
                if self.retry_ts is None:
                    self._alert('ingest_failed', False)
                    self._alert('event_processing_failed', False)
                self.health['backfill_consecutive_failures'] = 0
                self.health['last_backfill_success_ms'] = self.clock()
                self.health['last_backfill_ms'] = self.clock()
                self._save()
            self.backfill_scan_path.unlink(missing_ok=True)
            result = BackfillResult(ok=True, handled=handled)
        except HistoryBudgetWait as exc:
            with self.lock:
                self.health['history_budget_wait_seconds'] = exc.seconds
                self._save()
            result = BackfillResult(ok=False, handled=handled, retry_after_seconds=exc.seconds, deferred=True)
        except Exception as exc:  # Keep Socket Mode alive; preserve the recovery watermark.
            code = f':{exc}' if isinstance(exc, BackfillError) else ''
            detail = (f'{type(exc).__name__}{code} page={page} oldest={oldest} '
                      f'inclusive={inclusive} limit=100 has_cursor={bool(cursor)}')
            logging.getLogger(__name__).warning('Slack backfill failed: %s', detail)
            with self.lock:
                self._alert('backfill_failed', True, detail)
                self.health['backfill_consecutive_failures'] = int(self.health.get('backfill_consecutive_failures', 0)) + 1
                self.health['last_backfill_ms'] = self.clock()
                self._save()
            result = BackfillResult(ok=False, handled=handled, retry_after_seconds=retry_after_seconds(exc))
        return result

    def heartbeat(self, connected):
        with self.lock:
            self.health['connected'] = bool(connected)
            self.health['last_heartbeat_ms'] = self.clock()
            write_json(self.health_path, self.health)


def make_ctl(cfg):
    def run(*args):
        argv = [cfg['python'], cfg['ctl'], '--db', cfg['db'], *args]
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, encoding='utf-8', timeout=60)
            return proc.returncode, json.loads(proc.stdout)
        except (subprocess.TimeoutExpired, OSError, ValueError, TypeError):
            return 1, {'ok': False, 'reason': 'ctl_failed'}
    return run


def main(argv=None):
    listener_log = logging.getLogger(__name__)
    listener_log.setLevel(logging.INFO)
    if not listener_log.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
        listener_log.addHandler(handler)
    listener_log.propagate = False
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True)
    args = parser.parse_args(argv)
    cfg = json.loads(Path(args.config).read_text(encoding='utf-8-sig'))
    s = cfg['slack']
    for key in ('channel', 'peer_user_id', 'bot_token_path', 'app_token_path', 'ingest_after_ts'):
        if not s.get(key):
            raise SystemExit(f'slack.{key} is required')

    from slack_sdk import WebClient
    from slack_sdk.socket_mode import SocketModeClient
    from slack_sdk.socket_mode.request import SocketModeRequest
    from slack_sdk.socket_mode.response import SocketModeResponse

    web = WebClient(token=read_secret(s['bot_token_path']))
    client = SocketModeClient(app_token=read_secret(s['app_token_path']), web_client=web)
    bridge = SlackBridge(cfg, history=lambda **kw: web.conversations_history(**kw).data, run_ctl=make_ctl(cfg))

    if bridge.delivery_enabled:
        try:
            bridge.set_delivery_identity(web.auth_test().data)
        except Exception:
            with bridge.lock:
                bridge._alert('delivery_identity_mismatch', True, 'auth.test unavailable')
                bridge._save()

    backfill_now = threading.Event()

    def on_request(sm_client, req: SocketModeRequest):
        # Acknowledge first; Slack retries unacknowledged envelopes.
        sm_client.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))
        payload = req.payload or {}
        try:
            bridge.record_socket_envelope(req.type, payload)
        except Exception:
            listener_log.warning('Socket diagnostic write failed')
        if req.type == 'events_api':
            event = payload.get('event') or {}
            if event.get('type') == 'message':
                try:
                    if bridge.handle(event, workspace_id=payload.get('team_id')) == 'ingest_failed':
                        backfill_now.set()
                except Exception:
                    logging.getLogger(__name__).exception('Slack event processing failed ts=%s channel=%s',
                                                          event.get('ts'), event.get('channel'))
                    with bridge.lock:
                        bridge._alert('event_processing_failed', True, 'message event handler raised')
                        bridge._save()
                    backfill_now.set()

    client.socket_mode_request_listeners.append(on_request)
    stop = threading.Event()
    def request_stop(*_):
        stop.set()
        backfill_now.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    client.connect()
    bridge.heartbeat(client.is_connected())

    def heartbeat_loop():
        while not stop.wait(s.get('heartbeat_seconds', 10)):
            bridge.heartbeat(client.is_connected())

    heartbeat_thread = threading.Thread(target=heartbeat_loop, daemon=True)
    heartbeat_thread.start()

    def run_backfill():
        result = bridge.backfill()
        delay = backfill_delay_seconds(result, bridge.health['backfill_consecutive_failures'],
                                       s.get('backfill_seconds', DEFAULT_BACKFILL_SECONDS))
        with bridge.lock:
            bridge.health['next_backfill_ms'] = bridge.clock() + int(delay * 1000)
            bridge._save()
        return time.monotonic() + delay

    next_backfill = run_backfill()
    while not stop.is_set():
        backfill_now.wait(max(0, next_backfill-time.monotonic()))
        backfill_now.clear()
        if stop.is_set():
            break
        next_backfill = run_backfill()
    heartbeat_thread.join(timeout=2)
    client.close()
    bridge.heartbeat(False)
    return 0


if __name__ == '__main__':
    sys.exit(main())
