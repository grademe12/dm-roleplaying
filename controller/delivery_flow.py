"""Conversation progress is independent of eventual delivery confirmation.

A pass never proves delivery and never makes the original job retryable.
Its timestamp is immutable, including after a late positive/negative receipt.
"""
PASSED = ('confirmed', 'submitted', 'unknown')
SQL = '''CREATE TABLE IF NOT EXISTS dm_flow_passes (
    session_id TEXT NOT NULL, job_id TEXT NOT NULL, passed_ms INTEGER NOT NULL,
    PRIMARY KEY(session_id,job_id),
    FOREIGN KEY(session_id,job_id) REFERENCES dm_jobs(session_id,id))'''


def initialize(db):
    db.execute(SQL)
    db.execute('CREATE INDEX IF NOT EXISTS dm_flow_time ON dm_flow_passes(session_id,passed_ms DESC)')
    db.execute('''INSERT OR IGNORE INTO dm_flow_passes
        SELECT session_id,id,updated_ms FROM dm_jobs
        WHERE status IN ('confirmed','submitted','unknown')''')


def record(db, session, jid, status, now):
    if status in PASSED:
        db.execute('INSERT OR IGNORE INTO dm_flow_passes VALUES (?,?,?)', (session,jid,now))


def complete_group(db, session, gid):
    if not db.execute('SELECT 1 FROM dm_jobs WHERE session_id=? AND group_id=?', (session,gid)).fetchone():
        return False
    if db.execute('''SELECT 1 FROM dm_jobs j WHERE j.session_id=? AND j.group_id=?
        AND NOT EXISTS (SELECT 1 FROM dm_flow_passes f
            WHERE f.session_id=j.session_id AND f.job_id=j.id)''', (session,gid)).fetchone():
        return False
    changed = db.execute("UPDATE reply_groups SET status='completed' WHERE session_id=? AND id=? AND status='active'",
                         (session,gid)).rowcount
    if changed:
        db.execute('''UPDATE dm_messages SET status='handled',reply_due_ms=NULL
            WHERE session_id=? AND id IN (SELECT message_id FROM reply_targets
            WHERE session_id=? AND group_id=? AND active=1)''', (session,session,gid))
        db.execute('UPDATE reply_targets SET active=0 WHERE session_id=? AND group_id=?', (session,gid))
    return bool(changed)


def advance_session(db, session):
    # Also handles pre-patch queues without requiring a model/manual repair.
    changed = False
    for g in db.execute("SELECT id FROM reply_groups WHERE session_id=? AND status='active'", (session,)).fetchall():
        changed = complete_group(db,session,g['id']) or changed
    return changed
