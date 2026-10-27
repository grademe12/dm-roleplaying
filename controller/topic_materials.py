"""Verified material cache and bounded search claims. Search itself stays in Muse.

No provider credentials, HTTP, timers or extra decision wakeups are introduced.
"""
import hashlib
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from common import decode, encode, integer, require, text, file_lock
import conversation_flow as flow

SQL = (
    '''CREATE TABLE IF NOT EXISTS topic_search_runs (
        session_id TEXT NOT NULL REFERENCES sessions(id), id TEXT NOT NULL,
        token TEXT NOT NULL, status TEXT NOT NULL, day_key TEXT NOT NULL,
        started_ms INTEGER NOT NULL, expires_ms INTEGER NOT NULL, finished_ms INTEGER,
        result_digest TEXT, material_ids_json TEXT NOT NULL,
        PRIMARY KEY(session_id,id))''',
    '''CREATE INDEX IF NOT EXISTS topic_search_day ON topic_search_runs(session_id,day_key,started_ms)''',
)


def initialize(db):
    for statement in SQL:
        db.execute(statement)


def normalized_url(value):
    parsed = urlsplit(text(value))
    require(parsed.scheme in ('http', 'https') and bool(parsed.hostname)
            and parsed.username is None and parsed.password is None, 'topic_invalid_source_url')
    # Keep semantic query parameters (article IDs), remove only known tracking.
    query = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True)
             if not k.casefold().startswith('utm_') and k.casefold() not in ('fbclid', 'gclid')]
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path or '/',
                       urlencode(sorted(query)), ''))


def validate_news_facts(rows, checked, refresh, cfg):
    for row in rows:
        d = decode(row['details_json'])
        require(row['source'] == 'observed' and d.get('body_verified') is True,
                'topic_source_body_unverified')
        normalized_url(d.get('url'))
        text(d.get('publisher'), 'topic_publisher')
        require(integer(d.get('checked_ms'), 'checked_ms') == checked, 'topic_check_time_mismatch')
        require('published_ms' in d and 'event_ms' in d, 'topic_source_times_required')
        for name in ('published_ms', 'updated_ms'):
            if d.get(name) is not None:
                require(integer(d[name], name) <= checked, 'topic_source_future')
        if d.get('event_ms') is not None:
            event = integer(d['event_ms'], 'event_ms')
            require(event <= checked or d.get('event_status') == 'scheduled', 'topic_event_future')
        require(isinstance(d.get('developing', False), bool), 'topic_developing_flag_invalid')
        require(not d.get('developing') or refresh <= checked+cfg['developing_fresh_ms'],
                'topic_developing_refresh_required')
        flow.strings(d.get('point_keys', []))


def search_state(db, session, now):
    day = datetime.fromtimestamp(now/1000, timezone(timedelta(hours=9))).date().isoformat()
    used = db.execute('SELECT count(*) FROM topic_search_runs WHERE session_id=? AND day_key=?',
                      (session, day)).fetchone()[0]
    latest = db.execute('SELECT max(started_ms) FROM topic_search_runs WHERE session_id=?', (session,)).fetchone()[0]
    import topic_producer
    path=db.execute('PRAGMA database_list').fetchone()[2]
    produced,producer_latest,_=topic_producer.usage(path,session,day)
    latest=max((t for t in (latest,producer_latest) if t is not None),default=None)
    return day, used+produced, latest


def claim(store, session, now):
    # The legacy and independent collectors reserve one shared daily budget.
    with file_lock(store.path,'topic_search_budget'):
        return _legacy_claim(store,session,now)


def _legacy_claim(store, session, now):
    with store.connect(True, operation='topic_search_claim', session=session) as db:
        state = db.execute('SELECT revision FROM sessions WHERE id=?', (session,)).fetchone()
        require(state is not None, 'session_not_found')
        cfg = flow.policy(db, session)
        require(cfg['search_enabled'] and cfg['search_tool_verified'], 'topic_search_disabled')
        require(db.execute("SELECT 1 FROM dm_messages WHERE session_id=? AND status IN ('received','deferred','queued')",
                           (session,)).fetchone() is None, 'topic_search_incoming_pending')
        require(db.execute("SELECT 1 FROM dm_jobs WHERE session_id=? AND status IN ('pending','attempting','submitted','unknown')",
                           (session,)).fetchone() is None, 'topic_search_dispatch_pending')
        activity = db.execute("SELECT attention,planned_end_ms FROM activities WHERE session_id=? AND status='in_progress'",
                              (session,)).fetchone()
        require(activity is not None and activity[0] in ('free', 'light') and activity[1] > now,
                'topic_search_activity_unavailable')
        require(db.execute('SELECT 1 FROM conversation_leases WHERE session_id=? AND until_ms>?',
                           (session, now)).fetchone() is None, 'topic_search_conversation_active')
        require(db.execute("SELECT 1 FROM topic_search_runs WHERE session_id=? AND status='started' AND expires_ms>?",
                           (session, now)).fetchone() is None, 'topic_search_already_active')
        import topic_producer
        require(not topic_producer.active(store.path,session,now),'topic_search_already_active')
        day, used, latest = search_state(db, session, now)
        require(used < cfg['search_daily_limit'], 'topic_search_daily_budget')
        require(latest is None or latest+cfg['search_interval_ms'] <= now, 'topic_search_cooldown')
        db.execute("UPDATE topic_search_runs SET status='expired',finished_ms=? WHERE session_id=? AND status='started' AND expires_ms<=?",
                   (now, session, now))
        rid, token = 'search-'+uuid.uuid4().hex, uuid.uuid4().hex
        deadline = now+cfg['search_limit_ms']
        db.execute('INSERT INTO topic_search_runs VALUES (?,?,?,?,?,?,?,?,?,?)',
                   (session, rid, token, 'started', day, now, deadline, None, None, '[]'))
        db.execute('UPDATE sessions SET revision=revision+1 WHERE id=?', (session,))
        return {'ok': True, 'reason': 'topic_search_claimed', 'revision': state[0]+1,
                'run_id': rid, 'token': token, 'deadline_ms': deadline,
                'max_materials': cfg['search_max_materials'], 'attempts_today': used+1,
                'search_execution': 'Muse tool; this claim does not execute or cancel the tool'}


def result(store, session, doc, now):
    flow.obj(doc, {'run_id', 'token', 'status', 'facts', 'materials'}, {'run_id', 'token', 'status'})
    require(doc['status'] in ('success', 'no_candidates', 'failed', 'timeout'), 'topic_search_result_invalid')
    digest = hashlib.sha256(encode(doc).encode()).hexdigest()
    with store.connect(True, operation='topic_search_result', session=session) as db:
        run = db.execute('SELECT * FROM topic_search_runs WHERE session_id=? AND id=?',
                         (session, doc['run_id'])).fetchone()
        require(run is not None and run['token'] == doc['token'], 'topic_search_token_invalid')
        if run['result_digest'] is not None:
            require(run['result_digest'] == digest, 'topic_search_result_collision')
            return {'ok': True, 'reason': 'topic_search_result_already_recorded',
                    'revision': db.execute('SELECT revision FROM sessions WHERE id=?', (session,)).fetchone()[0],
                    'material_ids': decode(run['material_ids_json'])}
        require(run['status'] == 'started' and now <= run['expires_ms'], 'topic_search_result_expired')
        cfg = flow.policy(db, session)
        facts, materials = doc.get('facts', []), doc.get('materials', [])
        require(isinstance(facts, list) and len(facts) <= 30 and isinstance(materials, list), 'topic_search_result_invalid')
        if doc['status'] == 'success':
            require(0 < len(materials) <= cfg['search_max_materials'], 'topic_search_material_limit')
        else:
            require(not facts and not materials, 'topic_search_failure_has_materials')
        for fact in facts:
            require(isinstance(fact, dict) and fact.get('source') == 'observed', 'topic_news_requires_observed')
            checked = fact.get('details', {}).get('checked_ms')
            require(type(checked) is int and run['started_ms'] <= checked <= now, 'topic_check_time_mismatch')
            store._fact(db, session, fact, now)
        mids = []
        for material in materials:
            require(isinstance(material, dict) and material.get('kind') == 'news', 'topic_search_news_only')
            checked = material.get('checked_ms', now)
            require(type(checked) is int and run['started_ms'] <= checked <= now, 'topic_check_time_mismatch')
            mids.append(flow.register_material(db, session, material, now))
        db.execute('UPDATE topic_search_runs SET status=?,finished_ms=?,result_digest=?,material_ids_json=? '
                   'WHERE session_id=? AND id=?',
                   (doc['status'], now, digest, encode(sorted(set(mids))), session, run['id']))
        db.execute('UPDATE sessions SET revision=revision+1 WHERE id=?', (session,))
        revision = db.execute('SELECT revision FROM sessions WHERE id=?', (session,)).fetchone()[0]
        return {'ok': True, 'reason': 'topic_search_recorded', 'revision': revision,
                'status': doc['status'], 'material_ids': sorted(set(mids))}


def materials(store, session, now, offset=0, limit=20):
    integer(offset, 'offset'); integer(limit, 'limit', 1)
    require(limit <= 100, 'limit_too_large')
    with store.connect() as db:
        state = db.execute('SELECT revision FROM sessions WHERE id=?', (session,)).fetchone()
        require(state is not None, 'session_not_found')
        cfg = flow.policy(db, session)
        rows = db.execute('SELECT * FROM conversation_materials WHERE session_id=? ORDER BY checked_ms DESC,id LIMIT ? OFFSET ?',
                          (session, limit+1, offset)).fetchall()
        output = []
        for row in rows[:limit]:
            item = dict(row)
            item['fact_ids'] = decode(item.pop('fact_ids_json'))
            item['details'] = decode(item.pop('details_json'))
            item['source_notes'] = [{'id': f['id'], 'source': f['source'], 'summary': f['summary'],
                                     'details': decode(f['details_json'])}
                                    for f in flow.fact_rows(db, session, item['fact_ids'])]
            item['units'] = []
            for unit in db.execute('SELECT * FROM conversation_units WHERE session_id=? AND material_id=? ORDER BY id',
                                   (session, row['id'])):
                conflicts = flow.conflicts_for(db, session, unit, {'mode': 'introduce'}, '', now, cfg)
                item['units'].append({'unit_id': unit['id'], 'summary': unit['summary'],
                                      'eligible_for_introduction': not conflicts, 'excluded': conflicts})
            item['needs_refresh'] = row['kind'] == 'news' and row['refresh_after_ms'] <= now
            output.append(item)
        # Cache diagnostics must not hide already-published conversation facts.
        unavailable=False
        try:
            day, used, latest = search_state(db, session, now)
        except (OSError,ValueError,sqlite3.Error):
            day=datetime.fromtimestamp(now/1000,timezone(timedelta(hours=9))).date().isoformat()
            used,latest,unavailable=None,None,True
        return {'ok': True, 'reason': 'topic_materials', 'revision': state[0], 'materials': output,
                'has_more': len(rows) > limit, 'next_offset': offset+limit if len(rows) > limit else None,
                'search': {'enabled': cfg['search_enabled'], 'diagnostics_unavailable':unavailable, 'day_key': day, 'attempts_today': used,
                           'daily_limit': cfg['search_daily_limit'], 'next_search_ms':
                           None if latest is None else latest+cfg['search_interval_ms']}}
