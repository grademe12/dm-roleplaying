"""Evidence-linked background memories in a private sidecar, never controller writes.

Only the organizer writes here. Reply preparation performs bounded read-only queries.
Summaries describe evidence, not permissions, verified physical acts or instructions.
"""
from contextlib import contextmanager
import hashlib
import math
import re
import sqlite3
from pathlib import Path
import uuid

import memory_origins as origins

from common import ControlError, decode, encode, file_lock, integer, require, text, now_ms

SCHEMA = 'yujin-memory-organizer-v1'
CATEGORIES = ('shared_experience', 'appointment', 'relationship', 'hobby', 'daily', 'preference', 'identity', 'other')
SQL = '''
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sources(seq INTEGER PRIMARY KEY AUTOINCREMENT,
 canonical TEXT UNIQUE NOT NULL, channel TEXT NOT NULL, direction TEXT NOT NULL,
 source_id TEXT NOT NULL, occurred_ms INTEGER NOT NULL, captured_ms INTEGER NOT NULL, text TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS aliases(kind TEXT NOT NULL,id TEXT NOT NULL,seq INTEGER NOT NULL REFERENCES sources(seq),PRIMARY KEY(kind,id));
CREATE TABLE IF NOT EXISTS positions(channel TEXT PRIMARY KEY,seq INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS memories(id TEXT PRIMARY KEY,key TEXT NOT NULL,channel TEXT NOT NULL,
 kind TEXT NOT NULL,category TEXT NOT NULL,status TEXT NOT NULL,importance INTEGER NOT NULL,
 summary TEXT NOT NULL,keywords TEXT NOT NULL,evidence TEXT NOT NULL,interpretation TEXT NOT NULL,
 occurred_ms INTEGER NOT NULL,recorded_ms INTEGER NOT NULL,active INTEGER NOT NULL,
 supersedes TEXT,search_text TEXT NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS active_memory_key ON memories(channel,key) WHERE active=1;
CREATE INDEX IF NOT EXISTS memory_channel ON memories(channel,active,recorded_ms);
CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY,channel TEXT NOT NULL,through_seq INTEGER NOT NULL,
 deadline_ms INTEGER NOT NULL,status TEXT NOT NULL,packet TEXT NOT NULL,digest TEXT NOT NULL,
 response_digest TEXT,result TEXT);
CREATE TABLE IF NOT EXISTS failures(seq INTEGER PRIMARY KEY REFERENCES sources(seq),
 attempts INTEGER NOT NULL,batch_limit INTEGER NOT NULL,required_keys TEXT NOT NULL,
 reason TEXT NOT NULL,updated_ms INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS quarantined(seq INTEGER PRIMARY KEY REFERENCES sources(seq),
 reason TEXT NOT NULL,status TEXT NOT NULL,updated_ms INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS organizer_retries(channel TEXT PRIMARY KEY,through_seq INTEGER NOT NULL,
 batch_limit INTEGER NOT NULL,attempts INTEGER NOT NULL,not_before_ms INTEGER NOT NULL,reason TEXT NOT NULL);
''' + origins.SQL


def settings(cfg):
    raw = cfg.get('episodic_memory')
    if raw is None: return {'mode': 'off'}
    require(isinstance(raw, dict), 'invalid_episodic_settings')
    mode = raw.get('mode', 'off')
    require(mode in ('off', 'shadow', 'enforce'), 'invalid_episodic_mode')
    if mode == 'off': return {'mode': 'off'}
    value = {'mode': 'off', 'batch_size': 48, 'batch_bytes': 49152, 'lease_seconds': 600,
             'max_items': 6, 'context_bytes': 8192, 'bootstrap_hours': 24,
             # Two organizer intervals (300s) plus a minute of margin before reporting a stale capture.
             'capture_stale_seconds': 660,
             **raw}
    require(value['mode'] in ('off', 'shadow', 'enforce'), 'invalid_episodic_mode')
    for name, lo, hi in (('batch_size', 1, 100), ('batch_bytes', 4096, 262144),
                         ('lease_seconds', 30, 1800), ('max_items', 1, 12),
                         ('context_bytes', 1024, 32768), ('bootstrap_hours', 1, 720),
                         ('capture_stale_seconds', 30, 3600)):
        integer(value[name], name, lo)
        require(value[name] <= hi, 'invalid_' + name)
    state = Path(cfg.get('state_dir', Path(cfg['db']).parent / 'automation-state')).expanduser().resolve()
    value['path'] = Path(value.get('path', state / 'episodic-memory.sqlite3')).expanduser().resolve()
    require(value['path'] != Path(cfg['db']).expanduser().resolve(), 'memory_source_path_collision')
    value['binding'] = {'source': str(Path(cfg['db']).expanduser().resolve()),
                        'session': cfg.get('session', 'main'), 'persona': cfg.get('persona_id', 'yujin')}
    require(isinstance(value['binding']['persona'], str) and re.fullmatch(r'[a-z][a-z0-9-]{0,31}', value['binding']['persona']) is not None, 'invalid_memory_persona')
    return value


def reply_settings(cfg):
    """Optional memory configuration cannot make the core reply path unavailable."""
    try:
        return settings(cfg)
    except (ControlError, OSError, TypeError, ValueError, KeyError) as exc:
        return {'mode': 'off', 'diagnostic': exc.reason if isinstance(exc, ControlError)
                else 'invalid_episodic_settings'}


def generator_context(snapshot):
    return {k: snapshot[k] for k in ('mode', 'context_only', 'items', 'has_more', 'incomplete') if k in snapshot}


# Only attributed output validation failures split/isolate a batch. Unknown worker
# outcomes, network failures, expiry and contention never quarantine source data.
CONTENT_FAILURES = {'invalid_memory_response', 'invalid_memory_schema', 'memory_model_incomplete',
    'invalid_memory_operations', 'invalid_memory_item', 'duplicate_memory_key', 'invalid_memory_kind',
    'invalid_working_key', 'invalid_memory_status', 'invalid_importance', 'invalid_memory_keywords',
    'memory_version_changed', 'memory_previous_not_read', 'memory_kind_changed',
    'memory_retraction_missing_previous', 'invalid_memory_evidence', 'memory_evidence_not_read',
    'duplicate_memory_evidence', 'memory_evidence_mismatch', 'memory_new_evidence_required',
    'memory_response_too_large', 'invalid_key', 'invalid_summary', 'invalid_interpretation',
    'invalid_keyword', 'invalid_excerpt', 'nonfinite_number', 'memory_input_mismatch',
    'memory_run_not_found','invalid_memory_origin_refs','memory_origin_not_read',
    'memory_origin_key_mismatch','memory_author_episode_required'}


def content_failure(reason):
    return reason in CONTENT_FAILURES or (reason.startswith('memory_') and reason.endswith('_too_long'))


@contextmanager
def connection(cfg, write=False):
    opts = settings(cfg)
    require(opts['mode'] != 'off', 'episodic_memory_off')
    path = opts['path']
    if write:
        require(opts['mode'] != 'off', 'episodic_memory_off')
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    db = sqlite3.connect(path.as_uri() + ('?mode=rwc' if write else '?mode=ro'), uri=True,
                         isolation_level=None, timeout=.25)
    db.row_factory = sqlite3.Row
    try:
        db.execute('PRAGMA foreign_keys=ON')
        if write:
            path.chmod(0o600)
            db.executescript(SQL)
            db.execute('BEGIN IMMEDIATE')
            db.execute('INSERT OR IGNORE INTO meta VALUES (?,?)', ('binding', encode(opts['binding'])))
            db.execute("INSERT OR IGNORE INTO meta VALUES ('schema_version','1')")
        if not write: db.execute('BEGIN')
        require(db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == '1', 'incompatible_memory_database')
        require(decode(db.execute("SELECT value FROM meta WHERE key='binding'").fetchone()[0]) == opts['binding'],
                'memory_binding_mismatch')
        if not write:
            source = sqlite3.connect(Path(opts['binding']['source']).as_uri()+'?mode=ro',uri=True,timeout=.25)
            try:
                row = source.execute('SELECT sender,recipient FROM sessions WHERE id=?',
                                     (opts['binding']['session'],)).fetchone()
                peer = db.execute("SELECT value FROM meta WHERE key='peer'").fetchone()
                require(row is not None and peer is not None
                        and decode(peer[0]) == dict(zip(('sender','recipient'),row)), 'memory_peer_changed')
            finally:
                source.close()
        yield db
        if write: db.commit()
    except Exception:
        if write: db.rollback()
        raise
    finally:
        db.close()


def digest(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


def rendered(row, db=None):
    result = {k: row[k] for k in ('id', 'key', 'channel', 'kind', 'category', 'status', 'importance',
                               'summary', 'interpretation', 'occurred_ms', 'recorded_ms', 'supersedes')} | {
        'keywords': decode(row['keywords']), 'evidence': decode(row['evidence']),
        'context_only': True, 'scope': 'personal' if row['channel']==origins.PERSONAL else 'private_dm'}
    if db is not None:
        linked=origins.links(db,row['id'])
        if linked:
            result['origin_refs']=linked
            result['origin_context']=origins.descriptions(db,row['id'])
    if row['channel']==origins.PERSONAL:
        result['provenance']='author_event'
        result['source_note']='설정으로 확정된 시뮬레이션 사건; 실제 DM 발언이나 상대의 수락 증거가 아님'
    return result


def capture(db, cfg, now):
    """Read source DB without Store.connect/migration and dedupe by identity, not time cursor."""
    opts = settings(cfg)
    source = sqlite3.connect(Path(opts['binding']['source']).as_uri() + '?mode=ro', uri=True, timeout=.25)
    source.row_factory = sqlite3.Row
    try:
        source.execute('BEGIN')
        session = opts['binding']['session']
        peer = source.execute('SELECT sender,recipient FROM sessions WHERE id=?', (session,)).fetchone()
        require(peer is not None, 'session_not_found')
        binding = encode(dict(peer))
        old = db.execute("SELECT value FROM meta WHERE key='peer'").fetchone()
        require(old is None or old[0] == binding, 'memory_peer_changed')
        db.execute('INSERT OR IGNORE INTO meta VALUES (?,?)', ('peer', binding))
        row = db.execute("SELECT value FROM meta WHERE key='since_ms'").fetchone()
        since = int(row[0]) if row else max(0, now - opts['bootstrap_hours'] * 3600000)
        db.execute('INSERT OR IGNORE INTO meta VALUES (?,?)', ('since_ms', str(since)))
        # ATTACH is on the read-only source connection; the sidecar attachment is read-only too.
        source.execute('ATTACH DATABASE ? AS mem', (opts['path'].as_uri() + '?mode=ro',))
        # Bound each scan. Already-seen rows are filtered in SQL, including late confirmations.
        messages = source.execute('''SELECT m.* FROM dm_messages m WHERE session_id=? AND observed_ms>=?
          AND observed_ms<=? AND json_extract(observation_json,'$.identity') IN ('platform','verified_composite')
          AND COALESCE(json_extract(observation_json,'$.channel'),'instagram') IN ('slack','instagram')
          AND NOT COALESCE(json_extract(observation_json,'$.review_required'),0)
          AND NOT EXISTS(SELECT 1 FROM mem.aliases a WHERE a.kind='message' AND a.id=m.id)
          ORDER BY observed_ms,id LIMIT 200''', (session, since, now)).fetchall()
        jobs = source.execute('''SELECT j.*,COALESCE(gc.channel,'instagram') AS channel FROM dm_jobs j
          LEFT JOIN group_channels gc ON gc.session_id=j.session_id AND gc.group_id=j.group_id
          WHERE j.session_id=? AND j.status='confirmed' AND j.updated_ms>=? AND j.updated_ms<=?
          AND COALESCE(gc.channel,'instagram') IN ('slack','instagram')
          AND NOT EXISTS(SELECT 1 FROM mem.aliases a WHERE a.kind='job' AND a.id=j.id)
          ORDER BY j.updated_ms,j.id LIMIT 200''', (session, since, now)).fetchall()
        origins.capture(db,source,session,since,now)
        records = []
        for row in messages:
            observation = decode(row['observation_json'])
            channel = observation.get('channel', 'instagram')
            direction = observation.get('direction', 'incoming')
            provider = observation.get('slack_ts') or (row['id'].rsplit(':', 1)[-1] if channel == 'slack' else row['id'])
            canonical = encode([channel, direction, provider])
            records.append((row['observed_ms'], 'message', row['id'], channel, direction, canonical, row['text']))
        for row in jobs:
            canonical = encode([row['channel'], 'outgoing', row['provider_message_id'] or 'job:' + row['id']])
            records.append((row['updated_ms'], 'job', row['id'], row['channel'], 'outgoing', canonical, row['text']))
        for occurred, kind, sid, channel, direction, canonical, body in sorted(records):
            if channel not in ('slack', 'instagram'): continue
            db.execute('''INSERT OR IGNORE INTO sources(canonical,channel,direction,source_id,occurred_ms,captured_ms,text)
                          VALUES (?,?,?,?,?,?,?)''', (canonical, channel, direction, sid, occurred, now, body))
            seq = db.execute('SELECT seq FROM sources WHERE canonical=?', (canonical,)).fetchone()[0]
            db.execute('INSERT OR IGNORE INTO aliases VALUES (?,?,?)', (kind, sid, seq))
            db.execute('INSERT OR IGNORE INTO positions VALUES (?,0)', (channel,))
        db.execute("INSERT OR REPLACE INTO meta VALUES ('last_capture_ms',?)", (str(now),))
    finally:
        source.close()


def _fail(db, run, now, reason, response=None):
    """Count each failed lease once, split batches, then isolate a poison singleton."""
    if run is None or run['status'] != 'pending': return False
    packet = decode(run['packet'])
    sources = packet['new_sources']
    channel = run['channel']
    window = db.execute('SELECT * FROM organizer_retries WHERE channel=?', (channel,)).fetchone()
    if not content_failure(reason):
        attempts = min(10, (window['attempts'] if window else 0) + 1)
        delay = min(900000, 15000 * 2**(attempts-1))
        db.execute('INSERT OR REPLACE INTO organizer_retries VALUES (?,?,?,?,?,?)',
                   (channel, max(run['through_seq'], window['through_seq'] if window else 0),
                    window['batch_limit'] if window else len(sources), attempts, now+delay, reason))
        db.execute("UPDATE runs SET status='failed',result=? WHERE id=?",
                   (encode({'reason': reason, 'retryable': True, 'retry_after_ms': delay}), run['id']))
        return True
    seq = sources[0]['seq']
    old = db.execute('SELECT * FROM failures WHERE seq=?', (seq,)).fetchone()
    attempts = (old['attempts'] if old else 0) + 1 if len(sources) == 1 else 0
    hints = decode(old['required_keys']) if old else []
    read_ids = {m['id'] for m in packet['existing_memories']}
    # Give the retry the omitted predecessor; never accept an unread replacement.
    if isinstance(response, dict) and isinstance(response.get('memories'), list):
        for item in response['memories'][:12]:
            if not isinstance(item, dict) or not isinstance(item.get('key'), str): continue
            row = db.execute('SELECT id FROM memories WHERE channel=? AND key=? AND active=1',
                             (run['channel'], item['key'])).fetchone()
            if row and row['id'] not in read_ids and item['key'] not in hints:
                hints.append(item['key'])
    limit = max(1, len(sources)//2)
    db.execute('INSERT OR REPLACE INTO organizer_retries VALUES (?,?,?,?,?,?)',
               (channel, max(run['through_seq'], window['through_seq'] if window else 0),
                min(limit, window['batch_limit']) if window else limit, 0, 0, reason))
    # Singleton attempts are independent of how many larger batches were split.
    db.execute('INSERT OR REPLACE INTO failures VALUES (?,?,?,?,?,?)',
               (seq, attempts, limit, encode(hints[:12]), reason, now))
    if len(sources) == 1 and attempts >= 3:
        db.execute('INSERT OR REPLACE INTO quarantined VALUES (?,?,?,?)',
                   (seq, reason, 'quarantined', now))
    db.execute("UPDATE runs SET status='failed',result=? WHERE id=?",
               (encode({'reason': reason}), run['id']))
    return True


def fail(cfg, run_id, now, reason='memory_worker_failed', response=None):
    opts = settings(cfg)
    require(opts['mode'] != 'off', 'episodic_memory_off')
    with file_lock(opts['path'], 'organizer', private=True), connection(cfg, True) as db:
        run = db.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone()
        return _fail(db, run, now, reason, response)


def retry_quarantined(cfg, seq, now, *, all_sources=False):
    opts = settings(cfg)
    require(opts['mode'] != 'off', 'episodic_memory_off')
    require(type(all_sources) is bool and ((seq is None) == all_sources), 'invalid_quarantine_selection')
    if seq is not None: integer(seq, 'seq', 1)
    with file_lock(opts['path'], 'organizer', private=True), connection(cfg, True) as db:
        require(not db.execute("SELECT 1 FROM runs WHERE status='pending' AND deadline_ms>?", (now,)).fetchone(), 'busy')
        rows = db.execute("SELECT seq FROM quarantined WHERE status='quarantined'" +
                          ('' if all_sources else ' AND seq=?'), () if all_sources else (seq,)).fetchall()
        require(all_sources or bool(rows), 'memory_quarantine_not_found')
        for row in rows:
            db.execute("UPDATE quarantined SET status='retry',updated_ms=? WHERE seq=?", (now, row[0]))
            db.execute('DELETE FROM failures WHERE seq=?', (row[0],))
            db.execute('DELETE FROM organizer_retries WHERE channel=(SELECT channel FROM sources WHERE seq=?)', (row[0],))
    return {'ok': True, 'reason': 'retry_scheduled', 'seq': seq, 'count': len(rows)}


def prepare(cfg, now):
    opts = settings(cfg)
    if opts['mode'] == 'off': return {'ok': True, 'reason': 'off'}
    with file_lock(opts['path'], 'organizer', private=True), connection(cfg, True) as db:
        for run in db.execute("SELECT * FROM runs WHERE status='pending' AND deadline_ms<=?", (now,)).fetchall():
            _fail(db, run, now, 'memory_run_expired')
        pending_run = db.execute("SELECT id FROM runs WHERE status='pending'").fetchone()
        if pending_run:
            return {'ok': True, 'reason': 'busy', 'run_id': pending_run['id']}
        capture(db, cfg, now)
        pending_clause = "(s.seq>p.seq AND q.seq IS NULL) OR q.status='retry'"
        first = db.execute('''SELECT s.* FROM sources s JOIN positions p USING(channel)
                            LEFT JOIN quarantined q ON q.seq=s.seq
                            LEFT JOIN organizer_retries r ON r.channel=s.channel
                            WHERE ('''+pending_clause+') AND COALESCE(r.not_before_ms,0)<=? ORDER BY s.seq LIMIT 1', (now,)).fetchone()
        if first is None:
            retry = db.execute('SELECT MIN(not_before_ms) FROM organizer_retries WHERE not_before_ms>?', (now,)).fetchone()[0]
            if retry is not None:
                return {'ok': True, 'reason': 'retry_later', 'retry_after_ms': retry-now}
            count = db.execute("SELECT count(*) FROM quarantined WHERE status!='resolved'").fetchone()[0]
            return {'ok': True, 'reason': 'needs_attention' if count else 'no_work', 'quarantined_count': count}
        channel = first['channel']
        position = db.execute('SELECT seq FROM positions WHERE channel=?', (channel,)).fetchone()[0]
        failure = db.execute('SELECT * FROM failures WHERE seq=?', (first['seq'],)).fetchone()
        limit = min(opts['batch_size'], failure['batch_limit']) if failure else opts['batch_size']
        window = db.execute('SELECT * FROM organizer_retries WHERE channel=?', (channel,)).fetchone()
        if window and first['seq'] <= window['through_seq']:
            limit = min(limit, window['batch_limit'])
        recovery = first['seq'] <= position
        if recovery: limit = 1
        pending = db.execute('''SELECT s.* FROM sources s JOIN positions p USING(channel)
                            LEFT JOIN quarantined q ON q.seq=s.seq
                            WHERE s.channel=? AND ('''+pending_clause+') ORDER BY s.seq LIMIT ?',
                             (channel, limit)).fetchall()
        sources = []
        for row in pending:
            entry = origins.entry(db,row)
            if len(encode(sources + [entry]).encode()) > opts['batch_bytes']:
                if not sources:
                    db.execute('INSERT OR REPLACE INTO quarantined VALUES (?,?,?,?)',
                               (row['seq'], 'memory_source_exceeds_batch_budget', 'quarantined', now))
                    continue
                break
            sources.append(entry)
        if not sources: return {'ok': True, 'reason': 'needs_attention'}
        # Context assists event merging; only new_sources are checkpointed by this run.
        context = []
        for row in reversed(db.execute('SELECT * FROM sources WHERE channel=? AND seq<=? ORDER BY seq DESC LIMIT 8',
                                       (channel, position)).fetchall()):
            entry = origins.entry(db,row)
            if len(encode(context+[entry]).encode()) <= opts['batch_bytes']//4:
                context.append(entry)
        terms = query_terms(' '.join(entry['text'] for entry in sources[-6:]))
        matching = lookup_rows(db, 'channel=? AND active=1', [channel], terms, 24)
        recent = db.execute("SELECT * FROM memories WHERE channel=? AND active=1 ORDER BY (kind='working') DESC,recorded_ms DESC LIMIT 12", (channel,)).fetchall()
        hints = decode(failure['required_keys']) if failure else []
        required = [db.execute('SELECT * FROM memories WHERE channel=? AND key=? AND active=1',
                               (channel, key)).fetchone() for key in hints]
        required = [r for r in required if r is not None]
        candidates = {r['id']:r for r in [*required,*matching,*recent]}
        ordered = sorted(candidates.values(),key=lambda r:(r['key'] in hints, r['kind']=='working'),reverse=True)
        existing = []
        for row in ordered:
            entry = rendered(row,db)
            if len(encode(existing+[entry]).encode()) <= opts['batch_bytes']//2 or (row['key'] in hints and not existing):
                existing.append(entry)
        run = uuid.uuid4().hex
        packet = {'schema': SCHEMA, 'run_id': run, 'channel': channel, 'scope': 'personal' if channel==origins.PERSONAL else 'private_dm',
                  'now_ms': now, 'through_seq': sources[-1]['seq'], 'new_sources': sources,
                  'prior_context': context, 'existing_memories': existing,
                  'reference_context':origins.context(db,cfg,channel,terms,now,opts['batch_bytes']//2,existing),
                  'existing_memories_omitted':db.execute('SELECT count(*) FROM memories WHERE channel=? AND active=1',
                                                         (channel,)).fetchone()[0]-len(existing)}
        sha = digest(packet)
        db.execute('INSERT INTO runs VALUES (?,?,?,?,?,?,?,?,?)',
                   (run, channel, packet['through_seq'], now + opts['lease_seconds'] * 1000,
                    'pending', encode(packet), sha, None, None))
        return {'ok': True, 'reason': 'prepared', 'packet': packet | {'input_sha256': sha},
                'run_id': run,
                'deadline_ms': now + opts['lease_seconds'] * 1000}


def bounded(value, name, maximum):
    value = text(value, name)
    require(len(value) <= maximum, 'memory_' + name + '_too_long')
    return value


def complete(cfg, response, now):
    try:
        return _complete(cfg, response, now)
    except (ControlError, TypeError, ValueError, KeyError) as exc:
        if isinstance(exc, ControlError) and exc.reason == 'busy':
            raise  # Preserve the same pending run and valid response for resubmission.
        if isinstance(response, dict) and isinstance(response.get('run_id'), str):
            fail(cfg, response['run_id'], now, exc.reason if isinstance(exc, ControlError) else 'invalid_memory_response', response)
        raise


def response_binding(cfg, run_id):
    """Read-only envelope binding/replay metadata; never expose source packets."""
    require(isinstance(run_id, str) and bool(run_id), 'invalid_memory_run_id')
    with connection(cfg) as db:
        row = db.execute('SELECT id,digest,status,result FROM runs WHERE id=?', (run_id,)).fetchone()
        require(row is not None, 'memory_run_not_found')
        return {'request_id': row['id'], 'input_sha256': row['digest'], 'status': row['status'],
                'result': decode(row['result']) if row['result'] else None}


def validate_response_identity(response, run_id=None):
    require(isinstance(response, dict), 'invalid_memory_response')
    for name in ('run_id', 'input_sha256'):
        require(isinstance(response.get(name), str) and bool(response[name].strip()),
                'invalid_memory_response')
    require(type(response.get('through_seq')) is int and response['through_seq'] >= 1,
            'invalid_memory_response')
    if run_id is not None:
        require(response['run_id'] == run_id, 'memory_input_mismatch')


def _complete(cfg, response, now):
    opts = settings(cfg)
    require(isinstance(response, dict) and set(response) == {'schema','run_id','input_sha256','through_seq','memories'},
            'invalid_memory_response')
    require(response['schema'] == SCHEMA, 'invalid_memory_schema')
    validate_response_identity(response)
    sha = digest(response)
    require(opts['mode'] != 'off', 'episodic_memory_off')
    with file_lock(opts['path'], 'organizer', private=True), connection(cfg, True) as db:
        run = db.execute('SELECT * FROM runs WHERE id=?', (response['run_id'],)).fetchone()
        require(run is not None, 'memory_run_not_found')
        if run['status'] == 'completed':
            require(run['response_digest'] == sha, 'memory_response_changed')
            return decode(run['result']) | {'replayed': True}
        require(run['status'] == 'pending' and now < run['deadline_ms'], 'memory_run_expired')
        require(response['input_sha256'] == run['digest'] and type(response['through_seq']) is int
                and response['through_seq'] == run['through_seq'], 'memory_input_mismatch')
        memories = response['memories']
        require(len(encode(response).encode()) <= 65536, 'memory_response_too_large')
        require(isinstance(memories, list) and len(memories) <= 12, 'invalid_memory_operations')
        packet = decode(run['packet'])
        allowed_seq = {s['seq'] for s in packet['new_sources'] + packet['prior_context']}
        existing_ids = {m['id'] for m in packet['existing_memories']}
        keys = set()
        for item in memories:
            require(isinstance(item, dict) and set(item)-{'origin_refs'} == {'key','kind','category','status','importance','summary',
                                                            'keywords','evidence','interpretation','supersedes'},
                    'invalid_memory_item')
            key = bounded(item['key'], 'key', 100)
            require(key not in keys and not any(c.isspace() for c in key), 'duplicate_memory_key')
            keys.add(key)
            require(item['kind'] in ('working','episode','fact') and item['category'] in CATEGORIES,
                    'invalid_memory_kind')
            require((item['kind'] == 'working') == (key == 'working'), 'invalid_working_key')
            require(item['status'] in ('ongoing','completed','planned','uncertain','retracted'), 'invalid_memory_status')
            integer(item['importance'], 'importance', 1)
            require(item['importance'] <= 5, 'invalid_importance')
            summary = bounded(item['summary'], 'summary', 500)
            interpretation = bounded(item['interpretation'], 'interpretation', 240)
            keywords = item['keywords']
            require(isinstance(keywords, list) and 1 <= len(keywords) <= 16, 'invalid_memory_keywords')
            keywords = [bounded(k, 'keyword', 40).lower() for k in keywords]
            previous = db.execute('SELECT * FROM memories WHERE channel=? AND key=? AND active=1', (run['channel'], key)).fetchone()
            require((previous['id'] if previous else None) == item['supersedes'], 'memory_version_changed')
            require(previous is None or previous['id'] in existing_ids, 'memory_previous_not_read')
            require(previous is None or previous['kind'] == item['kind'], 'memory_kind_changed')
            require(item['status'] != 'retracted' or previous is not None, 'memory_retraction_missing_previous')
            evidence = item['evidence']
            require(isinstance(evidence, list) and 1 <= len(evidence) <= 8, 'invalid_memory_evidence')
            validated = []
            inherited = {e['seq'] for e in decode(previous['evidence'])} if previous else set()
            seen = set()
            for e in evidence:
                require(isinstance(e, dict) and set(e) == {'seq','excerpt'} and type(e['seq']) is int,
                        'invalid_memory_evidence')
                require(e['seq'] in allowed_seq | inherited, 'memory_evidence_not_read')
                require(e['seq'] not in seen, 'duplicate_memory_evidence')
                seen.add(e['seq'])
                row = db.execute('SELECT * FROM sources WHERE seq=? AND channel=?', (e['seq'], run['channel'])).fetchone()
                excerpt = bounded(e['excerpt'], 'excerpt', 240)
                require(row is not None and excerpt in row['text'], 'memory_evidence_mismatch')
                proof={'seq': e['seq'], 'source_id': row['source_id'], 'direction': row['direction'],
                       'occurred_ms': row['occurred_ms'], 'excerpt': excerpt}
                author=db.execute('SELECT reference_json FROM source_origins WHERE seq=?',(e['seq'],)).fetchone()
                if author:proof.update(source_kind='author_event',origin=decode(author[0]))
                validated.append(proof)
            require(any(e['seq'] in {s['seq'] for s in packet['new_sources']} for e in validated),
                    'memory_new_evidence_required')
            linked=origins.select_links(item,packet,origins.links(db,previous['id'],full=True) if previous else [])
            require(item['status']=='retracted' or origins.current(cfg,linked,run['channel'],now),'memory_origins_changed')
            if previous:
                db.execute('UPDATE memories SET active=0 WHERE id=?', (previous['id'],))
            fid = 'episode:' + uuid.uuid4().hex
            db.execute('INSERT INTO memories VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       (fid, key, run['channel'], item['kind'], item['category'], item['status'], item['importance'],
                        summary, encode(keywords), encode(validated), interpretation,
                        min(e['occurred_ms'] for e in validated), now, 1, item['supersedes'],
                        ' '.join([summary, *keywords, item['category']]).lower()))
            for ref in linked:
                db.execute('INSERT INTO memory_origin_links VALUES (?,?,?,?,?)',(fid,ref['kind'],ref['id'],ref['version'],encode(ref)))
        db.execute('UPDATE positions SET seq=MAX(seq,?) WHERE channel=?', (run['through_seq'], run['channel']))
        for s in packet['new_sources']:
            db.execute('DELETE FROM failures WHERE seq=?', (s['seq'],))
            db.execute("UPDATE quarantined SET status='resolved',updated_ms=? WHERE seq=?", (now, s['seq']))
        window = db.execute('SELECT * FROM organizer_retries WHERE channel=?', (run['channel'],)).fetchone()
        if window:
            if run['through_seq'] >= window['through_seq']:
                db.execute('DELETE FROM organizer_retries WHERE channel=?', (run['channel'],))
            else:
                db.execute('UPDATE organizer_retries SET attempts=0,not_before_ms=0 WHERE channel=?', (run['channel'],))
        result = {'ok': True, 'reason': 'completed', 'run_id': run['id'],
                  'through_seq': run['through_seq'], 'memories': len(memories)}
        db.execute("UPDATE runs SET status='completed',response_digest=?,result=? WHERE id=?", (sha, encode(result), run['id']))
        return result


# Conversational filler that matches almost every summary and crowds out topic words.
STOPWORDS = frozenset('''너무 진짜 정말 완전 그냥 근데 그런데 그래서 그리고 그럼 그러면 그거 이거 저거 그게 이게
그건 이건 나도 너도 나는 너는 내가 네가 니가 너가 우리 지금 아까 이제 많이 하면 해서 했어 했는데 하는
있는 있어 없어 같은 같아 그렇게 이렇게 저렇게 어떻게 뭐야 뭐가 괜히 다시 계속 혹시 일단 약간 되게
엄청 진심 그래 그치 맞아 아니 근까 그니까 ㅇㅇ 어디 언제 누구 무슨 어느'''.split())
JAMO = re.compile(r'^[ㄱ-ㆎ]+$')
QUERY_TERMS = 32
QUERY_USER_MESSAGES = 6


# Relative weight of a matched term by where it came from in build_query.
TARGET_WEIGHT, USER_WEIGHT, OWN_WEIGHT = 1.0, 0.5, 0.1
# 3-character substrings only bridge spelling variants; a full-word match counts more.
SUBSTRING_WEIGHT = 0.5
# Trailing particles stripped so "아침은" also finds "아침"; longest first.
PARTICLES = ('에서', '까지', '한테', '에게', '부터', '으로', '처럼', '이랑',
             '은', '는', '이', '가', '을', '를', '도', '만', '에', '로', '랑', '와', '과', '의')


def stem(word):
    for particle in PARTICLES:
        if word.endswith(particle) and len(word) - len(particle) >= 2:
            return word[:-len(particle)]
    return None


def weighted_terms(value):
    """(term, weight) pairs in priority order, capped at QUERY_TERMS.

    `value` is one text, or a list of texts and (text, weight) pairs, most important first.
    Each text contributes its words, their particle-stripped stems and then their
    3-character substrings before the next text, so the cap drops low-priority context
    rather than the target message.
    """
    segments = [value] if isinstance(value, str) else list(value)
    weights = {}
    for segment in segments:
        segment, weight = (segment, 1.0) if isinstance(segment, str) else segment
        words = [w for w in re.findall(r'[\w가-힣]+', segment.lower())
                 if len(w) >= 2 and w not in STOPWORDS and not JAMO.match(w)]
        for term in words + [stem(w) for w in words if stem(w)]:
            weights.setdefault(term, weight)
        # Unicode substrings tolerate other inflections and agent-authored aliases.
        for term in [w[i:i+3] for w in words if len(w) > 3 for i in range(len(w)-2)]:
            weights.setdefault(term, weight * SUBSTRING_WEIGHT)
    return list(weights.items())[:QUERY_TERMS]


def query_terms(value):
    return [term for term, _ in weighted_terms(value)]


def build_query(targets, messages):
    """Weighted recall query: target DMs, then recent user messages, then the last own turn.

    The persona's own long replies otherwise dominate matching and recall memories that
    resemble what was just said instead of what the user is talking about. The own turn
    stays at a low weight so a content-free answer ("응 그거") can still find its topic.
    """
    targets = [t for t in targets if t]
    users = [m.get('text', '') for m in messages if m.get('direction') == 'incoming']
    users = [t for t in users[-QUERY_USER_MESSAGES:][::-1] if t and t not in targets]
    own = []
    for m in reversed(messages):
        if m.get('direction') == 'outgoing':
            own.append(m.get('text', ''))
        elif own:
            break
    query = [(t, TARGET_WEIGHT) for t in targets] + [(t, USER_WEIGHT) for t in users]
    if own:
        query.append((' '.join(reversed(own)), OWN_WEIGHT))
    return query


# Okapi BM25 over substring matches. SQLite FTS5 trigram cannot match the 2-character
# Korean words that carry most topics (듀오, 조교, 병원), so term frequency is counted here.
BM25_K1, BM25_B = 1.2, 0.75
# Importance and recency break ties between similar matches; one distinctive term outweighs them.
IMPORTANCE_WEIGHT = 0.15
RECENCY_WEIGHT = 1.0
HALF_LIFE_HOURS = {'episode': 72, 'fact': 720}
OPEN_HALF_LIFE_HOURS = 168   # planned/ongoing episodes stay current for about a week
MATCH_LIMIT = 512


def lookup_rows(db,where,args,terms,limit):
    if not terms:return []
    condition=' OR '.join('instr(search_text,?)>0' for _ in terms)
    ranking='+'.join('CASE WHEN instr(search_text,?)>0 THEN 1 ELSE 0 END' for _ in terms)
    return list(db.execute('SELECT * FROM memories WHERE '+where+' AND ('+condition+') ORDER BY ('+ranking+') DESC,recorded_ms DESC LIMIT ?',args+terms+terms+[limit]))


def recency(row, now):
    if row['kind'] == 'working':
        return 0.0
    hours = HALF_LIFE_HOURS.get(row['kind'], 72)
    if row['kind'] == 'episode' and row['status'] in ('planned', 'ongoing'):
        hours = OPEN_HALF_LIFE_HOURS
    return RECENCY_WEIGHT * 0.5 ** (max(0, now - row['occurred_ms']) / 3600000 / hours)


def candidates(db, channel, query, now, categories=None):
    """Ranked active rows for one channel before origin checks and display budgeting."""
    args = [channel,origins.PERSONAL]
    where = "channel IN (?,?) AND active=1 AND status!='retracted' AND occurred_ms<=?"
    args.append(now)
    if categories:
        where += ' AND category IN (' + ','.join('?' for _ in categories) + ')'
        args.extend(categories)
    weighted = weighted_terms(query)
    terms = [t for t, _ in weighted]
    matched, idf, average = [], {}, 1.0
    if terms:
        matched = lookup_rows(db,where,args,terms,MATCH_LIMIT)
        count, average = db.execute('SELECT count(*),avg(length(search_text)) FROM memories WHERE '+where,args).fetchone()
        average = average or 1.0
        frequency = db.execute('SELECT '+','.join('SUM(instr(search_text,?)>0)' for _ in terms)
                               +' FROM memories WHERE '+where, terms+args).fetchone()
        idf = {t: math.log(1 + (count - (df or 0) + .5) / ((df or 0) + .5)) for t, df in zip(terms, frequency)}
    fallback = list(db.execute('SELECT * FROM memories WHERE '+where+''' AND
        (kind='working' OR status IN ('planned','ongoing') OR (importance>=4 AND occurred_ms>=?))
        ORDER BY (kind='working') DESC,importance DESC,recorded_ms DESC LIMIT 12''',args+[now-86400000]))
    rows = {r['id']:r for r in matched+fallback}
    def relevance(row):
        text, total = row['search_text'], 0.0
        norm = BM25_K1 * (1 - BM25_B + BM25_B * len(text) / average)
        for term, weight in weighted:
            tf = text.count(term)
            if tf:
                total += weight * idf[term] * tf * (BM25_K1 + 1) / (tf + norm)
        return total
    def score(row):
        return ((100 if row['kind']=='working' else 0) + relevance(row)
                + IMPORTANCE_WEIGHT * row['importance'] + recency(row, now))
    return sorted(rows.values(), key=lambda r:(score(r),r['recorded_ms'],r['id']), reverse=True)


def recall(cfg, channel, query, now, categories=None, recent_ids=()):
    opts = reply_settings(cfg)
    if opts['mode'] == 'off': return {'mode': 'off', 'items': [], **({'reason': opts['diagnostic']} if 'diagnostic' in opts else {})}
    require(channel in ('slack','instagram'), 'invalid_channel')
    require(categories is None or isinstance(categories, list) and all(c in CATEGORIES for c in categories),
            'invalid_memory_categories')
    try:
        with connection(cfg) as db:
            ordered = candidates(db, channel, query, now, categories)
            items=[];stale=0;usable=[]
            for row in ordered:
                if not origins.current(cfg,origins.links(db,row['id']),row['channel'],now):
                    stale+=1;continue
                usable.append(row)
            # A channel-specific retraction must not resurrect its personal root.
            roots={r['key'] for r in usable if r['channel']==origins.PERSONAL}
            withdrawn=set()
            if roots:
                marks=','.join('?' for _ in roots)
                for row in db.execute("SELECT * FROM memories WHERE channel=? AND active=1 AND status='retracted' AND key IN ("+marks+")",[channel]+list(roots)):
                    if origins.current(cfg,origins.links(db,row['id']),channel,now):withdrawn.add(row['key'])
            usable=[r for r in usable if r['channel']!=origins.PERSONAL or r['key'] not in withdrawn]
            # A channel-specific continuation replaces the display of its personal
            # root, without changing the author ledger or leaking DM into other channels.
            local={r['key']:r for r in usable if r['channel']==channel}
            personal_keys={r['key'] for r in usable if r['channel']==origins.PERSONAL}
            display=[];seen=set()
            for row in usable:
                # Prefer a local continuation, but retain the root as a fallback
                # when the local result cannot fit the caller's context budget.
                choices=([local[row['key']],row] if row['channel']==origins.PERSONAL and row['key'] in local else [row])
                for choice in choices:
                    if choice['id'] not in seen:display.append(choice);seen.add(choice['id'])
            chosen=set()
            for row in display:
                if row['key'] in personal_keys and row['key'] in chosen:continue
                item=rendered(row,db)
                if len(encode(items+[item]).encode()) > opts['context_bytes']: continue
                items.append(item);chosen.add(row['key'])
                if len(items)>=opts['max_items']: break
            display_count=len({r['key'] if r['key'] in personal_keys else r['id'] for r in display})
            position=db.execute('SELECT seq FROM positions WHERE channel=?',(channel,)).fetchone()
            through=position[0] if position else 0
            pending=db.execute('''SELECT count(*) FROM sources s LEFT JOIN positions p USING(channel)
                                  WHERE s.channel IN (?,?) AND s.seq>COALESCE(p.seq,0)''',(channel,origins.PERSONAL)).fetchone()[0]
            try:
                quarantined=[{'seq':r['seq'],'reason':r['reason'],'status':r['status']} for r in db.execute('''
                    SELECT q.* FROM quarantined q JOIN sources s USING(seq)
                    WHERE s.channel IN (?,?) AND q.status!='resolved' ORDER BY seq LIMIT 20''', (channel,origins.PERSONAL))]
                quarantine_count=db.execute('''SELECT count(*) FROM quarantined q JOIN sources s USING(seq)
                    WHERE s.channel IN (?,?) AND q.status!='resolved' ''',(channel,origins.PERSONAL)).fetchone()[0]
            except sqlite3.OperationalError:
                # A d64ff41 sidecar remains readable before its next organizer migration.
                quarantined=[];quarantine_count=0
            unread=[]
            for mid in list(recent_ids)[-30:]:
                alias=db.execute("SELECT seq FROM aliases WHERE kind='message' AND id=?",(mid,)).fetchone()
                if alias is None:
                    provider=mid.rsplit(':',1)[-1] if channel=='slack' else mid
                    alias=db.execute('SELECT seq FROM sources WHERE canonical=?',
                                     (encode([channel,'outgoing',provider]),)).fetchone()
                if alias is None or alias[0]>through: unread.append(mid)
            last=db.execute("SELECT value FROM meta WHERE key='last_capture_ms'").fetchone()
            since=db.execute("SELECT value FROM meta WHERE key='since_ms'").fetchone()
            result={'mode':opts['mode'],'context_only':True,'items':items if opts['mode']=='enforce' else [],
                    'candidate_count':len(items),'has_more':display_count>len(items),'pending_captured':pending,
                    'recent_unprocessed_ids':unread,'last_capture_ms':int(last[0]) if last else None,
                    'coverage_since_ms':int(since[0]) if since else None,'search_is_semantic':False}
            result.update(quarantined=quarantined, quarantined_count=quarantine_count,stale_origin_count=stale)
            result['incomplete']=bool(pending or unread or quarantine_count or stale or not last
                                  or now-int(last[0])>opts['capture_stale_seconds']*1000)
            return result
    except (OSError,sqlite3.Error,ControlError,TypeError,ValueError):
        return {'mode':opts['mode'],'items':[],'incomplete':True,'reason':'memory_store_unavailable'}


def validate(cfg, snapshots, now=None):
    """New unrelated memories do not invalidate a reply; changed selected versions do."""
    selected=[(channel,item) for channel,snapshot in snapshots.items() for item in snapshot.get('items',[])]
    if not selected: return
    require(reply_settings(cfg)['mode']=='enforce','memory_store_unavailable')
    try:
        with connection(cfg) as db:
            for channel,item in selected:
                row=db.execute('SELECT * FROM memories WHERE id=? AND channel IN (?,?) AND active=1 AND status!=?',
                               (item['id'],channel,origins.PERSONAL,'retracted')).fetchone()
                require(row is not None and rendered(row,db)==item,'memory_inputs_changed')
                require(origins.current(cfg,origins.links(db,row['id']),row['channel'],now if now is not None else now_ms()),
                        'memory_inputs_changed')
    except ControlError as exc:
        if exc.reason == 'memory_inputs_changed': raise
        if exc.reason in ('memory_peer_changed', 'memory_binding_mismatch', 'incompatible_memory_database'):
            raise ControlError('memory_inputs_changed') from exc
        raise ControlError('memory_store_unavailable') from exc
    except (OSError,sqlite3.Error,TypeError,ValueError) as exc:
        raise ControlError('memory_store_unavailable') from exc


def validate_reply(runner, state):
    """One scoped reprepare without unavailable optional inputs, never use stale wording."""
    try:
        validate(runner.cfg, state.get('episodic_memory', {}),runner.clock())
    except ControlError as exc:
        if exc.reason != 'memory_store_unavailable': raise
        if state.get('episodic_suspended_turn') != state['turn_id']:
            state['episodic_suspended_turn'] = state['turn_id']
            from gate import write_json
            write_json(runner.path, state)
            runner.event('episodic_memory_degraded', state['turn_id'], reason=exc.reason, next='prepare')
        raise ControlError('episodic_reprepare_required') from exc
