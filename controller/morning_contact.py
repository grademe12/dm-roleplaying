"""Daily proactive greeting tied to a recorded main sleep, never to wall-clock polling alone."""
from datetime import datetime, timedelta, timezone

from common import decode, require
import persona

KST = timezone(timedelta(hours=9))
HOUR = 3_600_000
DESTINATIONS = frozenset(('office', 'work', 'company', 'school', 'university', 'campus',
                          'studio', '회사', '학교', '스튜디오'))
DAY_ACTIVITIES = frozenset(('work', 'office', 'class', 'lecture', 'part_time',
                            '업무', '근무', '수업'))
SQL = '''CREATE TABLE IF NOT EXISTS morning_contacts (
    session_id TEXT NOT NULL REFERENCES sessions(id), local_date TEXT NOT NULL,
    wake_activity_id TEXT NOT NULL, group_id TEXT NOT NULL, expires_ms INTEGER NOT NULL,
    created_ms INTEGER NOT NULL, PRIMARY KEY(session_id,local_date),
    UNIQUE(session_id,group_id),
    FOREIGN KEY(session_id,group_id) REFERENCES reply_groups(session_id,id))'''


def local_day(now):
    return datetime.fromtimestamp(now / 1000, KST).date().isoformat()


def _main_sleep(row, end):
    details = decode(row['details_json'])
    kind = details.get('sleep_kind')
    if kind == 'nap':
        return False
    if row['category'] not in ('sleep', '수면', '취침'):
        return False
    start = datetime.fromtimestamp(row['start_ms'] / 1000, KST)
    finish = datetime.fromtimestamp(end / 1000, KST)
    if end <= row['start_ms']:
        return False
    # A completed main sleep can end at any hour, including an early departure.
    # An ambiguous overnight notification interruption is not a day-start signal.
    if row['status'] == 'interrupted' and finish.hour < 5 and not details.get('wake_for_day'):
        return False
    return (kind == 'main' or end - row['start_ms'] >= 3 * HOUR and
            (start.date() < finish.date() or start.hour < 7))


def window(db, session, now):
    midnight = int(datetime.fromtimestamp(now / 1000, KST).replace(
        hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)
    sleeps = db.execute('''SELECT * FROM activities WHERE session_id=?
        AND status IN ('completed','interrupted') AND actual_end_ms>=? AND actual_end_ms<=?
        ORDER BY actual_end_ms DESC,id DESC''', (session, midnight, now)).fetchall()
    sleep = next((r for r in sleeps if _main_sleep(r, r['actual_end_ms'])), None)
    if sleep is None:
        active = db.execute("SELECT * FROM activities WHERE session_id=? AND status='in_progress'",
                            (session,)).fetchone()
        waking = (active is not None and midnight <= active['planned_end_ms'] <= now
                  and _main_sleep(active, active['planned_end_ms']))
        return {'local_date': local_day(now), 'status': 'wake_pending' if waking else 'no_recorded_wake',
                'wake_activity_id': active['id'] if waking else None, 'expires_ms': None}
    wake = sleep['actual_end_ms']
    # The recorded day plan sets the deadline; three hours is not a hard cutoff.
    # With no known boundary, keep today's contact eligible until the local day ends.
    boundaries = [midnight + 24 * HOUR]
    for activity in db.execute("SELECT * FROM activities WHERE session_id=? AND status<>'cancelled' AND start_ms>=?",
                               (session, wake)):
        if activity['category'] in DAY_ACTIVITIES:
            boundaries.append(activity['start_ms'])
        if (activity['location'] == 'in_transit' and activity['destination'] in DESTINATIONS
                and activity['actual_end_ms'] is not None):
            boundaries.append(activity['actual_end_ms'])
    for appointment in db.execute("SELECT * FROM appointments WHERE session_id=? AND status='confirmed' AND start_ms>=?",
                                  (session, wake)):
        if appointment['location'] in DESTINATIONS:
            boundaries.append(appointment['start_ms'])
    expires = min(boundaries)
    return {'local_date': local_day(now), 'wake_activity_id': sleep['id'], 'wake_ms': wake,
            'expires_ms': expires, 'status': 'expired' if now >= expires else 'required'}


def _jobs(db, session, gid):
    return db.execute('SELECT * FROM dm_jobs WHERE session_id=? AND group_id=? ORDER BY part_index',
                      (session, gid)).fetchall()


def _retryable(jobs):
    return bool(jobs) and all(j['status'] in ('failed', 'cancelled') and
        j['provider_message_id'] is None and
        (decode(j['result_json']).get('not_sent') is True or
         j['status'] == 'cancelled' and j['attempt_started_ms'] is None
         and j['provider_message_id'] is None) for j in jobs)


def _prior_contact(db, session):
    return db.execute('SELECT 1 FROM dm_messages WHERE session_id=? LIMIT 1', (session,)).fetchone() is not None


def _blocking_groups(db, session, own_group=''):
    groups = db.execute("""SELECT g.id FROM reply_groups g WHERE session_id=? AND status='active' AND id<>?
        AND NOT EXISTS (SELECT 1 FROM reply_targets t WHERE t.session_id=g.session_id AND t.group_id=g.id)
        ORDER BY g.id""", (session, own_group)).fetchall()
    return [{'group_id':g['id'], 'statuses':sorted({j['status'] for j in _jobs(db,session,g['id'])})}
            for g in groups]


def retire_failed_groups(db, session, dm, now):
    """A valid morning draft may retire proven-unsent failures, never a live draft/request."""
    if dm.get('routine') != 'morning_contact':
        return []
    retired = []
    has_runs = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='continuation_groups'").fetchone()
    for group in _blocking_groups(db,session):
        gid = group['group_id']
        if has_runs and db.execute('SELECT 1 FROM continuation_groups WHERE session_id=? AND group_id=?',
                                   (session,gid)).fetchone():
            continue
        if not _retryable(_jobs(db,session,gid)):
            continue
        db.execute("UPDATE dm_jobs SET status='cancelled',updated_ms=? WHERE session_id=? AND group_id=? AND status='failed'",
                   (now,session,gid))
        db.execute("UPDATE reply_groups SET status='cancelled' WHERE session_id=? AND id=?",(session,gid))
        retired.append(gid)
    return retired


def snapshot(db, session, now, require_prior_contact=False):
    result = window(db, session, now)
    result.update(due=False, required=result['status'] in ('required', 'wake_pending'),
                  group_id=None, delivery_status=None, review_groups=[])
    if not persona.MORNING_CONTACT:
        result.update(status='disabled', required=False)
        return result
    if require_prior_contact and result['required'] and not _prior_contact(db, session):
        result.update(status='awaiting_first_contact', required=False)
        return result
    registered = db.execute('SELECT * FROM morning_contacts WHERE session_id=? AND local_date=?',
                            (session, result['local_date'])).fetchone()
    if registered is not None:
        jobs = _jobs(db, session, registered['group_id'])
        statuses = {j['status'] for j in jobs}
        result['group_id'] = registered['group_id']
        if 'confirmed' in statuses:
            result.update(status='confirmed', required=False,
                          delivery_status='confirmed' if statuses == {'confirmed'} else 'partial')
            return result
        if statuses & {'submitted', 'unknown', 'attempting'}:
            result.update(status='awaiting_receipt', required=False,
                          delivery_status='sending' if 'attempting' in statuses else 'uncertain')
            return result
        # Today's actual plan supersedes an earlier stored/fallback deadline.
        if result['status'] != 'required':
            return result
        if 'pending' in statuses:
            result.update(status='queued', delivery_status='pending')
            return result
        if not _retryable(jobs):
            result.update(status='awaiting_receipt', required=False, delivery_status='uncertain')
            return result
        result['delivery_status'] = 'not_sent'
    if result['status'] != 'required':
        return result
    activity = db.execute("SELECT attention FROM activities WHERE session_id=? AND status='in_progress'",
                          (session,)).fetchone()
    if activity is None or activity['attention'] == 'unavailable':
        result['status'] = 'waiting_for_availability'
    elif db.execute("SELECT 1 FROM dm_messages WHERE session_id=? AND status IN ('received','deferred','queued')",
                    (session,)).fetchone():
        result['status'] = 'waiting_for_incoming'
    else:
        groups = _blocking_groups(db,session,registered['group_id'] if registered else '')
        if groups:
            result.update(status='review_existing_group',review_groups=groups)
        result['due'] = True
    return result


def validate_tag(dm):
    if 'routine' not in dm:
        return
    require(persona.MORNING_CONTACT, 'morning_contact_disabled')
    require(dm['action'] == 'initiate' and dm['routine'] == 'morning_contact'
            and not dm.get('continuation'), 'invalid_morning_contact')
    require(isinstance(dm.get('parts'), list) and bool(dm['parts'])
            and all(isinstance(p,str) for p in dm['parts']), 'morning_contact_requires_text_parts')


def attach(db, session, dm, now, require_prior_contact=False):
    if 'routine' not in dm:
        return
    validate_tag(dm)
    require(not require_prior_contact or _prior_contact(db, session), 'morning_contact_first_contact_required')
    current = window(db, session, now)
    require(current['status'] == 'required', 'morning_contact_outside_window')
    existing = db.execute('SELECT * FROM morning_contacts WHERE session_id=? AND local_date=?',
                          (session, current['local_date'])).fetchone()
    if existing:
        require(_retryable(_jobs(db, session, existing['group_id'])), 'morning_contact_already_registered')
    jobs = _jobs(db, session, dm['group_id'])
    require(bool(jobs) and all(now <= j['due_ms'] < current['expires_ms'] for j in jobs),
            'morning_contact_due_outside_window')
    expires = current['expires_ms']
    db.execute('''INSERT INTO morning_contacts VALUES (?,?,?,?,?,?)
        ON CONFLICT(session_id,local_date) DO UPDATE SET group_id=excluded.group_id,
        expires_ms=excluded.expires_ms''', (session, current['local_date'], current['wake_activity_id'],
                                         dm['group_id'], expires, now))


def expire_pending(db, session, now):
    """Cancel only unclaimed drafts. Expiry never holds unrelated conversation."""
    expired = []
    rows = db.execute('''SELECT m.* FROM morning_contacts m JOIN reply_groups g
        ON g.session_id=m.session_id AND g.id=m.group_id WHERE m.session_id=? AND g.status='active'
        AND EXISTS (SELECT 1 FROM dm_jobs j WHERE j.session_id=m.session_id AND j.group_id=m.group_id
                    AND j.status IN ('pending','failed'))''', (session,)).fetchall()
    if not rows:
        return expired
    current = window(db, session, now)
    for row in rows:
        expires = row['expires_ms']
        if row['local_date'] == current['local_date'] and current['expires_ms'] is not None:
            expires = current['expires_ms']
        if now < expires:
            continue
        db.execute("UPDATE dm_jobs SET status='cancelled',updated_ms=? WHERE session_id=? AND group_id=? AND status IN ('pending','failed')",
                   (now, session, row['group_id']))
        statuses = {j['status'] for j in _jobs(db,session,row['group_id'])}
        if 'attempting' not in statuses:
            terminal = 'completed' if statuses & {'confirmed','submitted','unknown'} else 'cancelled'
            db.execute('UPDATE reply_groups SET status=? WHERE session_id=? AND id=?',
                       (terminal,session,row['group_id']))
        expired.append(row['group_id'])
    return expired
