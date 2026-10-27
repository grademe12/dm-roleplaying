"""Conversation identity, transactional repetition guards and delivery-backed uses.

No network or language model. Semantic classification remains the decision worker's
job; this module never treats matching annotations as proof of semantic correctness.
"""
import hashlib
import re
import unicodedata

from common import ControlError, decode, encode, integer, require, text

HOUR = 3600000
MAX_SEARCH_LIMIT_MS = 180000
DEFAULTS = {'mode': 'off', 'reintroduction_ms': 2*HOUR, 'same_material_ms': 24*HOUR,
            'search_enabled': False, 'search_tool_verified': False,
            'search_daily_limit': 2, 'search_interval_ms': 6*HOUR,
            'search_limit_ms': MAX_SEARCH_LIMIT_MS, 'search_max_materials': 3,
            'material_fresh_ms': 24*HOUR, 'developing_fresh_ms': HOUR}

SQL = (
    '''CREATE TABLE IF NOT EXISTS conversation_policy (
        session_id TEXT PRIMARY KEY REFERENCES sessions(id), settings_json TEXT NOT NULL,
        updated_ms INTEGER NOT NULL)''',
    '''CREATE TABLE IF NOT EXISTS conversation_materials (
        session_id TEXT NOT NULL REFERENCES sessions(id), id TEXT NOT NULL,
        canonical_key TEXT NOT NULL, subject_key TEXT NOT NULL, kind TEXT NOT NULL,
        fact_ids_json TEXT NOT NULL, checked_ms INTEGER NOT NULL, refresh_after_ms INTEGER NOT NULL,
        details_json TEXT NOT NULL, PRIMARY KEY(session_id,id), UNIQUE(session_id,canonical_key))''',
    '''CREATE TABLE IF NOT EXISTS conversation_aliases (
        session_id TEXT NOT NULL, alias_key TEXT NOT NULL, material_id TEXT NOT NULL,
        PRIMARY KEY(session_id,alias_key), FOREIGN KEY(session_id,material_id)
        REFERENCES conversation_materials(session_id,id))''',
    '''CREATE TABLE IF NOT EXISTS conversation_units (
        session_id TEXT NOT NULL REFERENCES sessions(id), id TEXT NOT NULL,
        canonical_key TEXT NOT NULL, material_id TEXT, subject_key TEXT NOT NULL,
        kind TEXT NOT NULL CHECK(kind IN ('statement','question')), facet_key TEXT NOT NULL,
        scope_key TEXT NOT NULL, summary TEXT NOT NULL, fact_ids_json TEXT NOT NULL,
        point_keys_json TEXT NOT NULL, PRIMARY KEY(session_id,id), UNIQUE(session_id,canonical_key),
        FOREIGN KEY(session_id,material_id) REFERENCES conversation_materials(session_id,id))''',
    '''CREATE TABLE IF NOT EXISTS conversation_groups (
        session_id TEXT NOT NULL, group_id TEXT NOT NULL, mode TEXT NOT NULL,
        plan_json TEXT, warnings_json TEXT NOT NULL, created_ms INTEGER NOT NULL,
        PRIMARY KEY(session_id,group_id), FOREIGN KEY(session_id,group_id)
        REFERENCES reply_groups(session_id,id))''',
    '''CREATE TABLE IF NOT EXISTS conversation_uses (
        session_id TEXT NOT NULL, id TEXT NOT NULL, unit_id TEXT NOT NULL,
        group_id TEXT NOT NULL, mode TEXT NOT NULL, spans_json TEXT NOT NULL,
        job_ids_json TEXT NOT NULL, status TEXT NOT NULL, first_confirmed_ms INTEGER,
        confirmed_ms INTEGER, created_ms INTEGER NOT NULL, updated_ms INTEGER NOT NULL,
        PRIMARY KEY(session_id,id), UNIQUE(session_id,group_id,unit_id),
        FOREIGN KEY(session_id,unit_id) REFERENCES conversation_units(session_id,id),
        FOREIGN KEY(session_id,group_id) REFERENCES reply_groups(session_id,id))''',
    '''CREATE INDEX IF NOT EXISTS conversation_use_unit_time
        ON conversation_uses(session_id,unit_id,created_ms)''',
    '''CREATE INDEX IF NOT EXISTS conversation_use_group ON conversation_uses(session_id,group_id)''',
    '''CREATE TABLE IF NOT EXISTS conversation_use_jobs (
        session_id TEXT NOT NULL, use_id TEXT NOT NULL, job_id TEXT NOT NULL,
        PRIMARY KEY(session_id,use_id,job_id), FOREIGN KEY(session_id,use_id)
        REFERENCES conversation_uses(session_id,id) ON DELETE CASCADE,
        FOREIGN KEY(session_id,job_id) REFERENCES dm_jobs(session_id,id))''',
    '''CREATE TABLE IF NOT EXISTS conversation_evidence (
        session_id TEXT NOT NULL, id TEXT NOT NULL, unit_id TEXT NOT NULL, use_id TEXT NOT NULL,
        kind TEXT NOT NULL CHECK(kind IN ('answer','close','shift','reopen')),
        refs_json TEXT NOT NULL, observed_ms INTEGER NOT NULL, recorded_ms INTEGER NOT NULL,
        PRIMARY KEY(session_id,id), FOREIGN KEY(session_id,unit_id)
        REFERENCES conversation_units(session_id,id), FOREIGN KEY(session_id,use_id)
        REFERENCES conversation_uses(session_id,id))''',
    '''CREATE INDEX IF NOT EXISTS conversation_evidence_unit
        ON conversation_evidence(session_id,unit_id,observed_ms)''',
)


def initialize(db):
    for statement in SQL:
        db.execute(statement)


def obj(value, allowed, required=()):
    require(isinstance(value, dict) and set(required) <= set(value) <= set(allowed),
            'invalid_conversation_document')
    return value


def key(value):
    result = re.sub(r'\s+', ' ', unicodedata.normalize('NFKC', text(value))).strip().casefold()
    require(len(result) <= 500, 'conversation_key_too_long')
    return result


def identity(prefix, value):
    return prefix+hashlib.sha256(encode(value).encode()).hexdigest()[:32]


def strings(value):
    require(isinstance(value, list) and len(value) <= 100, 'invalid_conversation_list')
    require(all(isinstance(v, str) and v.strip() for v in value) and len(set(value)) == len(value),
            'invalid_conversation_list')
    return value


def policy(db, session):
    row = db.execute('SELECT settings_json FROM conversation_policy WHERE session_id=?', (session,)).fetchone()
    return {**DEFAULTS, **decode(row[0])} if row else dict(DEFAULTS)


def configure(store, session, values, now):
    obj(values, DEFAULTS)
    with store.connect(True, operation='conversation_configure', session=session) as db:
        require(db.execute('SELECT 1 FROM sessions WHERE id=?', (session,)).fetchone() is not None,
                'session_not_found')
        cfg = {**policy(db, session), **values}
        require(cfg['mode'] in ('off', 'shadow', 'enforce'), 'invalid_conversation_mode')
        for name in ('search_enabled', 'search_tool_verified'):
            require(type(cfg[name]) is bool, 'invalid_conversation_flag')
        for name in set(DEFAULTS)-{'mode', 'search_enabled', 'search_tool_verified'}:
            integer(cfg[name], name, 1)
        require(cfg['search_daily_limit'] <= 10 and cfg['search_max_materials'] <= 10
                and cfg['search_limit_ms'] <= MAX_SEARCH_LIMIT_MS, 'invalid_conversation_budget')
        require(not cfg['search_enabled'] or cfg['search_tool_verified'], 'topic_search_tool_unverified')
        db.execute('INSERT INTO conversation_policy VALUES (?,?,?) ON CONFLICT(session_id) '
                   'DO UPDATE SET settings_json=excluded.settings_json,updated_ms=excluded.updated_ms',
                   (session, encode(cfg), now))
        db.execute('UPDATE sessions SET revision=revision+1 WHERE id=?', (session,))
        revision = db.execute('SELECT revision FROM sessions WHERE id=?', (session,)).fetchone()[0]
        return {'ok': True, 'reason': 'conversation_configured', 'revision': revision, 'settings': cfg}


def fact_rows(db, session, ids):
    strings(ids)
    require(bool(ids), 'conversation_fact_required')
    rows = []
    for fid in ids:
        row = db.execute('SELECT * FROM facts WHERE session_id=? AND id=?', (session, fid)).fetchone()
        require(row is not None and row['source'] != 'proposed', 'conversation_fact_invalid')
        details = decode(row['details_json'])
        require(isinstance(details, dict), 'conversation_fact_details_invalid')
        if 'point_keys' in details:
            strings(details['point_keys'])
        rows.append(row)
    return rows


def resolve(value, refs):
    value = text(value)
    if value.startswith('@'):
        require(value[1:] in refs, 'conversation_reference_missing')
        return refs[value[1:]]
    return value


def alias_key(value):
    value = text(value)
    if value.startswith('url:'):
        from topic_materials import normalized_url
        return 'url:'+normalized_url(value[4:])
    return key(value)


def material_for_alias(db, session, alias):
    row = db.execute('SELECT material_id FROM conversation_aliases WHERE session_id=? AND alias_key=?',
                     (session, alias_key(alias))).fetchone()
    return row[0] if row else None


def register_material(db, session, op, now):
    obj(op, {'op', 'ref', 'canonical_key', 'subject_key', 'kind', 'fact_ids', 'aliases',
             'checked_ms', 'refresh_after_ms', 'details'},
        {'canonical_key', 'subject_key', 'kind', 'fact_ids'})
    canonical = key(op['canonical_key'])
    subject = key(op['subject_key'])
    kind = op['kind']
    require(kind in ('news', 'experience', 'general'), 'invalid_conversation_material_kind')
    rows = fact_rows(db, session, op['fact_ids'])
    aliases = {canonical, *(alias_key(v) for v in strings(op.get('aliases', [])))}
    # Facts, like normalized source URLs, are stable identities even if a worker
    # proposes a different title/canonical key for the same information.
    aliases.update('fact:'+row['id'] for row in rows)
    if kind == 'news':
        from topic_materials import normalized_url
        aliases.update('url:'+normalized_url(decode(row['details_json']).get('url')) for row in rows)
    ids = {material_for_alias(db, session, alias) for alias in aliases} - {None}
    require(len(ids) <= 1, 'conversation_alias_conflict')
    mid = next(iter(ids)) if ids else identity('cm-', canonical)
    existing = db.execute('SELECT * FROM conversation_materials WHERE session_id=? AND id=?',
                          (session, mid)).fetchone()
    if existing:
        require(existing['subject_key'] == subject and existing['kind'] == kind,
                'conversation_material_identity_conflict')
    checked = integer(op.get('checked_ms', now), 'checked_ms')
    require(checked <= now, 'conversation_future_check')
    cfg = policy(db, session)
    refresh = integer(op.get('refresh_after_ms', checked+cfg['material_fresh_ms']), 'refresh_after_ms')
    require(checked < refresh <= checked+cfg['material_fresh_ms'], 'conversation_invalid_freshness')
    details = op.get('details', {})
    require(isinstance(details, dict), 'invalid_conversation_material_details')
    if kind == 'news':
        require(all(row['source'] == 'observed' for row in rows), 'topic_news_requires_observed')
        from topic_materials import validate_news_facts
        validate_news_facts(rows, checked, refresh, cfg)
    if existing:
        combined = sorted(set(decode(existing['fact_ids_json'])) | set(op['fact_ids']))
        # An old registration must not regress a more recently verified note.
        if checked >= existing['checked_ms']:
            db.execute('UPDATE conversation_materials SET fact_ids_json=?,checked_ms=?,refresh_after_ms=?,details_json=? '
                       'WHERE session_id=? AND id=?',
                       (encode(combined), checked, refresh, encode(details), session, mid))
    else:
        db.execute('INSERT INTO conversation_materials VALUES (?,?,?,?,?,?,?,?,?)',
                   (session, mid, canonical, subject, kind, encode(sorted(op['fact_ids'])),
                    checked, refresh, encode(details)))
    for alias in sorted(aliases):
        old = material_for_alias(db, session, alias)
        require(old in (None, mid), 'conversation_alias_conflict')
        db.execute('INSERT OR IGNORE INTO conversation_aliases VALUES (?,?,?)', (session, alias_key(alias), mid))
    return mid


def register_unit(db, session, op, refs):
    obj(op, {'op', 'ref', 'material_id', 'subject_key', 'kind', 'facet_key', 'scope_key',
             'summary', 'fact_ids', 'point_keys'},
        {'subject_key', 'kind', 'facet_key', 'scope_key', 'summary', 'fact_ids'})
    rows = fact_rows(db, session, op['fact_ids'])
    material = resolve(op['material_id'], refs) if op.get('material_id') else None
    subject = key(op['subject_key'])
    if material:
        row = db.execute('SELECT * FROM conversation_materials WHERE session_id=? AND id=?',
                         (session, material)).fetchone()
        require(row is not None and row['subject_key'] == subject, 'conversation_material_invalid')
        require(set(op['fact_ids']) <= set(decode(row['fact_ids_json'])), 'conversation_material_fact_mismatch')
        require(row['kind'] != 'news' or op.get('point_keys'), 'conversation_news_points_required')
    require(op['kind'] in ('statement', 'question'), 'conversation_unit_invalid')
    facet, scope = key(op['facet_key']), key(op['scope_key'])
    points = sorted(key(v) for v in strings(op.get('point_keys', [])))
    known = {key(p) for row in rows for p in decode(row['details_json']).get('point_keys', [])}
    require(set(points) <= known, 'conversation_point_not_in_fact')
    if material and points and op['kind'] == 'statement':
        for previous in db.execute("SELECT * FROM conversation_units WHERE session_id=? AND material_id=? AND kind='statement'",
                                   (session, material)):
            prior_points = set(decode(previous['point_keys_json']))
            if prior_points & set(points):
                require(prior_points == set(points), 'conversation_point_overlap')
                return previous['id']
    # Question identity is independent of article/material to prevent asking
    # the same question again under a newly registered piece of material.
    canonical = encode([subject, op['kind'], facet, scope,
                        material if op['kind'] == 'statement' else None])
    uid = identity('cu-', canonical)
    old = db.execute('SELECT * FROM conversation_units WHERE session_id=? AND id=?', (session, uid)).fetchone()
    if old:
        require(set(points) == set(decode(old['point_keys_json'])), 'conversation_unit_identity_conflict')
        return uid
    db.execute('INSERT INTO conversation_units VALUES (?,?,?,?,?,?,?,?,?,?,?)',
               (session, uid, canonical, material, subject, op['kind'], facet, scope,
                text(op['summary']), encode(sorted(op['fact_ids'])), encode(points)))
    return uid


def incoming_ref(db, session, ref, target_ids=None):
    obj(ref, {'message_id', 'excerpt'}, {'message_id', 'excerpt'})
    row = db.execute('SELECT * FROM dm_messages WHERE session_id=? AND id=?',
                     (session, text(ref['message_id']))).fetchone()
    require(row is not None and decode(row['observation_json']).get('direction') == 'incoming'
            and not decode(row['observation_json']).get('review_required'), 'conversation_request_invalid')
    require(text(ref['excerpt']) in row['text'], 'conversation_request_invalid')
    require(target_ids is None or row['id'] in target_ids, 'conversation_request_not_target')
    return row


def usage(db, session, use):
    """Derive status from the authoritative jobs, never from a model flag."""
    jobs = [db.execute('SELECT * FROM dm_jobs WHERE session_id=? AND id=?', (session, jid)).fetchone()
            for jid in decode(use['job_ids_json'])]
    require(bool(jobs) and all(jobs), 'conversation_use_jobs_missing')
    confirmed = [j for j in jobs if j['status'] == 'confirmed']
    unresolved = any(j['status'] in ('pending', 'attempting', 'submitted', 'unknown') or
                     (j['status'] == 'failed' and decode(j['result_json']).get('not_sent') is not True)
                     for j in jobs)
    status = ('confirmed' if len(confirmed) == len(jobs) else 'partial' if confirmed else
              'reserved' if unresolved else 'released')
    first = use['first_confirmed_ms']
    if confirmed and first is None:
        first = min(j['updated_ms'] for j in confirmed)
    return status, first, unresolved


def terminal_evidence(db, session, uid, since=0):
    return db.execute("SELECT * FROM conversation_evidence WHERE session_id=? AND unit_id=? "
                      "AND observed_ms>=? ORDER BY observed_ms DESC,recorded_ms DESC,id DESC LIMIT 1",
                      (session, uid, since)).fetchone()


def last_delivery(db, session, use):
    times = []
    for jid in decode(use['job_ids_json']):
        row = db.execute("SELECT updated_ms FROM dm_jobs WHERE session_id=? AND id=? AND status='confirmed'",
                         (session, jid)).fetchone()
        if row:
            times.append(row[0])
    return max(times) if times else None


def record_evidence(db, session, op, refs, now):
    obj(op, {'op', 'unit_id', 'use_id', 'kind', 'refs'}, {'unit_id', 'use_id', 'kind', 'refs'})
    uid = resolve(op['unit_id'], refs)
    use = db.execute('SELECT * FROM conversation_uses WHERE session_id=? AND id=?',
                     (session, op['use_id'])).fetchone()
    require(use is not None and use['unit_id'] == uid, 'conversation_evidence_use_invalid')
    status, first, _ = usage(db, session, use)
    require(first is not None, 'conversation_evidence_use_not_delivered')
    require(op['kind'] in ('answer', 'close', 'shift', 'reopen'), 'conversation_evidence_invalid')
    unit = db.execute('SELECT * FROM conversation_units WHERE session_id=? AND id=?', (session, uid)).fetchone()
    require(op['kind'] != 'answer' or unit['kind'] == 'question', 'conversation_answer_not_question')
    require(isinstance(op['refs'], list) and 0 < len(op['refs']) <= 20, 'conversation_evidence_required')
    times = []
    for ref in op['refs']:
        if 'message_id' in ref:
            row = incoming_ref(db, session, ref)
            require(row['observed_ms'] >= first, 'conversation_evidence_precedes_use')
            times.append(row['observed_ms'])
        else:
            obj(ref, {'job_id', 'excerpt'}, {'job_id', 'excerpt'})
            require(op['kind'] != 'answer', 'conversation_answer_requires_user')
            row = db.execute('SELECT * FROM dm_jobs WHERE session_id=? AND id=?',
                             (session, ref['job_id'])).fetchone()
            require(row is not None and row['status'] == 'confirmed' and text(ref['excerpt']) in row['text']
                    and row['updated_ms'] >= first, 'conversation_evidence_job_invalid')
            times.append(row['updated_ms'])
    observed = max(times)
    require(observed <= now, 'conversation_evidence_future')
    eid = identity('ce-', [uid, use['id'], op['kind'], op['refs']])
    db.execute('INSERT OR IGNORE INTO conversation_evidence VALUES (?,?,?,?,?,?,?,?)',
               (session, eid, uid, use['id'], op['kind'], encode(op['refs']), observed, now))
    return eid


def apply_updates(db, session, updates, now):
    require(isinstance(updates, list) and len(updates) <= 100, 'invalid_conversation_updates')
    refs = {}
    for op in updates:
        require(isinstance(op, dict), 'invalid_conversation_update')
        action = op.get('op')
        if action == 'register_material':
            value = register_material(db, session, op, now)
        elif action == 'register_unit':
            value = register_unit(db, session, op, refs)
        elif action == 'record_evidence':
            value = record_evidence(db, session, op, refs, now)
        elif action == 'link_alias':
            obj(op, {'op', 'material_id', 'alias_key', 'fact_ids', 'ref'},
                {'material_id', 'alias_key', 'fact_ids'})
            fact_rows(db, session, op['fact_ids'])
            value = resolve(op['material_id'], refs)
            require(db.execute('SELECT 1 FROM conversation_materials WHERE session_id=? AND id=?',
                               (session, value)).fetchone() is not None, 'conversation_material_invalid')
            old = material_for_alias(db, session, op['alias_key'])
            require(old in (None, value), 'conversation_alias_conflict')
            db.execute('INSERT OR IGNORE INTO conversation_aliases VALUES (?,?,?)',
                       (session, alias_key(op['alias_key']), value))
        else:
            raise ControlError('invalid_conversation_update')
        if 'ref' in op:
            name = text(op['ref'])
            require(name not in refs and not name.startswith('@'), 'conversation_reference_collision')
            refs[name] = value
    return refs


def mapped_spans(parts, spans, covered, skip):
    require(isinstance(spans, list) and 0 < len(spans) <= 100, 'conversation_plan_mismatch')
    result = []
    for span in spans:
        obj(span, {'part_index', 'excerpt', 'char_start', 'char_end'}, {'part_index', 'excerpt'})
        index = integer(span['part_index'], 'part_index')
        require(index < len(parts) and index not in skip and isinstance(parts[index], str),
                'conversation_plan_mismatch')
        body, excerpt = parts[index], text(span['excerpt'])
        if 'char_start' in span or 'char_end' in span:
            start = integer(span.get('char_start'), 'char_start')
            end = integer(span.get('char_end'), 'char_end')
            require(start < end <= len(body) and body[start:end] == excerpt, 'conversation_plan_mismatch')
        else:
            require(body.count(excerpt) == 1, 'conversation_span_ambiguous')
            start, end = body.index(excerpt), body.index(excerpt)+len(excerpt)
        covered[index].update(range(start, end))
        result.append({'part_index': index, 'excerpt': excerpt, 'char_start': start, 'char_end': end})
    return result


def basis_valid(db, session, entry, unit, targets, now):
    basis = obj(entry.get('basis', {}), {'request_refs', 'prior_use_ids', 'fact_ids', 'point_keys'})
    mode = entry['mode']
    require(mode in ('answer', 'continue', 'introduce', 'reintroduce', 'update'), 'conversation_mode_invalid')
    requests = basis.get('request_refs', [])
    require(isinstance(requests, list) and len(requests) <= 20, 'conversation_request_invalid')
    for ref in requests:
        incoming_ref(db, session, ref, targets)
    priors = []
    for uid in strings(basis.get('prior_use_ids', [])):
        use = db.execute('SELECT * FROM conversation_uses WHERE session_id=? AND id=?', (session, uid)).fetchone()
        require(use is not None, 'conversation_exception_invalid')
        old_unit = db.execute('SELECT * FROM conversation_units WHERE session_id=? AND id=?',
                              (session, use['unit_id'])).fetchone()
        require(old_unit['subject_key'] == unit['subject_key'] and usage(db, session, use)[1] is not None,
                'conversation_exception_invalid')
        priors.append(use)
    facts = strings(basis.get('fact_ids', []))
    if facts:
        fact_rows(db, session, facts)
        require(set(facts) <= set(decode(unit['fact_ids_json'])), 'conversation_exception_invalid')
    points = strings(basis.get('point_keys', []))
    require(set(points) <= set(decode(unit['point_keys_json'])), 'conversation_exception_invalid')
    if mode == 'answer':
        require(bool(requests) and not priors and not points, 'conversation_exception_invalid')
    elif mode == 'introduce':
        require(not requests and not priors, 'conversation_exception_invalid')
    elif mode == 'continue':
        require(bool(targets) and bool(priors), 'conversation_exception_invalid')
        for prior in priors:
            state = terminal_evidence(db, session, prior['unit_id'])
            require(state is None or state['kind'] not in ('answer', 'close', 'shift'),
                    'conversation_exception_invalid')
            require(now-last_delivery(db, session, prior) < policy(db, session)['reintroduction_ms'],
                    'conversation_exception_invalid')
    elif mode == 'reintroduce':
        require(bool(priors) and bool(requests or facts), 'conversation_exception_invalid')
        # Requesting a fresh context is not the same as asking for repetition.
    else:
        require(bool(priors) and bool(facts) and bool(points), 'conversation_exception_invalid')
        previous_facts, previous_points = set(), set()
        for prior in priors:
            previous = db.execute('SELECT * FROM conversation_units WHERE session_id=? AND id=?',
                                  (session, prior['unit_id'])).fetchone()
            previous_facts.update(decode(previous['fact_ids_json']))
            previous_points.update(decode(previous['point_keys_json']))
        require(set(facts).isdisjoint(previous_facts) and set(points).isdisjoint(previous_points),
                'conversation_exception_invalid')
        require(all(r['recorded_ms'] >= max(last_delivery(db, session, p) for p in priors)
                    for r in fact_rows(db, session, facts)), 'conversation_exception_invalid')
    return basis


def conflicts_for(db, session, unit, entry, group_id, now, cfg):
    conflicts = []
    # The full index, not a truncated context page, is the guard's authority.
    for old in db.execute('SELECT * FROM conversation_uses WHERE session_id=? AND unit_id=? AND group_id<>?',
                          (session, unit['id'], group_id)):
        status, first, unresolved = usage(db, session, old)
        reason, earliest = None, None
        if unresolved:
            reason = 'conversation_unit_reserved'
        elif first is not None and entry['mode'] not in ('answer', 'continue', 'update'):
            end = db.execute("SELECT max(observed_ms) FROM conversation_evidence WHERE session_id=? AND unit_id=? "
                             "AND kind IN ('close','shift')", (session, unit['id'])).fetchone()[0]
            if entry['mode'] == 'introduce':
                reason = 'conversation_material_already_shared'
            else:
                last = last_delivery(db, session, old)
                material = db.execute('SELECT kind FROM conversation_materials WHERE session_id=? AND id=?',
                                      (session, unit['material_id'])).fetchone()
                delay = cfg['same_material_ms'] if material and material[0] in ('news', 'experience') else cfg['reintroduction_ms']
                earliest = max(last+delay, (end or 0)+cfg['reintroduction_ms'])
                if now-last < delay:
                    reason = 'conversation_recent_reintroduction'
                if end is not None and now-end < cfg['reintroduction_ms']:
                    reason = 'conversation_recent_reintroduction'
        if reason:
            conflicts.append({'reason': reason, 'unit_id': unit['id'], 'use_id': old['id'],
                              'group_id': old['group_id'], 'earliest_review_ms': earliest})
    if unit['kind'] == 'question' and entry['mode'] != 'answer':
        answered = db.execute("SELECT id FROM conversation_evidence WHERE session_id=? AND unit_id=? AND kind='answer' LIMIT 1",
                               (session, unit['id'])).fetchone()
        if answered:
            conflicts.insert(0, {'reason': 'conversation_question_answered', 'unit_id': unit['id'],
                                 'evidence_id': answered[0], 'earliest_review_ms': None})
    if unit['material_id'] and entry['mode'] in ('introduce', 'reintroduce', 'update'):
        material = db.execute('SELECT * FROM conversation_materials WHERE session_id=? AND id=?',
                              (session, unit['material_id'])).fetchone()
        if material['kind'] == 'news' and material['refresh_after_ms'] <= now:
            conflicts.append({'reason': 'conversation_material_stale', 'unit_id': unit['id'],
                              'material_id': material['id'], 'earliest_review_ms': None})
        if material['kind'] == 'news':
            current_points = set()
            for fact in fact_rows(db, session, decode(material['fact_ids_json'])):
                details = decode(fact['details_json'])
                if details.get('checked_ms') == material['checked_ms']:
                    current_points.update(key(p) for p in details.get('point_keys', []))
            if not set(decode(unit['point_keys_json'])) <= current_points:
                conflicts.append({'reason': 'conversation_point_superseded', 'unit_id': unit['id'],
                                  'material_id': material['id'], 'earliest_review_ms': None})
    return conflicts


def validate_plan(db, session, dm, parts, targets, now, refs=None, revalidate=False):
    cfg = policy(db, session)
    gid = text(dm['group_id'])
    tracked = db.execute('SELECT * FROM conversation_groups WHERE session_id=? AND group_id=?',
                         (session, gid)).fetchone()
    applicable = dm.get('conversation_review', {}).get('memory', {}).get('kind') in ('general', 'hobby')
    required = applicable and cfg['mode'] == 'enforce' and (
        not revalidate or (tracked is not None and tracked['plan_json'] is not None))
    raw = dm.get('conversation_plan')
    if raw is None:
        require(not required, 'conversation_plan_required')
        if tracked and tracked['plan_json'] is not None:
            # Disabling enforcement must not destroy existing reservations.
            return None
        return {'plan': None, 'warnings': [{'reason': 'conversation_plan_required'}]
                if applicable and cfg['mode'] == 'shadow' else [], 'mode': cfg['mode']}
    obj(raw, {'version', 'units', 'non_substantive'}, {'version', 'units'})
    require(type(raw['version']) is int and raw['version'] == 1, 'conversation_plan_version')
    require(isinstance(raw['units'], list) and len(raw['units']) <= 50, 'conversation_plan_mismatch')
    covered = [set() for _ in parts]
    skip = {i for i, body in enumerate(parts) if isinstance(body, dict)}
    normalized, warnings, seen = [], [], set()
    for entry in raw['units']:
        obj(entry, {'unit_id', 'mode', 'spans', 'basis'}, {'unit_id', 'mode', 'spans', 'basis'})
        uid = resolve(entry['unit_id'], refs or {})
        require(uid not in seen, 'conversation_unit_repeated_in_plan')
        seen.add(uid)
        unit = db.execute('SELECT * FROM conversation_units WHERE session_id=? AND id=?', (session, uid)).fetchone()
        require(unit is not None, 'conversation_unit_invalid')
        spans = mapped_spans(parts, entry['spans'], covered, skip)
        basis = basis_valid(db, session, entry, unit, targets, now)
        normalized.append({'unit_id': uid, 'mode': entry['mode'], 'spans': spans, 'basis': basis})
        if cfg['mode'] != 'off':
            warnings.extend(conflicts_for(db, session, unit, entry, gid, now, cfg))
    plain = raw.get('non_substantive', [])
    require(isinstance(plain, list) and len(plain) <= 100, 'conversation_plan_mismatch')
    normalized_plain = []
    for entry in plain:
        obj(entry, {'mode', 'spans'}, {'mode', 'spans'})
        require(entry['mode'] in ('close', 'acknowledge'), 'conversation_mode_invalid')
        normalized_plain.append({'mode': entry['mode'], 'spans': mapped_spans(parts, entry['spans'], covered, skip)})
    for i, body in enumerate(parts):
        if i not in skip:
            require(all(c.isspace() or j in covered[i] for j, c in enumerate(body)), 'conversation_plan_mismatch')
    if warnings and cfg['mode'] == 'enforce':
        raise ControlError(warnings[0]['reason'], encode({'conflicts': warnings}))
    return {'plan': {'version': 1, 'units': normalized, 'non_substantive': normalized_plain},
            'warnings': warnings, 'mode': cfg['mode']}


def reserve(db, session, gid, checked, now):
    if checked is None:
        return
    plan = checked['plan']
    old_uses = list(db.execute('SELECT * FROM conversation_uses WHERE session_id=? AND group_id=?', (session, gid)))
    new_entries = {u['unit_id']: u for u in plan['units']} if plan else {}
    for old in old_uses:
        status, first, _ = usage(db, session, old)
        uncertain = db.execute('''SELECT 1 FROM dm_jobs j JOIN conversation_use_jobs u
            ON u.session_id=j.session_id AND u.job_id=j.id
            WHERE u.session_id=? AND u.use_id=? AND j.status IN ('submitted','unknown')''',
            (session,old['id'])).fetchone()
        if first is not None or uncertain:
            require(old['unit_id'] in new_entries and decode(old['spans_json']) == new_entries[old['unit_id']]['spans'],
                    'conversation_confirmed_mapping_immutable')
        elif old['unit_id'] not in new_entries:
            db.execute('DELETE FROM conversation_uses WHERE session_id=? AND id=?', (session, old['id']))
    db.execute('INSERT INTO conversation_groups VALUES (?,?,?,?,?,?) ON CONFLICT(session_id,group_id) '
               'DO UPDATE SET mode=excluded.mode,plan_json=excluded.plan_json,warnings_json=excluded.warnings_json',
               (session, gid, checked['mode'], encode(plan) if plan else None, encode(checked['warnings']), now))
    if plan:
        for entry in plan['units']:
            uid = entry['unit_id']
            jobs = []
            for index in sorted({s['part_index'] for s in entry['spans']}):
                row = db.execute('SELECT id FROM dm_jobs WHERE session_id=? AND group_id=? AND part_index=?',
                                 (session, gid, index)).fetchone()
                require(row is not None, 'conversation_use_jobs_missing')
                jobs.append(row[0])
            use_id = identity('use-', [gid, uid])
            db.execute('INSERT INTO conversation_uses VALUES (?,?,?,?,?,?,?,?,?,?,?,?) '
                       'ON CONFLICT(session_id,id) DO UPDATE SET mode=excluded.mode,spans_json=excluded.spans_json,'
                       'job_ids_json=excluded.job_ids_json,updated_ms=excluded.updated_ms',
                       (session, use_id, uid, gid, entry['mode'], encode(entry['spans']), encode(jobs),
                        'reserved', None, None, now, now))
            db.execute('DELETE FROM conversation_use_jobs WHERE session_id=? AND use_id=?', (session, use_id))
            for jid in jobs:
                db.execute('INSERT INTO conversation_use_jobs VALUES (?,?,?)', (session, use_id, jid))
    sync_group(db, session, gid, now)


def sync_group(db, session, gid, now):
    for use in list(db.execute('SELECT * FROM conversation_uses WHERE session_id=? AND group_id=?', (session, gid))):
        status, first, _ = usage(db, session, use)
        complete = use['confirmed_ms']
        if status == 'confirmed' and complete is None:
            complete = now
        db.execute('UPDATE conversation_uses SET status=?,first_confirmed_ms=?,confirmed_ms=?,updated_ms=? '
                   'WHERE session_id=? AND id=?', (status, first, complete, now, session, use['id']))


def context(store, session, now, subject=None, offset=0, limit=30):
    integer(offset, 'offset'); integer(limit, 'limit', 1)
    require(limit <= 100, 'limit_too_large')
    with store.connect() as db:
        state = db.execute('SELECT revision FROM sessions WHERE id=?', (session,)).fetchone()
        require(state is not None, 'session_not_found')
        where = 'u.session_id=?'
        params = [session]
        if subject:
            where += ' AND n.subject_key=?'; params.append(key(subject))
        else:
            # Answered questions and outstanding reservations remain visible
            # outside the recent window; guards themselves always query all uses.
            where += " AND (u.created_ms>=? OR u.status IN ('reserved','partial') OR EXISTS " \
                     "(SELECT 1 FROM conversation_evidence e WHERE e.session_id=u.session_id AND e.unit_id=u.unit_id AND e.kind='answer'))"
            params.append(now-24*HOUR)
        rows = db.execute('SELECT u.*,n.subject_key,n.kind,n.summary FROM conversation_uses u '
                          'JOIN conversation_units n ON n.session_id=u.session_id AND n.id=u.unit_id '
                          'WHERE '+where+' ORDER BY u.created_ms DESC,u.id LIMIT ? OFFSET ?',
                          (*params, limit+1, offset)).fetchall()
        unit_rows = db.execute('SELECT * FROM conversation_units WHERE session_id=?'+
                               (' AND subject_key=?' if subject else '')+
                               ' ORDER BY subject_key,scope_key,kind,facet_key,id LIMIT ? OFFSET ?',
                               (session, *([key(subject)] if subject else []), limit+1, offset)).fetchall()
        units = []
        for row in unit_rows[:limit]:
            item = dict(row)
            for name in ('fact_ids', 'point_keys'):
                item[name] = decode(item.pop(name+'_json'))
            units.append(item)
        uses = []
        for row in rows[:limit]:
            use = dict(row)
            use['status'], use['first_confirmed_ms'], use['unresolved'] = usage(db, session, row)
            for name in ('spans', 'job_ids'):
                use[name] = decode(use.pop(name+'_json'))
            use['evidence'] = [dict(e) for e in db.execute('SELECT * FROM conversation_evidence WHERE session_id=? AND unit_id=? '
                                                        'ORDER BY observed_ms DESC,id', (session, row['unit_id']))]
            for evidence in use['evidence']:
                evidence['refs'] = decode(evidence.pop('refs_json'))
            uses.append(use)
        return {'ok': True, 'reason': 'conversation_context', 'revision': state[0], 'settings': policy(db, session),
                'uses': uses, 'units': units, 'unit_has_more': len(unit_rows) > limit,
                'next_unit_offset': offset+limit if len(unit_rows) > limit else None,
                'has_more': len(rows) > limit,
                'next_offset': offset+limit if len(rows) > limit else None,
                'window_start_ms': None if subject else now-24*HOUR,
                'semantic_classification_verified': False}
