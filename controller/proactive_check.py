"""Message-only timer for re-judging initiate, independent of life next_decision_ms.

Seed rule: sessions.proactive_check_after_ms is the absolute next-check time.
When NULL, fall back to last outbound dm_job created_ms + interval. If there is
no outbound yet, this reason does not fire (status awaiting_outbound) — life wakes
can still initiate; the periodic timer starts after the first outbound or after an
apply that advanced the column. Controllers advance the column on apply when
proactive_check_due was present and/or a reply/initiate was saved, so Muse cannot
forget to bump the timer. Life next_decision_ms / advance_to_ms are untouched.
"""
import persona

OUTBOUND_STATUSES = ('pending', 'attempting', 'confirmed', 'submitted', 'unknown')


def ensure_schema(db):
    """Add sessions.proactive_check_after_ms on open, like other ensure-SQL migrations."""
    if any(r[1] == 'proactive_check_after_ms' for r in db.execute('PRAGMA table_info(sessions)')):
        return
    db.execute('BEGIN IMMEDIATE')
    if not any(r[1] == 'proactive_check_after_ms' for r in db.execute('PRAGMA table_info(sessions)')):
        db.execute('ALTER TABLE sessions ADD COLUMN proactive_check_after_ms INTEGER')
    db.commit()


def interval_ms():
    return int(persona.PROACTIVE_CHECK_INTERVAL_MS)


def last_outbound_ms(db, session):
    row = db.execute('''SELECT max(created_ms) FROM dm_jobs
        WHERE session_id=? AND status IN ('pending','attempting','confirmed','submitted','unknown')''',
                     (session,)).fetchone()
    return row[0] if row and row[0] is not None else None


def effective_after_ms(db, session, stored):
    """Earliest ms when proactive_check_due may fire, or None if not schedulable yet."""
    if stored is not None:
        return stored
    last = last_outbound_ms(db, session)
    if last is None:
        return None
    return last + interval_ms()


def snapshot(db, session, now, *, activity, unanswered_incoming, proactive_group_ids, stored_after_ms):
    """Conversation-only due signal. Never shortens life next_decision_ms."""
    after = effective_after_ms(db, session, stored_after_ms)
    result = {'due': False, 'after_ms': after, 'interval_ms': interval_ms(), 'status': 'idle'}
    if after is None:
        result['status'] = 'awaiting_outbound'
        return result
    if activity is None or activity['attention'] == 'unavailable':
        result['status'] = 'waiting_for_availability'
        return result
    if unanswered_incoming:
        result['status'] = 'waiting_for_incoming'
        return result
    if proactive_group_ids:
        result['status'] = 'proactive_active'
        return result
    if now < after:
        result['status'] = 'waiting_for_interval'
        return result
    result.update(due=True, status='due')
    return result


def advance(db, session, now):
    """Set next check to max(stored, now + interval). Controller-owned bookkeeping."""
    nxt = now + interval_ms()
    db.execute('''UPDATE sessions SET proactive_check_after_ms=CASE
        WHEN proactive_check_after_ms IS NULL OR proactive_check_after_ms < ? THEN ?
        ELSE proactive_check_after_ms END WHERE id=?''', (nxt, nxt, session))
