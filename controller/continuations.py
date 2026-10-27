"""Bounded lookahead for explicitly requested conversation continuations.

No language classifier or transport here. New input invalidates pending drafts;
the model must explicitly resume with the new generation before writing more.
"""
from common import decode, integer, require, text
import conversation_flow

SQL = (
    '''CREATE TABLE IF NOT EXISTS continuation_runs (
       session_id TEXT NOT NULL REFERENCES sessions(id), id TEXT NOT NULL,
       channel TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN
         ('active','paused','stopped','expired','completed')),
       generation INTEGER NOT NULL, source_message_id TEXT NOT NULL,
       interval_ms INTEGER NOT NULL, buffer_limit INTEGER NOT NULL,
       expires_ms INTEGER, max_groups INTEGER, review_at_ms INTEGER NOT NULL,
       created_ms INTEGER NOT NULL, updated_ms INTEGER NOT NULL, reason TEXT NOT NULL,
       PRIMARY KEY(session_id,id))''',
    '''CREATE UNIQUE INDEX IF NOT EXISTS continuation_open_channel
       ON continuation_runs(session_id,channel) WHERE status IN ('active','paused')''',
    '''CREATE TABLE IF NOT EXISTS continuation_groups (
       session_id TEXT NOT NULL, group_id TEXT NOT NULL, run_id TEXT NOT NULL,
       generation INTEGER NOT NULL, sequence INTEGER NOT NULL,
       PRIMARY KEY(session_id,group_id), UNIQUE(session_id,run_id,sequence),
       FOREIGN KEY(session_id,run_id) REFERENCES continuation_runs(session_id,id),
       FOREIGN KEY(session_id,group_id) REFERENCES reply_groups(session_id,id))''',
)


def initialize(db):
    for sql in SQL:
        db.execute(sql)


def get(db, session, run_id):
    row = db.execute('SELECT * FROM continuation_runs WHERE session_id=? AND id=?',
                     (session, run_id)).fetchone()
    require(row is not None, 'continuation_not_found')
    return row


def owner(db, session, gid):
    return db.execute('''SELECT r.*,c.sequence,c.generation AS group_generation
        FROM continuation_groups c JOIN continuation_runs r
        ON r.session_id=c.session_id AND r.id=c.run_id
        WHERE c.session_id=? AND c.group_id=?''', (session, gid)).fetchone()


def counts(db, session, rid):
    return db.execute('''SELECT count(*) AS total,
        COALESCE(sum(g.status='active'),0) AS buffered,
        COALESCE(sum(g.status='completed'),0) AS completed
        FROM continuation_groups c JOIN reply_groups g
        ON g.session_id=c.session_id AND g.id=c.group_id
        WHERE c.session_id=? AND c.run_id=?''', (session, rid)).fetchone()


def discard_pending(db, session, rid, now):
    """Keep in-flight/unknown evidence; only definitely unsent work is cancelled."""
    for row in db.execute('''SELECT c.group_id FROM continuation_groups c JOIN reply_groups g
                          ON g.session_id=c.session_id AND g.id=c.group_id
                          WHERE c.session_id=? AND c.run_id=? AND g.status='active' ''',
                          (session, rid)).fetchall():
        gid = row[0]
        db.execute("""UPDATE dm_jobs SET status='cancelled',updated_ms=?
            WHERE session_id=? AND group_id=? AND status IN ('pending','failed')""", (now, session, gid))
        unresolved = db.execute("""SELECT 1 FROM dm_jobs WHERE session_id=? AND group_id=?
            AND status='attempting'""", (session, gid)).fetchone()
        if not unresolved:
            finalize_discard(db, session, gid)
        conversation_flow.sync_group(db, session, gid, now)


def finalize_discard(db, session, gid):
    # Once flow has passed, do not replay its source request even without a receipt.
    sent = db.execute('''SELECT 1 FROM dm_jobs j JOIN dm_flow_passes f
                      ON f.session_id=j.session_id AND f.job_id=j.id WHERE j.session_id=? AND j.group_id=?''',
                      (session, gid)).fetchone()
    db.execute("UPDATE reply_groups SET status=? WHERE session_id=? AND id=? AND status='active'",
               ('completed' if sent else 'cancelled', session, gid))
    db.execute('''UPDATE dm_messages SET status=?,reply_due_ms=NULL WHERE session_id=? AND id IN
        (SELECT message_id FROM reply_targets WHERE session_id=? AND group_id=? AND active=1)''',
               ('handled' if sent else 'received', session, session, gid))
    db.execute('UPDATE reply_targets SET active=0 WHERE session_id=? AND group_id=?', (session, gid))


def transition(db, session, run, status, reason, now):
    db.execute('''UPDATE continuation_runs SET status=?,generation=generation+1,
        updated_ms=?,reason=? WHERE session_id=? AND id=?''',
               (status, now, reason, session, run['id']))
    discard_pending(db, session, run['id'], now)


def incoming(db, session, channel, now):
    # Observation revisions and attention belong to the shared persona/session.
    for run in db.execute("SELECT * FROM continuation_runs WHERE session_id=? AND status='active'",
                           (session,)).fetchall():
        transition(db, session, run, 'paused', 'incoming_message:'+channel, now)


def maintain(db, session, now):
    """Called within existing write transactions, including before every claim."""
    changed = False
    unavailable = db.execute("SELECT 1 FROM activities WHERE session_id=? AND status='in_progress' AND attention='unavailable'",
                              (session,)).fetchone()
    for run in db.execute("SELECT * FROM continuation_runs WHERE session_id=? AND status IN ('active','paused')",
                           (session,)).fetchall():
        if run['expires_ms'] is not None and run['expires_ms'] <= now:
            transition(db, session, run, 'expired', 'expired', now)
            changed = True
        elif run['status'] == 'active':
            stale = db.execute('''SELECT 1 FROM continuation_groups c JOIN reply_groups g
                ON g.session_id=c.session_id AND g.id=c.group_id
                LEFT JOIN group_reviews gr ON gr.session_id=g.session_id AND gr.group_id=g.id
                JOIN sessions s ON s.id=c.session_id WHERE c.session_id=? AND c.run_id=?
                AND g.status='active' AND (g.observation_revision<>s.dm_observation_revision OR EXISTS (
                    SELECT 1 FROM activities a WHERE a.session_id=g.session_id AND a.attention='unavailable'
                    AND a.status IN ('in_progress','completed','interrupted')
                    AND a.start_ms>=COALESCE(gr.reviewed_ms,g.created_ms) AND a.start_ms<=?)) LIMIT 1''',
                (session,run['id'],now)).fetchone()
            failure = db.execute("""SELECT 1 FROM continuation_groups c JOIN dm_jobs j
                ON j.session_id=c.session_id AND j.group_id=c.group_id
                JOIN reply_groups g ON g.session_id=c.session_id AND g.id=c.group_id
                WHERE c.session_id=? AND c.run_id=? AND g.status='active' AND
                ((j.status='failed' AND NOT EXISTS (SELECT 1 FROM dm_flow_passes f
                    WHERE f.session_id=j.session_id AND f.job_id=j.id))) LIMIT 1""",
                (session, run['id'])).fetchone()
            if unavailable or failure or stale:
                reason = ('activity_unavailable' if unavailable else 'context_changed' if stale
                          else 'delivery_failed')
                transition(db, session, run, 'paused', reason, now)
                changed = True
            elif run['max_groups'] is not None:
                n = counts(db, session, run['id'])
                if n['total'] >= run['max_groups'] and n['buffered'] == 0:
                    transition(db, session, run, 'completed', 'group_limit', now)
                    changed = True
    return changed


def snapshot(db, session):
    # Closed runs are bounded; open state is always present.
    rows = db.execute('''SELECT * FROM continuation_runs WHERE session_id=?
        ORDER BY (status IN ('active','paused')) DESC,updated_ms DESC,id LIMIT 32''', (session,)).fetchall()
    out = []
    for row in rows:
        n = counts(db, session, row['id'])
        groups = [dict(g) for g in db.execute('''SELECT c.group_id,c.sequence,g.status
            FROM continuation_groups c JOIN reply_groups g ON g.session_id=c.session_id AND g.id=c.group_id
            WHERE c.session_id=? AND c.run_id=? AND g.status='active' ORDER BY c.sequence''', (session, row['id']))]
        out.append({**dict(row), **dict(n), 'groups': groups})
    return out


def due_ids(db, session, now):
    out = []
    for run in db.execute("SELECT * FROM continuation_runs WHERE session_id=? AND status='active' ORDER BY id", (session,)):
        n = counts(db, session, run['id'])
        capped = run['max_groups'] is not None and n['total'] >= run['max_groups']
        if ((run['expires_ms'] is not None and run['expires_ms'] <= now) or
                (not capped and n['buffered'] <= 1 and run['review_at_ms'] <= now)):
            out.append(run['id'])
    return out


def _source(db, session, mid, channel):
    row = db.execute('SELECT * FROM dm_messages WHERE session_id=? AND id=?', (session, text(mid, 'source_message_id'))).fetchone()
    require(row is not None, 'continuation_source_not_found')
    info = decode(row['observation_json'])
    require(info.get('direction') == 'incoming' and not info.get('review_required') and
            info.get('channel', 'instagram') == channel, 'invalid_continuation_source')


def apply_ops(db, session, ops, now):
    require(isinstance(ops, list) and len(ops) <= 16, 'invalid_continuation_ops')
    seen = set()
    for op in ops:
        require(isinstance(op, dict), 'invalid_continuation_op')
        rid = text(op.get('id'), 'continuation_id')
        require(rid not in seen, 'duplicate_continuation_op')
        seen.add(rid)
        action = op.get('action')
        reason = text(op.get('reason'), 'continuation_reason')
        if action == 'start':
            require(set(op) <= {'id','action','reason','source_message_id','channel','interval_ms',
                               'buffer_limit','expires_ms','max_groups'}, 'unknown_continuation_field')
            channel = op.get('channel', 'instagram')
            require(channel in ('instagram','slack'), 'invalid_channel')
            _source(db, session, op.get('source_message_id'), channel)
            require(not db.execute("SELECT 1 FROM continuation_runs WHERE session_id=? AND channel=? AND status IN ('active','paused')",
                                   (session, channel)).fetchone(), 'continuation_already_open')
            interval = integer(op.get('interval_ms', 60000), 'interval_ms', 5000)
            require(interval <= 3600000, 'continuation_interval_too_large')
            buffer = integer(op.get('buffer_limit', 3), 'buffer_limit', 2)
            require(buffer <= 3, 'continuation_buffer_too_large')
            expires = op.get('expires_ms')
            if expires is not None:
                require(integer(expires, 'expires_ms') > now, 'continuation_expired')
            maximum = op.get('max_groups')
            if maximum is not None: integer(maximum, 'max_groups', 1)
            require(not db.execute('SELECT 1 FROM continuation_runs WHERE session_id=? AND id=?', (session,rid)).fetchone(),
                    'continuation_identity_collision')
            db.execute('INSERT INTO continuation_runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       (session,rid,channel,'active',1,op['source_message_id'],interval,buffer,expires,maximum,
                        now+interval,now,now,reason))
        else:
            allowed = {'id','action','reason','expected_generation'}
            if action == 'resume': allowed.add('source_message_id')
            if action == 'defer': allowed.add('review_at_ms')
            require(set(op) <= allowed,
                    'unknown_continuation_field')
            run = get(db, session, rid)
            require(integer(op.get('expected_generation'), 'expected_generation', 1) == run['generation'],
                    'continuation_generation_conflict')
            require(run['status'] in ('active','paused'), 'continuation_closed')
            if action == 'resume':
                require(run['status'] == 'paused', 'continuation_not_paused')
                require(run['expires_ms'] is None or run['expires_ms'] > now, 'continuation_expired')
                _source(db, session, op.get('source_message_id'), run['channel'])
                require(not db.execute('''SELECT 1 FROM continuation_groups c JOIN dm_jobs j
                    ON j.session_id=c.session_id AND j.group_id=c.group_id
                    WHERE c.session_id=? AND c.run_id=? AND j.status='attempting' ''',
                    (session,rid)).fetchone(), 'continuation_dispatch_unresolved')
                db.execute('''UPDATE continuation_runs SET status='active',generation=generation+1,
                    source_message_id=?,review_at_ms=?,updated_ms=?,reason=? WHERE session_id=? AND id=?''',
                    (op['source_message_id'],now+run['interval_ms'],now,reason,session,rid))
            elif action in ('pause','stop'):
                transition(db, session, run, 'paused' if action == 'pause' else 'stopped', reason, now)
            elif action == 'defer':
                require(run['status'] == 'active', 'continuation_not_active')
                review = integer(op.get('review_at_ms'), 'review_at_ms')
                require(review > now, 'continuation_review_not_future')
                db.execute('UPDATE continuation_runs SET review_at_ms=?,updated_ms=?,reason=? WHERE session_id=? AND id=?',
                           (review,now,reason,session,rid))
            else:
                require(False, 'invalid_continuation_action')


def prepare_group(db, session, d, now):
    ref = d.get('continuation')
    if ref is None:
        return None
    require(d['action'] in ('reply','initiate') and isinstance(ref,dict) and set(ref) == {'id','generation'},
            'invalid_continuation_group')
    run = get(db, session, text(ref['id'], 'continuation_id'))
    require(integer(ref['generation'], 'generation', 1) == run['generation'], 'continuation_generation_conflict')
    require(run['status'] == 'active', 'continuation_not_active')
    require(run['expires_ms'] is None or run['expires_ms'] > now, 'continuation_expired')
    n = counts(db, session, run['id'])
    require(n['buffered'] < run['buffer_limit'], 'continuation_buffer_full')
    require(run['max_groups'] is None or n['total'] < run['max_groups'], 'continuation_group_limit')
    return run


def attach(db, session, d, run, now):
    gid = d['group_id']
    channel = db.execute('SELECT channel FROM group_channels WHERE session_id=? AND group_id=?', (session,gid)).fetchone()[0]
    require(channel == run['channel'], 'continuation_channel_mismatch')
    sequence = db.execute('SELECT COALESCE(max(sequence),0)+1 FROM continuation_groups WHERE session_id=? AND run_id=?',
                          (session,run['id'])).fetchone()[0]
    db.execute('INSERT INTO continuation_groups VALUES (?,?,?,?,?)', (session,gid,run['id'],run['generation'],sequence))
    db.execute('UPDATE continuation_runs SET review_at_ms=?,updated_ms=? WHERE session_id=? AND id=?',
               (now+run['interval_ms'],now,session,run['id']))


def claim_hold(db, session, gid):
    run = owner(db, session, gid)
    if run is None: return None
    if run['status'] != 'active' or run['group_generation'] != run['generation']:
        return 'continuation_inactive'
    if db.execute('''SELECT 1 FROM continuation_groups c JOIN reply_groups g
        ON g.session_id=c.session_id AND g.id=c.group_id WHERE c.session_id=? AND c.run_id=?
        AND c.sequence<? AND g.status='active' ''', (session,run['id'],run['sequence'])).fetchone():
        return 'continuation_previous_group'
    return None


def after_result(db, session, gid, now):
    run = owner(db, session, gid)
    if run is None: return
    # A stop may arrive while the transport is running; finish bookkeeping only.
    if run['status'] != 'active' and not db.execute("""SELECT 1 FROM dm_jobs WHERE session_id=? AND group_id=?
        AND status='attempting'""", (session,gid)).fetchone():
        # A late negative proof can turn an unknown part into failed after the
        # original cancellation. Also release its unsent media/reservations.
        discard_pending(db, session, run['id'], now)
    maintain(db, session, now)
