"""Opt-in, evidence-linked memories. Muse chooses meaning; SQLite checks provenance.

No extraction model, network calls, automatic historical backfill, or send authority.
Entries are immutable versions; only lifecycle status changes after delivery/revocation.
"""
import re

from common import ControlError, decode, encode, integer, require, text

PREFIX = 'memory:'
MAX_OPERATION_BYTES = 6144
MAX_SNAPSHOT_BYTES = 12288
DEFAULTS = {'mode': 'off', 'scopes': ['private_dm']}
SQL = (
    '''CREATE TABLE IF NOT EXISTS continuity_policy (
       session_id TEXT PRIMARY KEY REFERENCES sessions(id), settings_json TEXT NOT NULL,
       updated_ms INTEGER NOT NULL)''',
    '''CREATE TABLE IF NOT EXISTS continuity_memories (
       session_id TEXT NOT NULL REFERENCES sessions(id), id TEXT NOT NULL,
       kind TEXT NOT NULL CHECK(kind IN ('nickname','preference','utterance')),
       memory_key TEXT NOT NULL, scope TEXT NOT NULL, channel TEXT NOT NULL,
       summary TEXT NOT NULL, scope_note TEXT NOT NULL, evidence_json TEXT NOT NULL,
       job_id TEXT, excerpt TEXT, supersedes TEXT,
       status TEXT NOT NULL CHECK(status IN ('pending','active','superseded','revoked','cancelled')),
       created_ms INTEGER NOT NULL, activated_ms INTEGER, updated_ms INTEGER NOT NULL,
       reason TEXT, PRIMARY KEY(session_id,id),
       FOREIGN KEY(session_id,job_id) REFERENCES dm_jobs(session_id,id),
       FOREIGN KEY(session_id,supersedes) REFERENCES continuity_memories(session_id,id))''',
    '''CREATE UNIQUE INDEX IF NOT EXISTS continuity_one_active ON continuity_memories
       (session_id,kind,memory_key,scope,channel) WHERE status='active' AND kind!='utterance' ''',
    '''CREATE UNIQUE INDEX IF NOT EXISTS continuity_one_pending ON continuity_memories
       (session_id,kind,memory_key,scope,channel) WHERE status='pending' AND kind!='utterance' ''',
    '''CREATE INDEX IF NOT EXISTS continuity_by_job ON continuity_memories(session_id,job_id,status)''',
    '''CREATE TABLE IF NOT EXISTS continuity_diagnostics (
       session_id TEXT NOT NULL, decision_id TEXT NOT NULL, created_ms INTEGER NOT NULL,
       error_code TEXT NOT NULL, operation_count INTEGER,
       PRIMARY KEY(session_id,decision_id),
       FOREIGN KEY(session_id,decision_id) REFERENCES decisions(session_id,id))''',
    '''CREATE INDEX IF NOT EXISTS continuity_diagnostic_time
       ON continuity_diagnostics(session_id,created_ms DESC,decision_id DESC)''',
)
VISIBLE_FACT_SQL = """(substr(facts.id,1,7)!='memory:' OR EXISTS (
    SELECT 1 FROM continuity_memories cm JOIN continuity_policy cp ON cp.session_id=cm.session_id
    WHERE cm.session_id=facts.session_id AND 'memory:'||cm.id=facts.id AND cm.status='active'
    AND json_extract(cp.settings_json,'$.mode')='enforce'))"""


def initialize(db):
    for sql in SQL:
        db.execute(sql)


def bounded(value, name, maximum):
    value = text(value, name)
    require(len(value) <= maximum, 'memory_' + name + '_too_long')
    return value


def token(value):
    value = bounded(value, 'key', 80)
    require(re.fullmatch(r'[^\s/\\]+', value) is not None, 'invalid_memory_key')
    return value


def scopes(values):
    require(isinstance(values, list) and 1 <= len(values) <= 6, 'invalid_memory_scopes')
    require(all(isinstance(value, str) for value in values), 'invalid_memory_scopes')
    result = [token(value) for value in values]
    require(len(set(result)) == len(result), 'duplicate_memory_scope')
    return result


def policy(db, session):
    row = db.execute('SELECT settings_json FROM continuity_policy WHERE session_id=?', (session,)).fetchone()
    return decode(row[0]) if row else {'mode': 'off', 'scopes': ['private_dm']}


def configure(store, session, values, now):
    require(isinstance(values, dict) and set(values) <= {'mode', 'scopes'}, 'invalid_memory_settings')
    with store.connect(True, operation='memory_configure', session=session) as db:
        require(db.execute('SELECT 1 FROM sessions WHERE id=?', (session,)).fetchone() is not None,
                'session_not_found')
        cfg = {**policy(db, session), **values}
        require(cfg['mode'] in ('off', 'shadow', 'enforce'), 'invalid_memory_mode')
        cfg['scopes'] = scopes(cfg['scopes'])
        db.execute('INSERT INTO continuity_policy VALUES (?,?,?) ON CONFLICT(session_id) '
                   'DO UPDATE SET settings_json=excluded.settings_json,updated_ms=excluded.updated_ms',
                   (session, encode(cfg), now))
        if cfg['mode'] == 'off':
            db.execute("UPDATE continuity_memories SET status='cancelled',reason='policy_off',updated_ms=? "
                       "WHERE session_id=? AND status='pending'", (now, session))
        db.execute('UPDATE sessions SET revision=revision+1 WHERE id=?', (session,))
        return {'ok': True, 'reason': 'memory_configured', 'settings': cfg,
                'revision': db.execute('SELECT revision FROM sessions WHERE id=?', (session,)).fetchone()[0]}


def incoming_evidence(db, session, evidence, channel):
    require(isinstance(evidence, list) and len(evidence) <= 4, 'invalid_memory_evidence')
    seen = set()
    for item in evidence:
        require(isinstance(item, dict) and set(item) == {'message_id', 'excerpt'}, 'invalid_memory_evidence')
        mid = bounded(item['message_id'], 'message_id', 200)
        excerpt = bounded(item['excerpt'], 'evidence_excerpt', 200)
        require(mid not in seen, 'duplicate_memory_evidence')
        seen.add(mid)
        row = db.execute('SELECT * FROM dm_messages WHERE session_id=? AND id=?', (session, mid)).fetchone()
        require(row is not None, 'memory_evidence_not_found')
        observation = decode(row['observation_json'])
        require(observation.get('direction') == 'incoming'
                and observation.get('identity') in ('platform', 'verified_composite')
                and not observation.get('review_required'), 'memory_evidence_not_verified')
        require(observation.get('channel', 'instagram') == channel, 'memory_evidence_channel_mismatch')
        require(excerpt in row['text'], 'memory_evidence_excerpt_mismatch')


def render(row):
    result = {key: row[key] for key in ('id', 'kind', 'scope', 'channel', 'summary', 'scope_note',
                                       'status', 'created_ms', 'activated_ms', 'supersedes', 'reason')}
    result.update(key=row['memory_key'], fact_id=PREFIX + row['id'] if row['activated_ms'] is not None else None,
                  evidence=decode(row['evidence_json']), job_id=row['job_id'], excerpt=row['excerpt'],
                  interpretation='발언 증거는 내용의 진실·현재 의사·확정 약속을 뜻하지 않음'
                  if row['kind'] == 'utterance' else '명시된 범위에서만 적용; 이후 변경·철회 가능')
    return result


def _activate(db, session, row, now):
    try:
        incoming_evidence(db, session, decode(row['evidence_json']), row['channel'])
        if row['job_id'] is not None:
            job = db.execute('SELECT * FROM dm_jobs WHERE session_id=? AND id=?', (session, row['job_id'])).fetchone()
            require(job is not None and job['status'] == 'confirmed' and row['excerpt'] in job['text'],
                    'memory_send_not_confirmed')
        previous = None
        if row['kind'] != 'utterance':
            previous = db.execute("SELECT * FROM continuity_memories WHERE session_id=? AND kind=? "
                "AND memory_key=? AND scope=? AND channel=? AND status='active'",
                (session, row['kind'], row['memory_key'], row['scope'], row['channel'])).fetchone()
            require((previous['id'] if previous else None) == row['supersedes'], 'memory_version_changed')
        if policy(db, session)['mode'] == 'off':
            raise ControlError('memory_policy_off')
    except ControlError as error:
        # A stale optional memory must not roll back a real positive delivery receipt.
        db.execute("UPDATE continuity_memories SET status='cancelled',reason=?,updated_ms=? "
                   'WHERE session_id=? AND id=?', (error.reason, now, session, row['id']))
        return
    if previous:
        db.execute("UPDATE continuity_memories SET status='superseded',updated_ms=? WHERE session_id=? AND id=?",
                   (now, session, previous['id']))
    db.execute("UPDATE continuity_memories SET status='active',activated_ms=?,updated_ms=? "
               'WHERE session_id=? AND id=?', (now, now, session, row['id']))
    details = dict(kind='memory_continuity', memory_kind=row['kind'], key=row['memory_key'],
                   scope=row['scope'], channel=row['channel'], scope_note=row['scope_note'],
                   evidence='verified incoming excerpts and/or confirmed outgoing job',
                   sources=decode(row['evidence_json']), job_id=row['job_id'], excerpt=row['excerpt'])
    summary = 'self_said:' if row['kind'] == 'utterance' else 'memory_' + row['kind'] + ':'
    summary += row['memory_key'] + ' ' + row['summary']
    occurred = job['updated_ms'] if row['job_id'] else now
    db.execute('INSERT INTO facts VALUES (?,?,?,?,?,?,?)',
               (session, PREFIX + row['id'], occurred, now, 'observed', summary, encode(details)))


def sync(db, session, now, job_id=None):
    sql = "SELECT cm.*,j.status AS job_status FROM continuity_memories cm LEFT JOIN dm_jobs j "
    sql += "ON j.session_id=cm.session_id AND j.id=cm.job_id WHERE cm.session_id=? AND cm.status='pending'"
    args = [session]
    if job_id is not None:
        sql += ' AND cm.job_id=?'
        args.append(job_id)
    for row in list(db.execute(sql + ' ORDER BY cm.created_ms,cm.id', args)):
        if row['job_status'] in ('failed', 'cancelled'):
            db.execute("UPDATE continuity_memories SET status='cancelled',reason='send_not_completed',updated_ms=? "
                       'WHERE session_id=? AND id=?', (now, session, row['id']))
        elif row['job_id'] is None or row['job_status'] == 'confirmed':
            _activate(db, session, row, now)


def apply_ops(db, session, operations, now):
    require(isinstance(operations, list) and len(operations) <= 12, 'invalid_memory_ops')
    if not operations:
        sync(db, session, now)
        return []
    require(policy(db, session)['mode'] != 'off', 'memory_policy_off')
    sync(db, session, now)
    for operation in operations:
        require(isinstance(operation, dict), 'invalid_memory_op')
        require(len(encode(operation).encode('utf-8')) <= MAX_OPERATION_BYTES, 'memory_op_too_large')
        if operation.get('op') == 'revoke':
            require(set(operation) == {'op', 'id', 'reason'}, 'invalid_memory_revoke')
            row = db.execute('SELECT * FROM continuity_memories WHERE session_id=? AND id=?',
                             (session, token(operation['id']))).fetchone()
            require(row is not None and row['status'] in ('active', 'pending'), 'memory_not_open')
            reason = bounded(operation['reason'], 'reason', 240)
            db.execute("UPDATE continuity_memories SET status='revoked',reason=?,updated_ms=? "
                       'WHERE session_id=? AND id=?', (reason, now, session, row['id']))
            continue
        required = {'op', 'id', 'kind', 'key', 'scope', 'channel', 'summary', 'scope_note'}
        if operation.get('kind') != 'utterance':
            required.add('evidence')
        require(operation.get('op') == 'remember' and required <= set(operation)
                and set(operation) <= required | {'evidence', 'on_send', 'supersedes'}, 'invalid_memory_remember')
        mid, key, scope = [token(operation[name]) for name in ('id', 'key', 'scope')]
        kind, channel = operation['kind'], operation['channel']
        require(kind in ('nickname', 'preference', 'utterance'), 'invalid_memory_kind')
        require(channel in ('slack', 'instagram'), 'invalid_memory_channel')
        summary = bounded(operation['summary'], 'summary', 400)
        note = bounded(operation['scope_note'], 'scope_note', 240)
        require(db.execute('SELECT 1 FROM continuity_memories WHERE session_id=? AND id=?',
                           (session, mid)).fetchone() is None, 'memory_identity_collision')
        require(db.execute('SELECT 1 FROM facts WHERE session_id=? AND id=?',
                           (session, PREFIX + mid)).fetchone() is None, 'memory_fact_collision')
        evidence = operation.get('evidence', [])
        incoming_evidence(db, session, evidence, channel)
        require(kind == 'utterance' or bool(evidence), 'memory_incoming_evidence_required')
        job_id = excerpt = None
        send = operation.get('on_send')
        require(kind == 'preference' or send is not None, 'memory_confirmed_send_required')
        if send is not None:
            require(isinstance(send, dict) and set(send) == {'group_id', 'part_index', 'excerpt'},
                    'invalid_memory_send')
            integer(send['part_index'], 'memory_part_index')
            excerpt = bounded(send['excerpt'], 'send_excerpt', 400)
            job = db.execute('SELECT j.*,gc.channel FROM dm_jobs j JOIN group_channels gc '
                'ON gc.session_id=j.session_id AND gc.group_id=j.group_id '
                'WHERE j.session_id=? AND j.group_id=? AND j.part_index=?',
                (session, bounded(send['group_id'], 'group_id', 200), send['part_index'])).fetchone()
            require(job is not None and job['channel'] == channel, 'memory_send_not_found')
            require(job['status'] in ('pending','attempting','submitted','unknown','confirmed'),
                    'memory_send_not_open')
            require(excerpt in job['text'], 'memory_send_excerpt_mismatch')
            job_id = job['id']
            if kind == 'utterance':
                require(db.execute("SELECT 1 FROM continuity_memories WHERE session_id=? AND kind='utterance' "
                    "AND job_id=? AND excerpt=? AND status NOT IN ('revoked','cancelled')",
                    (session, job_id, excerpt)).fetchone() is None, 'memory_duplicate_utterance')
        previous = db.execute("SELECT * FROM continuity_memories WHERE session_id=? AND kind=? "
            "AND memory_key=? AND scope=? AND channel=? AND status='active'",
            (session, kind, key, scope, channel)).fetchone() if kind != 'utterance' else None
        supersedes = operation.get('supersedes')
        require(kind != 'utterance' or supersedes is None, 'memory_utterance_is_event')
        require((previous['id'] if previous else None) == supersedes, 'memory_explicit_version_required')
        if kind != 'utterance':
            require(db.execute("SELECT 1 FROM continuity_memories WHERE session_id=? AND kind=? "
                "AND memory_key=? AND scope=? AND channel=? AND status='pending'",
                (session, kind, key, scope, channel)).fetchone() is None, 'memory_candidate_pending')
        db.execute('INSERT INTO continuity_memories VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (session, mid, kind, key, scope, channel, summary, note, encode(evidence), job_id, excerpt,
             supersedes, 'pending', now, None, now, None))
    sync(db, session, now)
    return [dict(db.execute('SELECT id,status,reason FROM continuity_memories WHERE session_id=? AND id=?',
                            (session, op['id'])).fetchone()) for op in operations]


def apply_for_decision(db, session, operations, now):
    """Reject a shadow memory batch without discarding a valid parent decision.

    Only expected validation failures are isolated. Database failures and unrelated
    decision errors still abort the outer transaction. No argument is repaired.
    """
    if policy(db, session)['mode'] != 'shadow':
        return apply_ops(db, session, operations, now), None
    db.execute('SAVEPOINT shadow_memory_batch')
    try:
        results = apply_ops(db, session, operations, now)
    except ControlError as exc:
        db.execute('ROLLBACK TO shadow_memory_batch')
        return [], {'mode': 'shadow', 'status': 'shadow_rejected', 'error_code': exc.reason,
                    'operation_count': len(operations) if isinstance(operations, list) else None}
    finally:
        db.execute('RELEASE shadow_memory_batch')
    return results, None


def valid_factual_basis(db, session, fact_id, channel):
    if not fact_id.startswith(PREFIX):
        return True
    row = db.execute('SELECT kind,status,scope,channel FROM continuity_memories WHERE session_id=? AND id=?',
                     (session, fact_id[len(PREFIX):])).fetchone()
    cfg = policy(db, session)
    return (row is not None and row['kind'] != 'utterance' and row['status'] == 'active'
            and cfg['mode'] == 'enforce' and row['channel'] == channel and row['scope'] in cfg['scopes'])


def context(db, session, channel, selected_scopes=None, *, key=None, history=False, offset=0, limit=6):
    require(channel in ('slack', 'instagram'), 'invalid_memory_channel')
    integer(offset, 'memory_offset'); integer(limit, 'memory_limit', 1)
    require(limit <= 10, 'memory_limit_too_large')
    selected = scopes(selected_scopes if selected_scopes is not None else policy(db, session)['scopes'])
    require(not history or key is not None, 'memory_history_key_required')
    where = 'session_id=? AND channel=? AND scope IN (' + ','.join('?' for _ in selected) + ')'
    args = [session, channel, *selected]
    if key is not None:
        where += ' AND memory_key=?'; args.append(token(key))
    if not history:
        where += " AND status='active'"
    total = db.execute('SELECT count(*) FROM continuity_memories WHERE ' + where, args).fetchone()[0]
    rows = db.execute('SELECT * FROM continuity_memories WHERE ' + where +
        " ORDER BY CASE kind WHEN 'nickname' THEN 0 WHEN 'preference' THEN 1 ELSE 2 END,created_ms DESC,id LIMIT ? OFFSET ?",
        (*args, limit, offset)).fetchall()
    return dict(mode=policy(db, session)['mode'], scopes=selected, channel=channel, history=history,
                items=[render(row) for row in rows], total=total, offset=offset, has_more=offset+len(rows)<total)


def snapshot(db, session):
    result = {channel: context(db, session, channel, limit=3) for channel in ('slack', 'instagram')}
    # Keep evidence intact. The caller can page explicitly instead of receiving cut quotations.
    while len(encode(result).encode('utf-8')) > MAX_SNAPSHOT_BYTES:
        candidates = [page for page in result.values() if page['items']]
        page = max(candidates, key=lambda p: len(encode(p['items'][-1]).encode('utf-8')))
        page['items'].pop()
        page['has_more'] = True
    return result


def history_search(db, session, channel, keywords, selected_scopes=None, *, offset=0, limit=6,
                   order='newest', before_ms=None):
    """Discover keys in confirmed history without injecting past states as current memory."""
    require(channel in ('slack', 'instagram'), 'invalid_memory_channel')
    require(isinstance(keywords, list) and 1 <= len(keywords) <= 10, 'invalid_memory_keywords')
    integer(offset, 'memory_offset'); integer(limit, 'memory_limit', 1)
    require(limit <= 10 and order in ('newest', 'oldest'), 'invalid_memory_page')
    selected = scopes(selected_scopes if selected_scopes is not None else policy(db, session)['scopes'])
    patterns = ['%' + bounded(k, 'keyword', 100).replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'
                for k in keywords]
    fields = ('memory_key', 'summary', 'scope_note', 'reason', 'excerpt', 'evidence_json')
    match = ' OR '.join(f"{field} LIKE ? ESCAPE '\\'" for _ in patterns for field in fields)
    where = ('session_id=? AND channel=? AND scope IN (' + ','.join('?' for _ in selected) + ')'
             " AND activated_ms IS NOT NULL AND status IN ('active','superseded','revoked') AND (" + match + ')')
    args = [session, channel, *selected, *(p for p in patterns for _ in fields)]
    if before_ms is not None:
        integer(before_ms, 'memory_before_ms'); where += ' AND updated_ms<?'; args.append(before_ms)
    total = db.execute('SELECT count(*) FROM continuity_memories WHERE ' + where, args).fetchone()[0]
    direction = 'ASC' if order == 'oldest' else 'DESC'
    rows = db.execute('SELECT * FROM continuity_memories WHERE ' + where +
        f' ORDER BY updated_ms {direction},id {direction} LIMIT ? OFFSET ?', (*args, limit, offset)).fetchall()
    metadata = ('id', 'kind', 'scope', 'channel', 'summary', 'scope_note', 'status',
                'created_ms', 'activated_ms', 'updated_ms', 'supersedes', 'reason')
    items = [{**{k: row[k] for k in metadata}, 'key': row['memory_key']} for row in rows]
    return dict(mode=policy(db, session)['mode'], scopes=selected, channel=channel, items=items,
                total=total, offset=offset, has_more=offset+len(items)<total, order=order, before_ms=before_ms,
                purpose='history_discovery', next_read='memory-context with returned key and history=true',
                interpretation='이력 발견용 메타데이터; 현재 허용·선호는 활성 상태와 범위를 별도로 확인')


def recall(db, session, keywords, visible_sql, *, offset=0, limit=10, order='newest', before_ms=None):
    require(policy(db, session)['mode'] != 'off', 'memory_policy_off')
    require(isinstance(keywords, list) and 1 <= len(keywords) <= 10, 'invalid_memory_keywords')
    integer(offset, 'memory_offset'); integer(limit, 'memory_limit', 1)
    require(limit <= 10 and order in ('newest', 'oldest'), 'invalid_memory_page')
    patterns = ['%' + bounded(k, 'keyword', 100).replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'
                for k in keywords]
    match = ' OR '.join("(summary LIKE ? ESCAPE '\\' OR details_json LIKE ? ESCAPE '\\')" for _ in patterns)
    where = "session_id=? AND substr(id,1,9)!='decision:' AND " + visible_sql + ' AND (' + match + ')'
    args = [session, *(p for pattern in patterns for p in (pattern, pattern))]
    if before_ms is not None:
        integer(before_ms, 'memory_before_ms'); where += ' AND recorded_ms<?'; args.append(before_ms)
    # Shadow reads are explicit diagnostics; automatic legacy recall stays unchanged.
    where += " AND (substr(id,1,7)!='memory:' OR EXISTS (SELECT 1 FROM continuity_memories cm "
    where += "WHERE cm.session_id=facts.session_id AND 'memory:'||cm.id=facts.id AND cm.status='active'))"
    total = db.execute('SELECT count(*) FROM facts WHERE ' + where, args).fetchone()[0]
    direction = 'ASC' if order == 'oldest' else 'DESC'
    rows = db.execute('SELECT * FROM facts WHERE ' + where + ' ORDER BY recorded_ms ' + direction +
                      ',id LIMIT ? OFFSET ?', (*args, limit, offset)).fetchall()
    facts = []
    for row in rows:
        item = dict(row); item['details'] = decode(item.pop('details_json')); facts.append(item)
    return dict(ok=True, reason='memory_recalled', facts=facts, total=total, offset=offset,
                has_more=offset+len(facts)<total, order=order, before_ms=before_ms,
                mode=policy(db, session)['mode'])
