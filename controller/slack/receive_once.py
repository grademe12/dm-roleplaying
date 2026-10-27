"""VM receive-only sync for doorbell wakeups, using the existing backfill cursor.

No decisions, gate claims or sends. Only completed backfill plus actual DB rows
can certify a captured hint batch. Normal receive history may also supply exact
delivery receipts; no delivery-specific rewind or history scan is introduced.
"""
from copy import deepcopy
from pathlib import Path

from common import require
from doorbell import binding, timestamp
from slack.slack_listener import SlackBridge, make_ctl, read_secret, ts_key
from store import Store


def synchronize(cfg, offer=None, *, web=None, run_ctl=None, clock=None):
    target = cfg['wake']['binding']
    binding(target)
    require(target['channel'] == cfg['slack']['channel'], 'receive_binding_mismatch')
    if offer:
        require(offer['binding'] == target, 'receive_binding_mismatch')
    times = [timestamp(h['message_ts']) for h in (offer or {}).get('hints', []) if h['message_ts']]
    def incoming_count():
        with Store(cfg['db']).connect() as db:
            return db.execute("SELECT count(*) FROM dm_messages WHERE session_id=? "
                              "AND json_extract(observation_json,'$.channel')='slack' "
                              "AND json_extract(observation_json,'$.direction')='incoming'",
                              (cfg['session'],)).fetchone()[0]
    before = incoming_count()
    if web is None:
        from slack_sdk import WebClient
        web = WebClient(token=read_secret(cfg['slack']['bot_token_path']),timeout=10,retry_handlers=[])
    auth = web.auth_test()
    require(auth.get('ok') is True and auth.get('team_id') == target['workspace'], 'receive_identity_mismatch')
    local = deepcopy(cfg)
    # Optional migration-only path. The old listener defaults remain untouched.
    local['slack']['backfill_receipts_enabled'] = True
    bridge = SlackBridge(local,lambda **kw: web.conversations_history(**kw),run_ctl or make_ctl(cfg),
                         **({'clock': clock} if clock else {}))
    try:
        if bridge.delivery_enabled:
            require(bridge.set_delivery_identity(auth), 'receive_delivery_identity_mismatch')
        if times:
            boundary = timestamp(cfg['slack']['ingest_after_ts'])
            require(all(ts_key(ts)>ts_key(boundary) for ts in times), 'receive_before_boundary')
            first = min(times,key=ts_key)
            # A hint can lag the cursor, or a restored DB can lack a previously
            # seen row. Re-read the captured range inclusively and idempotently.
            bridge.seen = [ts for ts in bridge.seen if ts not in times]
            if ts_key(first) <= ts_key(bridge.backfill_cursor):
                bridge.backfill_cursor = first
                bridge.retry_ts = first
        result = bridge.backfill(pages=cfg['wake'].get('backfill_pages',10))
        if not result.ok:
            bridge.heartbeat(False)
            return {'ok': False, 'complete': False, 'reason': 'receive_sync_incomplete',
                    'retry_after_ms': max(15000,result.retry_after_seconds*1000)}
        with Store(cfg['db']).connect() as db:
            for ts in times:
                row = db.execute('SELECT observation_json FROM dm_messages WHERE session_id=? AND id=?',
                                 (cfg['session'],f"slack:{target['channel']}:{ts}")).fetchone()
                require(row is not None, 'receive_hint_not_ingested')
        bridge.health['receive_mode'] = 'doorbell_backfill'
        bridge.heartbeat(True)
        return {'ok': True, 'complete': True, 'binding': target,
                'epoch': (offer or {}).get('epoch'), 'through_seq': (offer or {}).get('through_seq'),
                'new_incoming_count': max(0,incoming_count()-before)}
    except Exception:
        bridge.heartbeat(False)
        raise
