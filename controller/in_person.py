"""Explicit shared-scene state. A clock/appointment alone never creates a meeting."""
from common import decode, encode, integer, require, text

SQL = (
    '''CREATE TABLE IF NOT EXISTS in_person_state (
        session_id TEXT PRIMARY KEY REFERENCES sessions(id), appointment_id TEXT NOT NULL,
        channel TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('active','ended')),
        started_ms INTEGER NOT NULL, ended_ms INTEGER, review_at_ms INTEGER NOT NULL,
        reason TEXT NOT NULL, evidence_json TEXT NOT NULL)''',
    '''CREATE TABLE IF NOT EXISTS group_scene_context (
        session_id TEXT NOT NULL, group_id TEXT NOT NULL, scene_key TEXT NOT NULL,
        PRIMARY KEY(session_id,group_id),
        FOREIGN KEY(session_id,group_id) REFERENCES reply_groups(session_id,id))''',
)


def active(db, session):
    return db.execute("SELECT * FROM in_person_state WHERE session_id=? AND status='active'",(session,)).fetchone()


def linked(activity, appointment_id):
    if activity is None:
        return False
    details = (decode(activity['details_json']) if 'details_json' in activity.keys()
               else activity.get('details',{}))
    return details.get('appointment_id') == appointment_id


def snapshot(db, session, now):
    row = active(db,session)
    if row is None:
        result = {'mode':'remote','active':False,'available':False}
        activity = db.execute("SELECT * FROM activities WHERE session_id=? AND status='in_progress'",(session,)).fetchone()
        if activity is not None:
            aid = decode(activity['details_json']).get('appointment_id')
            appointment = db.execute("SELECT * FROM appointments WHERE session_id=? AND id=? AND status='confirmed'",
                                     (session,aid)).fetchone()
            if (appointment is not None and activity['location'] == appointment['location']
                    and activity['location'] != 'in_transit'
                    and decode(appointment['details_json']).get('with_user') is not False):
                result['candidate_appointment_id'] = aid
        return result
    appointment = db.execute('SELECT * FROM appointments WHERE session_id=? AND id=?',
                             (session,row['appointment_id'])).fetchone()
    activity = db.execute("SELECT * FROM activities WHERE session_id=? AND status='in_progress'",(session,)).fetchone()
    available = (linked(activity,row['appointment_id']) and activity['attention'] != 'unavailable'
                 and activity['category'] not in ('sleep','수면'))
    review = max(row['review_at_ms'],appointment['end_ms'])
    return {'mode':'in_person','active':True,'available':bool(available),
            'appointment_id':row['appointment_id'],'channel':row['channel'],'started_ms':row['started_ms'],
            'location':activity['location'] if activity is not None else None,
            'destination':activity['destination'] if activity is not None else None,
            'activity_linked':linked(activity,row['appointment_id']),
            'planned_end_ms':appointment['end_ms'],'review_at_ms':review,
            'review_due':now >= review,'evidence_message_ids':decode(row['evidence_json'])}


def live(db, session, now, channel=None):
    scene = snapshot(db,session,now)
    return scene['active'] and scene['available'] and (channel is None or scene['channel'] == channel)


def scene_key(db, session, channel):
    row = db.execute('SELECT * FROM in_person_state WHERE session_id=?',(session,)).fetchone()
    if row is not None and row['channel'] == channel:
        if row['status'] == 'active':
            return f"in_person:{row['appointment_id']}:{row['started_ms']}:{channel}"
        return f"remote:{channel}:after:{row['appointment_id']}:{row['started_ms']}:{row['ended_ms']}"
    return 'remote:'+channel


def capture_group(db, session, group_id, channel):
    db.execute('INSERT INTO group_scene_context VALUES (?,?,?) ON CONFLICT(session_id,group_id) '
               'DO UPDATE SET scene_key=excluded.scene_key',(session,group_id,scene_key(db,session,channel)))


def group_current(db, session, group_id, channel):
    row = db.execute('SELECT scene_key FROM group_scene_context WHERE session_id=? AND group_id=?',
                     (session,group_id)).fetchone()
    # Old drafts were remote; they must be reviewed across the first scene transition.
    return (row['scene_key'] if row else 'remote:'+channel) == scene_key(db,session,channel)


def stale_groups(db, session):
    rows = db.execute('''SELECT DISTINCT g.id,COALESCE(c.channel,'instagram') AS channel
        FROM reply_groups g JOIN dm_jobs j ON j.session_id=g.session_id AND j.group_id=g.id
        LEFT JOIN group_channels c ON c.session_id=g.session_id AND c.group_id=g.id
        WHERE g.session_id=? AND g.status='active' AND j.status='pending'
        AND NOT EXISTS(SELECT 1 FROM dm_jobs b WHERE b.session_id=g.session_id AND b.group_id=g.id
                       AND b.status='attempting')''',(session,))
    return [r['id'] for r in rows if not group_current(db,session,r['id'],r['channel'])]


def approaching(activity, appointment):
    """Linked travel still heading to the appointment's own location (e.g. running late)."""
    return (activity is not None and linked(activity,appointment['id']) and activity['location'] == 'in_transit'
            and activity['destination'] == appointment['location'])


def mobile_activity(db, session, activity, appointment):
    row = active(db,session)
    return (row is not None and row['appointment_id'] == appointment['id']
            and activity['start_ms'] >= row['started_ms'] and linked(activity,appointment['id']))


def apply_ops(db, session, operations, at_ms):
    require(isinstance(operations,list) and len(operations) <= 2,'invalid_in_person_ops')
    for op in operations:
        require(isinstance(op,dict),'invalid_in_person_ops')
        action = op.get('action')
        current = active(db,session)
        reason = text(op.get('reason'),'in_person_reason')
        if action == 'start':
            require(set(op) <= {'action','appointment_id','channel','evidence_message_ids','reason'},'invalid_in_person_ops')
            require(current is None,'in_person_already_active')
            aid = text(op.get('appointment_id'),'appointment_id')
            appointment = db.execute("SELECT * FROM appointments WHERE session_id=? AND id=? AND status='confirmed'",
                                     (session,aid)).fetchone()
            require(appointment is not None,'in_person_appointment_not_confirmed')
            channel = op.get('channel','slack')
            require(channel in ('slack','instagram'),'invalid_channel')
            ids = op.get('evidence_message_ids')
            require(isinstance(ids,list) and bool(ids) and all(isinstance(i,str) for i in ids)
                    and len(ids) == len(set(ids)),'in_person_evidence_required')
            for mid in ids:
                msg = db.execute('SELECT * FROM dm_messages WHERE session_id=? AND id=?',(session,mid)).fetchone()
                info = decode(msg['observation_json']) if msg else {}
                require(msg is not None and msg['observed_ms'] <= at_ms
                        and info.get('direction') == 'incoming' and not info.get('review_required')
                        and info.get('channel','instagram') == channel,'invalid_in_person_evidence')
            activity = db.execute("SELECT * FROM activities WHERE session_id=? AND status='in_progress'",(session,)).fetchone()
            require(linked(activity,aid) and activity['location'] == appointment['location']
                    and activity['start_ms'] <= at_ms < activity['planned_end_ms']
                    and activity['location'] != 'in_transit','in_person_arrival_required')
            db.execute('''INSERT INTO in_person_state VALUES (?,?,?,'active',?,NULL,?,?,?)
                ON CONFLICT(session_id) DO UPDATE SET appointment_id=excluded.appointment_id,channel=excluded.channel,
                status='active',started_ms=excluded.started_ms,ended_ms=NULL,review_at_ms=excluded.review_at_ms,
                reason=excluded.reason,evidence_json=excluded.evidence_json''',
                (session,aid,channel,at_ms,appointment['end_ms'],reason,encode(ids)))
        elif action in ('end','review_later'):
            allowed = {'action','reason'} | ({'review_at_ms'} if action == 'review_later' else set())
            require(set(op) <= allowed,'invalid_in_person_ops')
            require(current is not None,'in_person_not_active')
            if action == 'review_later':
                review = integer(op.get('review_at_ms'),'in_person_review_at_ms')
                require(review > at_ms,'in_person_review_not_future')
                db.execute('UPDATE in_person_state SET review_at_ms=?,reason=? WHERE session_id=?',(review,reason,session))
            else:
                db.execute("UPDATE in_person_state SET status='ended',ended_ms=?,reason=? WHERE session_id=?",
                           (at_ms,reason,session))
                db.execute("UPDATE appointments SET status='completed' WHERE session_id=? AND id=?",
                           (session,current['appointment_id']))
        else:
            require(False,'invalid_in_person_action')


def validate(db, session):
    row = active(db,session)
    if row:
        appointment = db.execute('SELECT status FROM appointments WHERE session_id=? AND id=?',
                                 (session,row['appointment_id'])).fetchone()
        require(appointment is not None and appointment['status'] == 'confirmed','in_person_close_required')
