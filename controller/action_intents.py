"""Model-selected personal intentions. No text extraction, scheduler, or outbound sends."""
from common import decode, encode, integer, require, text

DAY_MS = 86400000
MAX_TTL_MS = 7 * DAY_MS
OPEN = ('pending', 'in_progress')
DDL = (
    '''CREATE TABLE IF NOT EXISTS action_intents (
       session_id TEXT NOT NULL REFERENCES sessions(id), id TEXT NOT NULL,
       target_key TEXT NOT NULL, summary TEXT NOT NULL,
       status TEXT NOT NULL CHECK(status IN ('pending','in_progress','completed','cancelled','expired')),
       earliest_ms INTEGER NOT NULL, review_at_ms INTEGER NOT NULL, expires_ms INTEGER NOT NULL,
       created_ms INTEGER NOT NULL, updated_ms INTEGER NOT NULL, decision_id TEXT NOT NULL,
       condition_text TEXT, self_chosen INTEGER NOT NULL CHECK(self_chosen IN (0,1)),
       supersedes_id TEXT, followup_text TEXT, PRIMARY KEY(session_id,id),
       FOREIGN KEY(session_id,supersedes_id) REFERENCES action_intents(session_id,id),
       CHECK(expires_ms>earliest_ms), CHECK(review_at_ms>=earliest_ms))''',
    '''CREATE UNIQUE INDEX IF NOT EXISTS one_open_action_target
       ON action_intents(session_id,target_key) WHERE status IN ('pending','in_progress')''',
    '''CREATE TABLE IF NOT EXISTS action_intent_sources (
       session_id TEXT NOT NULL, intent_id TEXT NOT NULL,
       kind TEXT NOT NULL CHECK(kind IN ('message','job','shared_result_job')), source_id TEXT NOT NULL,
       decision_id TEXT NOT NULL, created_ms INTEGER NOT NULL,
       PRIMARY KEY(session_id,intent_id,kind,source_id),
       FOREIGN KEY(session_id,intent_id) REFERENCES action_intents(session_id,id))''',
    '''CREATE TABLE IF NOT EXISTS action_intent_activities (
       session_id TEXT NOT NULL, intent_id TEXT NOT NULL, activity_id TEXT NOT NULL,
       PRIMARY KEY(session_id,activity_id),
       FOREIGN KEY(session_id,intent_id) REFERENCES action_intents(session_id,id),
       FOREIGN KEY(session_id,activity_id) REFERENCES activities(session_id,id))''',
    '''CREATE TABLE IF NOT EXISTS action_intent_events (
       sequence INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
       intent_id TEXT NOT NULL, decision_id TEXT NOT NULL, at_ms INTEGER NOT NULL,
       action TEXT NOT NULL, reason TEXT NOT NULL, detail_json TEXT NOT NULL CHECK(json_valid(detail_json)),
       FOREIGN KEY(session_id,intent_id) REFERENCES action_intents(session_id,id))''',
    '''CREATE INDEX IF NOT EXISTS action_intent_activity_lookup
       ON action_intent_activities(session_id,intent_id)''',
    '''CREATE INDEX IF NOT EXISTS action_intent_event_lookup
       ON action_intent_events(session_id,intent_id,sequence)''',
)


def initialize(db):
    # Do not executescript here: it would implicitly commit the caller's transaction.
    for sql in DDL:
        db.execute(sql)


def active(db, session):
    return db.execute("SELECT * FROM activities WHERE session_id=? AND status='in_progress'",
                      (session,)).fetchone()


def effective_due(row, activity, now):
    due = min(row['review_at_ms'], row['expires_ms'])
    if activity and activity['attention'] == 'unavailable' and activity['planned_end_ms'] > now:
        due = max(due, activity['planned_end_ms'])
    return due


def next_due(db, session, now):
    activity = active(db, session)
    times = [effective_due(r, activity, now) for r in db.execute(
        "SELECT * FROM action_intents WHERE session_id=? AND status='pending'", (session,))]
    return min(times) if times else None


def _sources(db, session, iid, source, decision_id, now, shared=False):
    require(isinstance(source, dict), 'invalid_intent_sources')
    keys = {'job_ids'} if shared else {'message_ids', 'job_ids'}
    require(set(source) <= keys, 'unknown_intent_source_field')
    count = 0
    for field, table, kind in (('message_ids', 'dm_messages', 'message'),
                               ('job_ids', 'dm_jobs', 'shared_result_job' if shared else 'job')):
        ids = source.get(field, [])
        require(isinstance(ids, list) and len(ids) <= 32, 'invalid_intent_source_ids')
        for sid in ids:
            text(sid, 'intent_source_id')
            row = db.execute(f'SELECT * FROM {table} WHERE session_id=? AND id=?', (session, sid)).fetchone()
            require(row is not None, 'intent_source_not_found')
            if table == 'dm_jobs':
                if shared:
                    completed_at = db.execute("SELECT max(at_ms) FROM action_intent_events WHERE session_id=? AND intent_id=? AND action='complete'",
                                              (session,iid)).fetchone()[0]
                    require(completed_at is not None and row['created_ms'] >= completed_at,
                            'intent_result_job_predates_completion')
            db.execute('''INSERT OR IGNORE INTO action_intent_sources
                VALUES (?,?,?,?,?,?)''', (session, iid, kind, sid, decision_id, now))
            count += 1
    return count


def _event(db, session, iid, decision_id, now, op):
    db.execute('''INSERT INTO action_intent_events
        (session_id,intent_id,decision_id,at_ms,action,reason,detail_json) VALUES (?,?,?,?,?,?,?)''',
        (session, iid, decision_id, now, op['action'], op['reason'], encode(op)))


def _result(db, session, row, op, decision):
    """Require fresh simulated experience tied to the latest ended execution segment."""
    activity = db.execute('''SELECT a.* FROM action_intent_activities l JOIN activities a
        ON a.session_id=l.session_id AND a.id=l.activity_id
        WHERE l.session_id=? AND l.intent_id=? ORDER BY a.start_ms DESC,a.id DESC LIMIT 1''',
        (session, row['id'])).fetchone()
    require(activity is not None and activity['status'] in ('completed', 'interrupted'),
            'intent_activity_not_ended')
    ids = op.get('result_fact_ids')
    require(isinstance(ids, list) and 0 < len(ids) <= 32, 'intent_result_required')
    fresh = {f['id']: f for f in decision.get('facts', [])}
    for fid in ids:
        text(fid, 'intent_result_fact_id')
        fact = fresh.get(fid)
        require(fact is not None and fact.get('source') == 'simulated', 'intent_result_not_fresh_simulated')
        details = fact.get('details', {})
        require(details.get('activity_id') == activity['id'] and details.get('action_intent_id') == row['id'],
                'intent_result_link_mismatch')
        occurred = fact.get('occurred_ms')
        integer(occurred, 'intent_result_time')
        require(activity['start_ms'] <= occurred <= activity['actual_end_ms'], 'intent_result_time_mismatch')
        require(details.get('intent_outcome') == ('completed' if op['action'] == 'complete' else 'partial'),
                'intent_result_outcome_mismatch')
        # _fact's general idempotence only compares summary/source. Check the stored full evidence here.
        stored = db.execute('SELECT * FROM facts WHERE session_id=? AND id=?', (session, fid)).fetchone()
        require(stored is not None and stored['recorded_ms'] == decision['_now_ms']
                and decode(stored['details_json']) == details and stored['occurred_ms'] == occurred,
                'intent_result_identity_collision')
        require(fid not in decision['_previous_fact_ids'], 'intent_result_reused')
    if op['action'] == 'complete':
        require(activity['status'] == 'completed', 'intent_completion_requires_completed_activity')
    return activity


def apply(db, session, decision, now, old_activity, previous_fact_ids):
    operations = decision.get('action_intents', [])
    require(isinstance(operations, list) and len(operations) <= 64, 'invalid_action_intents')
    # Decisions use tick.now_ms; CLI persistence naturally happens later. Keep simulation
    # time separate from write time, as Store._activity and Store.apply already do.
    recorded_now = now
    now = decision['advance_to_ms']
    context = {**decision, '_now_ms': recorded_now, '_previous_fact_ids': previous_fact_ids}
    for op in operations:
        require(isinstance(op, dict), 'invalid_intent_operation')
        action = op.get('action')
        allowed = {
            'create': {'target_key','summary','earliest_ms','review_at_ms','expires_ms','condition_text',
                       'self_chosen','sources','supersedes_id','followup_text'},
            'link_sources': {'sources'}, 'share_result': {'sources'},
            'start': {'activity_id'}, 'defer': {'review_at_ms','result_fact_ids'},
            'complete': {'result_fact_ids'}, 'cancel': {'result_fact_ids'}, 'expire': set(),
        }
        require(action in allowed, 'invalid_intent_action')
        require(set(op) <= allowed[action] | {'action','id','reason'}, 'unknown_intent_field')
        iid = text(op.get('id'), 'intent_id')
        text(op.get('reason'), 'intent_reason')
        row = db.execute('SELECT * FROM action_intents WHERE session_id=? AND id=?', (session, iid)).fetchone()
        if action == 'create':
            require(row is None, 'intent_identity_collision')
            key = text(op.get('target_key'), 'intent_target_key')
            summary = text(op.get('summary'), 'intent_summary')
            chosen = op.get('self_chosen')
            require(chosen is True, 'intent_requires_self_choice')
            earliest = integer(op.get('earliest_ms', now), 'intent_earliest_ms')
            review = integer(op.get('review_at_ms'), 'intent_review_at_ms')
            expires = integer(op.get('expires_ms', now + DAY_MS), 'intent_expires_ms')
            require(now <= earliest <= review < expires <= now + MAX_TTL_MS and review > now,
                    'invalid_intent_window')
            condition = op.get('condition_text')
            if condition is not None:
                text(condition, 'intent_condition')
            followup = op.get('followup_text')
            if followup is not None:
                text(followup, 'intent_followup')
            supersedes = op.get('supersedes_id')
            if supersedes is not None:
                text(supersedes, 'supersedes_id')
                previous = db.execute('SELECT status FROM action_intents WHERE session_id=? AND id=?',
                                      (session, supersedes)).fetchone()
                require(previous is not None and previous['status'] in ('cancelled','expired'),
                        'intent_supersedes_not_closed')
            require(db.execute("SELECT 1 FROM action_intents WHERE session_id=? AND target_key=? AND status IN ('pending','in_progress')",
                               (session,key)).fetchone() is None, 'intent_target_already_open')
            db.execute('INSERT INTO action_intents VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       (session,iid,key,summary,'pending',earliest,review,expires,recorded_now,recorded_now,
                        decision['decision_id'],condition,1,supersedes,followup))
            _sources(db,session,iid,op.get('sources',{}),decision['decision_id'],recorded_now)
        else:
            require(row is not None, 'intent_not_found')
            status = row['status']
            if action == 'link_sources':
                require(status in OPEN, 'intent_terminal')
                require(_sources(db,session,iid,op.get('sources',{}),decision['decision_id'],recorded_now) > 0,
                        'intent_sources_required')
            elif action == 'share_result':
                require(status == 'completed', 'intent_not_completed')
                require(_sources(db,session,iid,op.get('sources',{}),decision['decision_id'],recorded_now,shared=True) > 0,
                        'intent_sources_required')
            elif action == 'start':
                require(status == 'pending', 'intent_not_pending')
                require(row['earliest_ms'] <= now < row['expires_ms'], 'intent_not_executable')
                require(not old_activity or old_activity['attention'] != 'unavailable'
                        or old_activity['planned_end_ms'] <= now, 'intent_cannot_interrupt_unavailable')
                aid = text(op.get('activity_id'), 'intent_activity_id')
                a = db.execute('SELECT * FROM activities WHERE session_id=? AND id=?', (session,aid)).fetchone()
                require(a is not None and a['status'] == 'in_progress' and a['start_ms'] == now,
                        'intent_start_requires_new_current_activity')
                require(any(v.get('id') == aid for v in decision.get('activities', [])),
                        'intent_activity_not_in_decision')
                require(decode(a['details_json']).get('action_intent_id') == iid, 'intent_activity_link_mismatch')
                require(db.execute('SELECT 1 FROM action_intent_activities WHERE session_id=? AND activity_id=?',
                                   (session,aid)).fetchone() is None, 'intent_activity_already_linked')
                db.execute('INSERT INTO action_intent_activities VALUES (?,?,?)', (session,iid,aid))
                status = 'in_progress'
            elif action == 'defer':
                require(status in OPEN, 'intent_terminal')
                if status == 'in_progress':
                    _result(db,session,row,op,context)
                review = integer(op.get('review_at_ms'), 'intent_review_at_ms')
                require(max(now,row['earliest_ms']) <= review < row['expires_ms'] and review > now,
                        'invalid_intent_window')
                db.execute('UPDATE action_intents SET review_at_ms=? WHERE session_id=? AND id=?',
                           (review,session,iid))
                status = 'pending'
            elif action == 'complete':
                require(status == 'in_progress', 'intent_not_in_progress')
                _result(db,session,row,op,context)
                status = 'completed'
            elif action == 'cancel':
                require(status in OPEN, 'intent_terminal')
                if status == 'in_progress':
                    _result(db,session,row,op,context)
                status = 'cancelled'
            elif action == 'expire':
                require(status == 'pending' and row['expires_ms'] <= now, 'intent_not_expirable')
                status = 'expired'
            db.execute('UPDATE action_intents SET status=?,updated_ms=? WHERE session_id=? AND id=?',
                       (status,recorded_now,session,iid))
        _event(db,session,iid,decision['decision_id'],recorded_now,op)
    # No ended segment can leave a dangling in_progress intention, even if this apply omitted operations.
    require(db.execute('''SELECT 1 FROM action_intents i WHERE i.session_id=? AND i.status='in_progress'
        AND NOT EXISTS (SELECT 1 FROM action_intent_activities l JOIN activities a
          ON a.session_id=l.session_id AND a.id=l.activity_id
          WHERE l.session_id=i.session_id AND l.intent_id=i.id AND a.status='in_progress')''',
        (session,)).fetchone() is None, 'intent_activity_resolution_required')
    require(db.execute('''SELECT 1 FROM activities a WHERE a.session_id=? AND a.status='in_progress'
        AND json_extract(a.details_json,'$.action_intent_id') IS NOT NULL
        AND NOT EXISTS (SELECT 1 FROM action_intent_activities l JOIN action_intents i
          ON i.session_id=l.session_id AND i.id=l.intent_id
          WHERE l.session_id=a.session_id AND l.activity_id=a.id AND i.status='in_progress'
            AND l.intent_id=json_extract(a.details_json,'$.action_intent_id'))''',
        (session,)).fetchone() is None, 'intent_activity_not_registered')
    require(db.execute("SELECT count(*) FROM action_intents WHERE session_id=? AND status IN ('pending','in_progress')",
                       (session,)).fetchone()[0] <= 32, 'too_many_open_intents')
    # Existing sleeping/unavailable activities may defer processing an overdue or expired intent.
    due = next_due(db,session,recorded_now)
    require(due is None or due > recorded_now, 'intent_review_required')


def _view(db, session, row, now, activity, history=False):
    result = dict(row)
    result['self_chosen'] = bool(result['self_chosen'])
    result['effective_review_ms'] = effective_due(row,activity,now) if row['status'] == 'pending' else None
    result['expired_by_clock'] = row['status'] == 'pending' and row['expires_ms'] <= now
    result['sources'] = []
    for source in db.execute('SELECT * FROM action_intent_sources WHERE session_id=? AND intent_id=? ORDER BY kind,source_id',
                             (session,row['id'])):
        s = dict(source)
        if s['kind'] in ('job','shared_result_job'):
            job = db.execute('SELECT status,provider_message_id FROM dm_jobs WHERE session_id=? AND id=?',
                             (session,s['source_id'])).fetchone()
            # Read from the transport ledger, including partial send/unknown; never infer from a draft.
            s['delivery_status'] = job['status'] if job else 'missing'
            s['delivered'] = bool(job and job['status'] == 'confirmed' and job['provider_message_id'])
        result['sources'].append(s)
    events = [dict(r) for r in db.execute(
        'SELECT * FROM action_intent_events WHERE session_id=? AND intent_id=? ORDER BY sequence',
        (session,row['id']))]
    for event in events:
        event['detail'] = decode(event.pop('detail_json'))
    result['result_fact_ids'] = list(dict.fromkeys(fid for e in events
                                                 for fid in e['detail'].get('result_fact_ids', [])))
    delivered = {s['source_id'] for s in result['sources']
                 if s['kind'] == 'shared_result_job' and s['delivered']}
    # A share operation may span multiple parts. One confirmed part is insufficient.
    result['result_shared'] = any(e['action'] == 'share_result'
        and set(e['detail']['sources']['job_ids']) <= delivered for e in events)
    result['activity_ids'] = [r[0] for r in db.execute('''SELECT l.activity_id FROM action_intent_activities l
        JOIN activities a ON a.session_id=l.session_id AND a.id=l.activity_id
        WHERE l.session_id=? AND l.intent_id=? ORDER BY a.start_ms,a.id''', (session,row['id']))]
    if history:
        result['events'] = events
    return result


def snapshot(db, session, now):
    activity = active(db,session)
    rows = db.execute("SELECT * FROM action_intents WHERE session_id=? AND status IN ('pending','in_progress') ORDER BY review_at_ms,id",
                      (session,)).fetchall()
    return [_view(db,session,r,now,activity) for r in rows]


def inspect(db, session, iid, now):
    row = db.execute('SELECT * FROM action_intents WHERE session_id=? AND id=?', (session,text(iid,'intent_id'))).fetchone()
    require(row is not None, 'intent_not_found')
    return _view(db,session,row,now,active(db,session),history=True)


def followups(db, session, now):
    """Conversation context only: no wake reason, priority, or send authorization."""
    rows = db.execute("""SELECT * FROM action_intents
        WHERE session_id=? AND status='completed' AND followup_text IS NOT NULL AND updated_ms>=?
        ORDER BY updated_ms DESC,id LIMIT 100""", (session,now-7*DAY_MS)).fetchall()
    views = (_view(db,session,r,now,active(db,session)) for r in rows)
    result = []
    for view in views:
        if not view['result_shared']:
            result.append(view)
            if len(result) >= 20:
                break
    return result
