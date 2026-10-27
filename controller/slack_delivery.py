"""Positive-evidence Slack delivery recovery. No networking or message generation."""
import hashlib
import math
import re
import uuid

from common import decode, encode, file_lock, integer, require, text


SQL = (
    '''CREATE TABLE IF NOT EXISTS slack_delivery_settings (
        session_id TEXT PRIMARY KEY REFERENCES sessions(id), settings_json TEXT NOT NULL)''',
    '''CREATE TABLE IF NOT EXISTS slack_delivery_attempts (
        session_id TEXT NOT NULL, job_id TEXT NOT NULL, attempt_token TEXT NOT NULL,
        nonce TEXT NOT NULL UNIQUE, binding TEXT NOT NULL, wire_digest TEXT NOT NULL,
        started_ms INTEGER NOT NULL, unresolved_ms INTEGER, PRIMARY KEY(session_id,job_id,attempt_token),
        FOREIGN KEY(session_id,job_id) REFERENCES dm_jobs(session_id,id))''',
    '''CREATE TABLE IF NOT EXISTS slack_delivery_receipts (
        session_id TEXT NOT NULL, nonce TEXT NOT NULL REFERENCES slack_delivery_attempts(nonce),
        workspace_id TEXT NOT NULL, channel_id TEXT NOT NULL, message_ts TEXT NOT NULL,
        source TEXT NOT NULL CHECK(source IN ('socket','history')), observed_ms INTEGER NOT NULL,
        evidence_json TEXT NOT NULL, PRIMARY KEY(session_id,nonce,message_ts,source))''',
    '''CREATE TABLE IF NOT EXISTS slack_delivery_claims (
        workspace_id TEXT NOT NULL, channel_id TEXT NOT NULL, message_ts TEXT NOT NULL,
        session_id TEXT NOT NULL, job_id TEXT NOT NULL, attempt_token TEXT NOT NULL,
        PRIMARY KEY(workspace_id,channel_id,message_ts),
        FOREIGN KEY(session_id,job_id) REFERENCES dm_jobs(session_id,id))''',
    '''CREATE TABLE IF NOT EXISTS slack_history_budget (
        workspace_id TEXT NOT NULL, app_id TEXT NOT NULL, next_allowed_ms INTEGER NOT NULL,
        PRIMARY KEY(workspace_id,app_id))''',
    '''CREATE TABLE IF NOT EXISTS slack_history_sync (
        session_id TEXT PRIMARY KEY REFERENCES sessions(id), blocked INTEGER NOT NULL CHECK(blocked IN (0,1)),
        updated_ms INTEGER NOT NULL)''',
)
IDENTITY = ('workspace_id', 'channel_id', 'self_user_id', 'app_id', 'event_type')
FLAGS = ('correlation_enabled', 'auto_confirm_enabled', 'metadata_verified', 'history_verified', 'events_verified')
DEFAULTS = {'correlation_enabled': False, 'auto_confirm_enabled': False,
            'metadata_verified': False, 'history_verified': False, 'events_verified': False,
            'mode': 'history', 'event_type': 'persona.delivery', 'history_min_interval_ms': 15000,
            'history_page_size': 15}


def wire_digest(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def escaped(value):
    return value.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def settings(db, session):
    row = db.execute('SELECT settings_json FROM slack_delivery_settings WHERE session_id=?', (session,)).fetchone()
    return decode(row[0]) if row else dict(DEFAULTS)


def binding(cfg):
    return wire_digest(encode({key: cfg[key] for key in IDENTITY}))


def configure(store, session, values, now):
    require(isinstance(values, dict) and set(values) <= set(DEFAULTS) | set(IDENTITY), 'invalid_delivery_settings')
    cfg = {**DEFAULTS, **values}
    for key in FLAGS:
        require(type(cfg[key]) is bool, 'invalid_delivery_flag')
    require(cfg['mode'] in ('history', 'event'), 'invalid_delivery_mode')
    interval = integer(cfg['history_min_interval_ms'], 'history_min_interval_ms', 1000)
    require(interval <= 600000, 'invalid_history_min_interval_ms')
    require(integer(cfg['history_page_size'], 'history_page_size', 1) <= 100, 'invalid_history_page_size')
    if cfg['correlation_enabled']:
        for key in IDENTITY:
            text(cfg.get(key), 'delivery_'+key)
    if cfg['auto_confirm_enabled']:
        require(cfg['correlation_enabled'] and cfg['metadata_verified'] and cfg['history_verified'],
                'delivery_capabilities_unverified')
        require(cfg['mode'] != 'event' or cfg['events_verified'], 'delivery_events_unverified')
    with file_lock(store.path, 'send:'+session), store.connect(True, operation='slack_delivery_configure', session=session) as db:
        require(db.execute('SELECT 1 FROM sessions WHERE id=?', (session,)).fetchone() is not None, 'session_not_found')
        db.execute('INSERT INTO slack_delivery_settings VALUES (?,?) ON CONFLICT(session_id) '
                   'DO UPDATE SET settings_json=excluded.settings_json', (session, encode(cfg)))
        db.execute('UPDATE sessions SET revision=revision+1 WHERE id=?', (session,))
    return {'ok': True, 'reason': 'delivery_configured', 'settings': cfg}


def prepare_attempt(db, session, job, attempt_token, now, channel):
    cfg = settings(db, session)
    if channel != 'slack' or not cfg['correlation_enabled']:
        return None
    nonce = uuid.uuid4().hex
    db.execute('INSERT INTO slack_delivery_attempts '
               '(session_id,job_id,attempt_token,nonce,binding,wire_digest,started_ms) VALUES (?,?,?,?,?,?,?)',
               (session, job['id'], attempt_token, nonce, binding(cfg), wire_digest(escaped(job['text'])), now))
    return {'delivery_id': nonce, 'event_type': cfg['event_type'], 'channel_id': cfg['channel_id']}


def observe(store, session, observation, now):
    """Trusted local ingress, fed only by authenticated Socket or auth.test-bound history."""
    require(isinstance(observation, dict), 'invalid_delivery_observation')
    source = observation.get('source')
    require(source in ('socket', 'history'), 'invalid_delivery_source')
    msg = observation.get('message')
    require(isinstance(msg, dict), 'invalid_delivery_message')
    with store.connect(True) as db:
        cfg = settings(db, session)
        if not cfg['correlation_enabled']:
            return {'ok': True, 'reason': 'delivery_disabled'}
        if observation.get('workspace_id') != cfg['workspace_id'] or msg.get('channel') != cfg['channel_id']:
            return {'ok': True, 'reason': 'delivery_wrong_destination'}
        if msg.get('user') != cfg['self_user_id'] or msg.get('app_id') != cfg['app_id']:
            return {'ok': True, 'reason': 'delivery_wrong_sender'}
        if msg.get('type', 'message') != 'message' or msg.get('subtype') in ('message_changed', 'message_deleted', 'tombstone') or msg.get('hidden'):
            return {'ok': True, 'reason': 'delivery_unsupported_message'}
        if msg.get('thread_ts') and msg['thread_ts'] != msg.get('ts'):
            return {'ok': True, 'reason': 'delivery_thread_reply'}
        meta = msg.get('metadata')
        payload = meta.get('event_payload') if isinstance(meta, dict) else None
        nonce = payload.get('delivery_id') if isinstance(payload, dict) else None
        if not isinstance(meta, dict) or meta.get('event_type') != cfg['event_type'] or not isinstance(nonce, str):
            return {'ok': True, 'reason': 'delivery_no_correlation'}
        attempt = db.execute('SELECT * FROM slack_delivery_attempts WHERE session_id=? AND nonce=?', (session, nonce)).fetchone()
        if not attempt or attempt['binding'] != binding(cfg):
            return {'ok': True, 'reason': 'delivery_unknown_attempt'}
        ts = msg.get('ts')
        if not isinstance(ts, str) or not re.fullmatch(r'[0-9]{10,}\.[0-9]{6}', ts):
            return {'ok': True, 'reason': 'delivery_invalid_timestamp'}
        seconds, fraction = ts.split('.')
        posted_ms = int(seconds)*1000 + int(fraction)//1000
        if posted_ms < attempt['started_ms']-5000 or posted_ms > now+5000:
            return {'ok': True, 'reason': 'delivery_timestamp_out_of_window'}
        body = msg.get('text')
        if not isinstance(body, str) or wire_digest(body) != attempt['wire_digest']:
            return {'ok': True, 'reason': 'delivery_body_mismatch'}
        evidence = {'workspace_id': cfg['workspace_id'], 'channel_id': cfg['channel_id'],
                    'message_ts': ts, 'user_id': msg['user'], 'app_id': msg['app_id'],
                    'delivery_id': nonce, 'wire_digest': attempt['wire_digest'], 'source': source}
        db.execute('INSERT INTO slack_delivery_receipts VALUES (?,?,?,?,?,?,?,?) '
                   'ON CONFLICT(session_id,nonce,message_ts,source) DO NOTHING',
                   (session, nonce, cfg['workspace_id'], cfg['channel_id'], ts, source, now, encode(evidence)))
        count = db.execute('SELECT COUNT(DISTINCT message_ts) FROM slack_delivery_receipts WHERE nonce=?', (nonce,)).fetchone()[0]
        return {'ok': True, 'reason': 'delivery_receipt_conflict' if count > 1 else 'delivery_receipt_recorded'}


def pending(store, session, now):
    with store.connect() as db:
        cfg = settings(db, session)
        rows = db.execute('''SELECT j.id,j.status,j.attempt_started_ms,j.updated_ms,
            EXISTS(SELECT 1 FROM slack_delivery_receipts r WHERE r.nonce=a.nonce) AS has_receipt
            FROM dm_jobs j JOIN slack_delivery_attempts a
            ON a.session_id=j.session_id AND a.job_id=j.id AND a.attempt_token=j.attempt_token
            WHERE j.session_id=? AND j.status IN ('submitted','unknown') ORDER BY j.created_ms,j.part_index''', (session,)).fetchall()
        budget = db.execute('SELECT next_allowed_ms FROM slack_history_budget WHERE workspace_id=? AND app_id=?',
                            (cfg.get('workspace_id'), cfg.get('app_id'))).fetchone()
        total = db.execute("SELECT COUNT(*) FROM dm_jobs j WHERE session_id=? AND status IN ('submitted','unknown') "
            "AND COALESCE((SELECT channel FROM group_channels WHERE session_id=j.session_id AND group_id=j.group_id),'instagram')='slack'",
            (session,)).fetchone()[0]
        dispatch_pending = db.execute('''SELECT 1 FROM dm_jobs j
            JOIN group_channels gc ON gc.session_id=j.session_id AND gc.group_id=j.group_id AND gc.channel='slack'
            WHERE j.session_id=? AND j.status='pending' AND EXISTS (
                SELECT 1 FROM dm_jobs prior JOIN slack_delivery_claims c
                ON c.session_id=prior.session_id AND c.job_id=prior.id
                WHERE prior.session_id=j.session_id AND prior.group_id=j.group_id
                AND prior.part_index<j.part_index AND prior.status='confirmed') LIMIT 1''', (session,)).fetchone()
        return {'ok': True, 'reason': 'delivery_pending', 'settings': cfg,
                'jobs': [dict(r) for r in rows], 'manual_unresolved_count': total-len(rows),
                'dispatch_pending': dispatch_pending is not None,
                'history_next_allowed_ms': budget[0] if budget else now}


def history_budget(store, session, now, retry_after_ms=None):
    with store.connect(True) as db:
        cfg = settings(db, session)
        if not cfg['correlation_enabled']:
            return {'ok': True, 'reason': 'delivery_disabled', 'allowed': True}
        row = db.execute('SELECT next_allowed_ms FROM slack_history_budget WHERE workspace_id=? AND app_id=?',
                         (cfg['workspace_id'], cfg['app_id'])).fetchone()
        due = row[0] if row else now
        if retry_after_ms is not None:
            integer(retry_after_ms, 'retry_after_ms', 1)
            due = max(due, now+retry_after_ms)
        elif due > now:
            return {'ok': True, 'reason': 'history_budget_wait', 'allowed': False, 'retry_after_ms': due-now}
        else:
            due = now+cfg['history_min_interval_ms']
        db.execute('INSERT INTO slack_history_budget VALUES (?,?,?) ON CONFLICT(workspace_id,app_id) '
                   'DO UPDATE SET next_allowed_ms=excluded.next_allowed_ms', (cfg['workspace_id'], cfg['app_id'], due))
        return {'ok': True, 'reason': 'history_budget_reserved', 'allowed': retry_after_ms is None,
                'retry_after_ms': due-now}


def inspect_attempt(store, session, job_id):
    with store.connect() as db:
        cfg = settings(db, session)
        require(cfg['correlation_enabled'], 'delivery_correlation_disabled')
        job = db.execute('SELECT * FROM dm_jobs WHERE session_id=? AND id=?', (session, job_id)).fetchone()
        require(job is not None, 'job_not_found')
        attempt = db.execute('SELECT * FROM slack_delivery_attempts WHERE session_id=? AND job_id=? AND attempt_token=?',
                             (session, job_id, job['attempt_token'])).fetchone()
        require(attempt is not None and attempt['binding'] == binding(cfg), 'delivery_attempt_mismatch')
        return {'ok': True, 'reason': 'delivery_attempt', 'settings': cfg, 'job_id': job_id,
                'delivery_id': attempt['nonce'], 'wire_digest': attempt['wire_digest'],
                'message_ts': job['provider_message_id'], 'status': job['status']}


def history_sync(store, session, blocked, now):
    """Legacy scan-status API; this value no longer authorizes or blocks dispatch."""
    require(type(blocked) is bool, 'invalid_history_sync_state')
    with store.connect(True) as db:
        db.execute('INSERT INTO slack_history_sync VALUES (?,?,?) ON CONFLICT(session_id) '
                   'DO UPDATE SET blocked=excluded.blocked,updated_ms=excluded.updated_ms', (session, int(blocked), now))
    return {'ok': True, 'reason': 'history_sync_blocked' if blocked else 'history_sync_complete'}


def history_is_blocked(db, session):
    """Read legacy status for compatibility. Never use it as a send gate."""
    if not settings(db, session)['correlation_enabled']:
        return False
    row = db.execute('SELECT blocked FROM slack_history_sync WHERE session_id=?', (session,)).fetchone()
    return bool(row and row[0])


def mark_unresolved(db, session, job, now):
    db.execute('UPDATE slack_delivery_attempts SET unresolved_ms=COALESCE(unresolved_ms,?) '
               'WHERE session_id=? AND job_id=? AND attempt_token=?', (now, session, job['id'], job['attempt_token']))


def report(store, session, now):
    with store.connect() as db:
        cfg = settings(db, session)
        rows = db.execute('''SELECT a.unresolved_ms,j.status,j.updated_ms,j.result_json,
            (SELECT min(observed_ms) FROM slack_delivery_receipts r WHERE r.nonce=a.nonce) AS evidence_ms
            FROM slack_delivery_attempts a JOIN dm_jobs j
            ON j.session_id=a.session_id AND j.id=a.job_id AND j.attempt_token=a.attempt_token
            WHERE a.session_id=? AND a.unresolved_ms IS NOT NULL''', (session,)).fetchall()
        automatic, manual, unresolved = [], [], []
        early = 0
        for row in rows:
            if row['status'] in ('submitted', 'unknown'):
                unresolved.append(max(0, now-row['unresolved_ms']))
            elif row['status'] == 'confirmed':
                latency = max(0, row['updated_ms']-row['unresolved_ms'])
                evidence = decode(row['result_json']).get('evidence', '')
                try:
                    automatic_proof = isinstance(evidence, str) and decode(evidence).get('kind') == 'slack_delivery_receipt'
                except (ValueError, AttributeError):
                    automatic_proof = False
                (automatic if automatic_proof else manual).append(latency)
                early += row['evidence_ms'] is not None and row['evidence_ms'] < row['unresolved_ms']
        def p95(values):
            return sorted(values)[math.ceil(len(values)*0.95)-1] if values else None
        return {'ok': True, 'reason': 'delivery_report', 'automatic_confirmed': len(automatic),
                'manual_confirmed': len(manual), 'correlated_unresolved': len(unresolved),
                'oldest_correlated_unresolved_ms': max(unresolved) if unresolved else None,
                'automatic_unknown_to_confirm_p95_ms': p95(automatic),
                'manual_unknown_to_confirm_p95_ms': p95(manual), 'early_evidence_count': early,
                'mode': cfg['mode'], 'events_verified': cfg['events_verified'],
                'scope': 'all retained correlated current attempts; legacy and images are in delivery-pending manual count'}


def verified_result(db, session, job, now):
    cfg = settings(db, session)
    require(cfg['auto_confirm_enabled'] and cfg['metadata_verified'] and cfg['history_verified'], 'delivery_recovery_disabled')
    require(cfg['mode'] != 'event' or cfg['events_verified'], 'delivery_events_unverified')
    require(job['status'] in ('unknown', 'submitted'), 'job_not_reconcilable')
    attempt = db.execute('SELECT * FROM slack_delivery_attempts WHERE session_id=? AND job_id=? AND attempt_token=?',
                         (session, job['id'], job['attempt_token'])).fetchone()
    require(attempt is not None and attempt['binding'] == binding(cfg), 'delivery_attempt_mismatch')
    require(wire_digest(escaped(job['text'])) == attempt['wire_digest'], 'delivery_body_changed')
    receipts = db.execute('SELECT * FROM slack_delivery_receipts WHERE session_id=? AND nonce=?', (session, attempt['nonce'])).fetchall()
    require(receipts, 'delivery_evidence_missing')
    require(len({r['message_ts'] for r in receipts}) == 1, 'delivery_receipt_conflict')
    receipt = receipts[0]
    # Revalidate even persisted evidence against the current binding; no caller-supplied result.
    proof = decode(receipt['evidence_json'])
    require(proof['wire_digest'] == attempt['wire_digest'] and proof['delivery_id'] == attempt['nonce']
            and proof['workspace_id'] == cfg['workspace_id'] and proof['channel_id'] == cfg['channel_id']
            and proof['user_id'] == cfg['self_user_id'] and proof['app_id'] == cfg['app_id'], 'delivery_evidence_mismatch')
    reserve_claim(db, session, job, receipt['message_ts'], cfg)
    return {'status': 'confirmed', 'provider_message_id': receipt['message_ts'],
            'evidence': encode({'kind': 'slack_delivery_receipt', **proof})}


def reserve_claim(db, session, job, message_ts, cfg=None):
    cfg = cfg or settings(db, session)
    if not cfg['correlation_enabled']:
        return
    attempt = db.execute('SELECT 1 FROM slack_delivery_attempts WHERE session_id=? AND job_id=? AND attempt_token=?',
                         (session, job['id'], job['attempt_token'])).fetchone()
    if not attempt:
        return
    previous = db.execute('SELECT session_id,job_id,attempt_token FROM slack_delivery_claims '
                          'WHERE workspace_id=? AND channel_id=? AND message_ts=?',
                          (cfg['workspace_id'], cfg['channel_id'], message_ts)).fetchone()
    identity = (session, job['id'], job['attempt_token'])
    require(previous is None or tuple(previous) == identity, 'delivery_receipt_already_used')
    db.execute('INSERT OR IGNORE INTO slack_delivery_claims VALUES (?,?,?,?,?,?)',
               (cfg['workspace_id'], cfg['channel_id'], message_ts, *identity))
