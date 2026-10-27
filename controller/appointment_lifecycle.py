"""Existing appointment boundaries; preparation times are explicitly supplied."""
from common import decode, integer, require
import in_person


def milestones(appointment):
    details = decode(appointment['details_json'])
    result = []
    for phase in ('prepare','depart'):
        at = details.get(phase+'_at_ms')
        if at is not None:
            integer(at,'appointment_'+phase+'_at_ms')
            require(at <= appointment['start_ms'],'invalid_appointment_milestone')
            result.append((phase,at))
    require(len(result) < 2 or result[0][1] <= result[1][1],'invalid_appointment_milestone_order')
    return result


def pending(db, session, appointment, activity):
    if activity is not None and activity['location'] == appointment['location']:
        return []
    rows = db.execute("SELECT * FROM activities WHERE session_id=? AND status IN ('in_progress','completed','interrupted')",
                      (session,))
    prepared = False
    for row in rows:
        if not in_person.linked(row,appointment['id']):
            continue
        details = decode(row['details_json'])
        prepared = prepared or details.get('appointment_phase') == 'prepare'
        if row['status'] == 'in_progress' and row['location'] == 'in_transit' and row['destination'] == appointment['location']:
            return []
    return [(phase,at) for phase,at in milestones(appointment) if not (phase == 'prepare' and prepared)]


def view(db, session, now, activity):
    scene = in_person.snapshot(db,session,now)
    rows = list(db.execute("SELECT * FROM appointments WHERE session_id=? AND status='confirmed' ORDER BY start_ms,id",(session,)))
    reviews, preparations, future, due = [], [], [], False
    for a in rows:
        details = decode(a['details_json'])
        review_at = max(a['end_ms'],details.get('review_at_ms',a['end_ms']))
        if scene['active'] and scene['appointment_id'] == a['id']:
            review_at = max(review_at,scene['review_at_ms'])
        if now >= review_at:
            reviews.append(a['id'])
        elif a['end_ms'] <= now:
            future.append(review_at)
        if a['start_ms'] > now:
            future.append(a['start_ms'])
            for phase,at in pending(db,session,a,activity):
                if at <= now:
                    preparations.append({'appointment_id':a['id'],'phase':phase,'at_ms':at})
                else:
                    future.append(at)
        elif a['end_ms'] > now:
            # Linked travel to the meeting point counts as serving; its planned end raises activity_due.
            serving = in_person.linked(activity,a['id']) and (activity['location'] == a['location'] or
                      in_person.approaching(activity,a) or
                      scene['active'] and scene['appointment_id'] == a['id'])
            due = due or not serving
            future.append(review_at)
    return {'review_ids':reviews,'preparations':preparations,'appointment_due':bool(due),
            'next_boundary_ms':min(future) if future else None}
