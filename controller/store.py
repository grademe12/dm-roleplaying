"""Deterministic persistence only. Muse supplies activities and dialogue."""
import hashlib
import os
import re
import sqlite3
import tempfile
import uuid
from contextlib import contextmanager, nullcontext
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from common import ControlError, decode, encode, file_lock, integer, now_ms, require, text
from migrate_parts import LEGACY_CHECK
from message_limits import MAX_PARTS
from slack_delivery import SQL as SLACK_DELIVERY_SQL
import persona
import action_intents
import conversation_flow
import topic_materials
import decision_routing
import instrumentation
import continuations
import delivery_flow
import activity_history
import morning_contact
import proactive_check
import world_events
import memory_continuity
import in_person
import appointment_lifecycle

VERSION = '0.3'
KST = timezone(timedelta(hours=9))
METRICS = ('fatigue', 'mood', 'hunger', 'social_fatigue')
# Active reply groups created by `initiate` (no reply_targets rows).
PROACTIVE_ACTIVE_SQL = '''SELECT g.id FROM reply_groups g WHERE g.session_id=? AND g.status='active'
    AND NOT EXISTS (SELECT 1 FROM reply_targets t WHERE t.session_id=g.session_id AND t.group_id=g.id)'''
SCHEMA = Path(__file__).parent / 'schema'
NEXT_DECISION_MIN_MS = 60000


# Conversation session lease: while active, the gate stays silent and one Muse run owns the DMs.
# owner is the wake ID that set it (NULL for legacy/CLI waits); an ended wake may release only its own lease.
LEASE_SQL = '''CREATE TABLE IF NOT EXISTS conversation_leases (
  session_id TEXT PRIMARY KEY REFERENCES sessions(id),
  until_ms INTEGER NOT NULL,
  started_ms INTEGER NOT NULL,
  waits INTEGER NOT NULL DEFAULT 0,
  owner TEXT)'''
GROUP_REVIEW_SQL = '''CREATE TABLE IF NOT EXISTS group_reviews (
  session_id TEXT NOT NULL, group_id TEXT NOT NULL, reviewed_ms INTEGER NOT NULL,
  PRIMARY KEY(session_id,group_id),
  FOREIGN KEY(session_id,group_id) REFERENCES reply_groups(session_id,id))'''
SELF_CLAIMS_SQL = '''CREATE TABLE IF NOT EXISTS job_self_claims (
  session_id TEXT NOT NULL, job_id TEXT NOT NULL, claim_index INTEGER NOT NULL,
  fact_id TEXT NOT NULL, excerpt TEXT NOT NULL,
  PRIMARY KEY(session_id,job_id,claim_index),
  FOREIGN KEY(session_id,job_id) REFERENCES dm_jobs(session_id,id))'''
# Delivery channels inside one session (one persona, one memory, several messengers).
# Groups without a row and messages without observation.channel are 'instagram' (pre-channel data).
# Dispatch also uses this name for Instagram-only blocking; new initiates use persona.DEFAULT_CHANNEL.
DEFAULT_CHANNEL = 'instagram'
CHANNEL_SQL = {
    'group_channels': '''CREATE TABLE IF NOT EXISTS group_channels (
      session_id TEXT NOT NULL, group_id TEXT NOT NULL, channel TEXT NOT NULL,
      PRIMARY KEY(session_id,group_id))''',
    'channel_blocks': '''CREATE TABLE IF NOT EXISTS channel_blocks (
      session_id TEXT NOT NULL, channel TEXT NOT NULL, blocked_until_ms INTEGER NOT NULL,
      PRIMARY KEY(session_id,channel))''',
    'channel_pacing': '''CREATE TABLE IF NOT EXISTS channel_pacing (
      session_id TEXT NOT NULL REFERENCES sessions(id), channel TEXT NOT NULL,
      next_send_after_ms INTEGER NOT NULL,
      PRIMARY KEY(session_id,channel))'''}
GROUP_CHANNEL_SQL = "COALESCE((SELECT channel FROM group_channels gc WHERE gc.session_id=? AND gc.group_id=?),'instagram')"


def valid_channel(value):
    require(isinstance(value, str) and value.replace('_', '').replace('-', '').isalnum() and value == value.lower()
            and 0 < len(value) <= 32, 'invalid_channel')
    return value


def message_channel(row):
    return decode(row['observation_json']).get('channel', DEFAULT_CHANNEL)


def group_channel(db, session, gid):
    return db.execute('SELECT '+GROUP_CHANNEL_SQL, (session, gid)).fetchone()[0]


def validate_review_checks(checks):
    """Pure shape/evidence/pass checks, shared with pre-submit validation."""
    require(isinstance(checks, dict) and set(checks) == {'self_reset', 'missed_answer', 'conflict', 'self_consistency'},
            'missing_review_checks')
    for name in ('self_reset', 'missed_answer', 'conflict'):
        check = checks[name]
        require(isinstance(check, dict) and set(check) == {'passed', 'evidence'}, 'invalid_review_check')
        text(check.get('evidence'), 'review_evidence')
        require(check['passed'] is True, 'conversation_review_failed')
    consistency = checks['self_consistency']
    require(isinstance(consistency, dict) and set(consistency) == {'passed', 'evidence', 'claims'},
            'invalid_self_consistency_check')
    text(consistency.get('evidence'), 'review_evidence')
    require(consistency['passed'] is True, 'conversation_review_failed')
    claims = consistency['claims']
    require(isinstance(claims, list) and len(claims) <= 5, 'invalid_self_claims')
    return claims


def validate_conversation_review(db, session, decision, parts, target_ids, channel):
    """Require an auditable context and draft check before a DM enters the send queue.

    The controller validates references and completion, not the semantic truth of Muse's judgment.
    """
    review = decision.get('conversation_review')
    require(isinstance(review, dict) and set(review) == {'context', 'memory', 'draft_parts', 'checks'},
            'missing_conversation_review')
    require(review['draft_parts'] == parts, 'review_draft_mismatch')
    context = review['context']
    require(isinstance(context, dict) and set(context) == {
        'channel', 'recent_message_ids', 'target_message_ids', 'previous_self', 'user_uptake', 'topic'},
        'invalid_review_context')
    require(context['channel'] == channel, 'review_channel_mismatch')
    require(context['target_message_ids'] == target_ids, 'review_target_mismatch')
    recent = context['recent_message_ids']
    require(isinstance(recent, list) and len(recent) <= 30
            and all(isinstance(mid, str) and mid.strip() for mid in recent), 'invalid_review_recent_ids')
    require(len(recent) == len(set(recent)) and (not target_ids or recent), 'invalid_review_recent_ids')
    for mid in target_ids:
        require(db.execute('SELECT 1 FROM dm_messages WHERE session_id=? AND id=?', (session, mid)).fetchone() is not None,
                'review_target_not_found')
    previous = context['previous_self']
    if previous is not None:
        require(isinstance(previous, dict) and set(previous) == {'message_id', 'point'}, 'invalid_review_previous_self')
        require(text(previous.get('message_id'), 'review_previous_self_id') in recent,
                'review_previous_self_not_in_context')
        text(previous.get('point'), 'review_previous_self_point')
    if target_ids:
        text(context.get('user_uptake'), 'review_user_uptake')
    else:
        require(context['user_uptake'] is None, 'invalid_review_user_uptake')
    text(context.get('topic'), 'review_topic')
    memory = review['memory']
    require(isinstance(memory, dict) and set(memory) == {'kind', 'topic_state_key', 'result'},
            'invalid_review_memory')
    require(memory['kind'] in ('general', 'hobby'), 'invalid_review_memory_kind')
    if memory['kind'] == 'general':
        require(memory['topic_state_key'] is None and memory['result'] == 'not_applicable',
                'invalid_review_memory')
    else:
        key = text(memory['topic_state_key'], 'review_topic_state_key')
        require(key.startswith('topic_state:'), 'invalid_review_topic_state_key')
        require(memory['result'] in ('found', 'missing'), 'missing_topic_state_lookup')
    claims = validate_review_checks(review['checks'])
    draft_text = [part for part in parts if isinstance(part, str)]
    for claim in claims:
        require(isinstance(claim, dict) and set(claim) == {'excerpt', 'fact_id'}, 'invalid_self_claim')
        excerpt = text(claim.get('excerpt'), 'self_claim_excerpt')
        require(any(excerpt in part for part in draft_text), 'self_claim_not_in_draft')
        fact_id = text(claim.get('fact_id'), 'self_claim_fact_id')
        require(not fact_id.startswith('self-said:'), 'self_claim_requires_factual_basis')
        require(memory_continuity.valid_factual_basis(db, session, fact_id, channel),
                'self_claim_requires_factual_basis')
        require(db.execute('SELECT 1 FROM facts WHERE session_id=? AND id=?', (session, fact_id)).fetchone() is not None,
                'self_claim_fact_missing')
    return claims


LIVE_WINDOW_MS = 300000
# A typing burst is answered once the user pauses, but never waits longer than the cap.
INCOMING_SETTLE_MS = 6000
INCOMING_SETTLE_CAP_MS = 20000
# Revisions caused only by new incoming DMs; a decision prepared before them may still apply.
REVISION_SOURCES_SQL = '''CREATE TABLE IF NOT EXISTS revision_sources (
  session_id TEXT NOT NULL, revision INTEGER NOT NULL, source TEXT NOT NULL,
  PRIMARY KEY(session_id, revision))'''
LEASE_GRACE_MS = 180000
WAIT_LEASE_MS = 30000
WAIT_LEASE_RENEW_MS = 10000
APPOINTMENT_CONFIRMED_WINDOW_MS = 7*86400000
APPOINTMENT_PROPOSED_WINDOW_MS = 86400000
APPOINTMENT_TICK_LIMIT = 20
WAIT_WAKE_NEEDS = ('morning_contact_due', 'proactive_check_due', 'decision_due', 'reply_due', 'activity_due', 'appointment_due',
                   'appointment_prepare_due','appointment_review_due',
                   'draft_needs_review', 'draft_stale')


def conversation_mode(db, session, now, channel=None):
    """Keep a free/light conversation live briefly after a reply, even if composing took time."""
    hour = datetime.fromtimestamp(now/1000, KST).hour
    active = db.execute("SELECT attention,category FROM activities WHERE session_id=? AND status='in_progress'", (session,)).fetchone()
    scene = in_person.active(db,session)
    if scene is not None and (channel is None or scene['channel'] == channel) and active and active['category'] in ('sleep','수면'):
        return 'quiet'
    if in_person.live(db,session,now,channel):
        return 'live'
    night = persona.NIGHT_QUIET_UNTIL_HOUR is not None and hour < persona.NIGHT_QUIET_UNTIL_HOUR
    if night or (active and active['attention'] in ('focused', 'unavailable')):
        return 'quiet'
    received = db.execute("""SELECT max(observed_ms) FROM dm_messages WHERE session_id=?
        AND json_extract(observation_json,'$.direction')='incoming'""", (session,)).fetchone()[0]
    if active and active['attention'] in ('free', 'light'):
        if received is not None and now-LIVE_WINDOW_MS <= received <= now:
            return 'live'
        replied = db.execute("""SELECT max(g.created_ms) FROM reply_groups g
            JOIN reply_targets t ON t.session_id=g.session_id AND t.group_id=g.id
            JOIN dm_jobs j ON j.session_id=g.session_id AND j.group_id=g.id
            WHERE g.session_id=? AND g.status IN ('active','completed') AND j.due_ms<=?""",
            (session, now)).fetchone()[0]
        if replied is not None and now-LIVE_WINDOW_MS <= replied <= now:
            return 'live'
    return 'normal'


def rowdict(row):
    if row is None:
        return None
    result = dict(row)
    for key in list(result):
        if key.endswith('_json'):
            result[key[:-5]] = decode(result.pop(key))
    return result


def session_row(db, session):
    row = db.execute('SELECT * FROM sessions WHERE id=?', (session,)).fetchone()
    require(row is not None, 'session_not_found')
    return dict(row)


def incoming_settle_remaining_ms(messages, now, settle_ms=INCOMING_SETTLE_MS):
    """Milliseconds until the newest unanswered message is settle_ms old; 0 if none."""
    times = [max(m.get('observed_ms') or 0, (m.get('observation') or {}).get('ingested_ms') or 0)
             for m in messages if m.get('status') == 'received']
    return max(0, max(times) + settle_ms - now) if times else 0


def only_incoming_since(db, session, since, current):
    """True when every revision in (since, current] came from an incoming-only ingest."""
    if not since < current:
        return False
    count = db.execute('''SELECT COUNT(*) FROM revision_sources WHERE session_id=? AND revision>?
        AND revision<=? AND source='incoming_dm' ''', (session, since, current)).fetchone()[0]
    return count == current - since


def bump(db, session):
    db.execute('UPDATE sessions SET revision=revision+1 WHERE id=?', (session,))


class Store:
    def __init__(self, path):
        self.path = Path(path).expanduser().resolve()

    @contextmanager
    def connect(self, write=False, *, operation=None, session=None, busy_timeout_ms=5000):
        require(self.path.is_file(), 'database_not_initialized')
        integer(busy_timeout_ms,'busy_timeout_ms')
        db = sqlite3.connect(self.path.as_uri() + '?mode=rw', uri=True, timeout=busy_timeout_ms/1000,
                             isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute('PRAGMA foreign_keys=ON')
            db.execute(f'PRAGMA busy_timeout={busy_timeout_ms}')
            require(db.execute('SELECT version FROM controller_meta').fetchone()[0] == VERSION,
                    'incompatible_database')
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='conversation_leases'").fetchone() is None:
                db.execute(LEASE_SQL)
            if not any(r[1] == 'owner' for r in db.execute('PRAGMA table_info(conversation_leases)')):
                db.execute('BEGIN IMMEDIATE')
                if not any(r[1] == 'owner' for r in db.execute('PRAGMA table_info(conversation_leases)')):
                    db.execute('ALTER TABLE conversation_leases ADD COLUMN owner TEXT')
                db.commit()
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='group_reviews'").fetchone() is None:
                db.execute(GROUP_REVIEW_SQL)
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='job_self_claims'").fetchone() is None:
                db.execute(SELF_CLAIMS_SQL)
            for name, sql in CHANNEL_SQL.items():
                if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is None:
                    if name == 'channel_pacing':
                        db.execute('BEGIN IMMEDIATE')
                        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='channel_pacing'").fetchone() is None:
                            db.execute(sql)
                            # Move the old shared gap to the channel that last attempted a send.
                            db.execute('''INSERT INTO channel_pacing(session_id,channel,next_send_after_ms)
                                SELECT s.id,COALESCE((SELECT gc.channel FROM dm_jobs j
                                    LEFT JOIN group_channels gc ON gc.session_id=j.session_id AND gc.group_id=j.group_id
                                    WHERE j.session_id=s.id AND j.status IN ('confirmed','submitted','unknown')
                                    ORDER BY j.updated_ms DESC LIMIT 1),'instagram'),s.next_send_after_ms
                                FROM sessions s WHERE s.next_send_after_ms IS NOT NULL''')
                            db.execute("UPDATE channel_pacing SET next_send_after_ms=min(next_send_after_ms,?) WHERE channel='slack'",
                                       (int(datetime.now(timezone.utc).timestamp()*1000) + 45000,))
                            db.execute('UPDATE sessions SET next_send_after_ms=NULL')
                        db.commit()
                    else:
                        db.execute(sql)
            for statement in SLACK_DELIVERY_SQL:
                db.execute(statement)
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='action_intents'").fetchone() is None:
                db.execute('BEGIN IMMEDIATE')
                action_intents.initialize(db)
                db.commit()
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='conversation_use_jobs'").fetchone() is None:
                db.execute('BEGIN IMMEDIATE')
                conversation_flow.initialize(db)
                topic_materials.initialize(db)
                db.commit()
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='continuation_runs'").fetchone() is None:
                db.execute('BEGIN IMMEDIATE')
                continuations.initialize(db)
                db.commit()
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='dm_flow_passes'").fetchone() is None:
                db.execute('BEGIN IMMEDIATE')
                delivery_flow.initialize(db)
                db.commit()
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='event_imports'").fetchone() is None:
                db.execute('BEGIN IMMEDIATE')
                world_events.initialize(db)
                db.commit()
            db.execute(morning_contact.SQL)
            proactive_check.ensure_schema(db)
            db.execute(REVISION_SOURCES_SQL)
            if db.execute("SELECT count(*) FROM sqlite_master WHERE type='table' AND name IN ('in_person_state','group_scene_context')").fetchone()[0] != 2:
                db.execute('BEGIN IMMEDIATE')
                for statement in in_person.SQL:
                    db.execute(statement)
                db.commit()
            if db.execute("SELECT count(*) FROM sqlite_master WHERE type='table' AND name IN ('continuity_policy','continuity_diagnostics')").fetchone()[0] != 2:
                db.execute('BEGIN IMMEDIATE')
                memory_continuity.initialize(db)
                db.commit()
            db.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
            revision_before = None
            transaction_started_ms = now_ms()
            if write and operation and session is not None:
                row = db.execute('SELECT revision FROM sessions WHERE id=?', (session,)).fetchone()
                revision_before = row[0] if row is not None else None
            yield db
            # An explicit rollback (preflight or warning review) must not produce a write event.
            revision_after = None
            if revision_before is not None and db.in_transaction:
                row = db.execute('SELECT revision FROM sessions WHERE id=?', (session,)).fetchone()
                revision_after = row[0] if row is not None else None
            db.commit()
            if revision_after is not None and revision_after != revision_before:
                instrumentation.revision_event(self.path, operation, session, revision_before,
                                               revision_after, transaction_started_ms, now_ms())
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def seed(self, data, now, session='main', dry_run=False):
        """Validate all input in a private DB, publish only after successful commit."""
        integer(now, 'now_ms')
        text(session, 'session')
        require(isinstance(data,dict),'invalid_seed_document')
        encode(data)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with file_lock(self.path, 'initialize'):
            require(not self.path.exists(), 'database_exists')
            fd, tmp = tempfile.mkstemp(prefix='.controller-seed-', suffix='.db', dir=self.path.parent)
            os.close(fd)
            db = sqlite3.connect(tmp, isolation_level=None)
            db.row_factory = sqlite3.Row
            try:
                db.execute('PRAGMA foreign_keys=ON')
                sql = (SCHEMA/'base.sql').read_text(encoding='utf-8') + '\n' + (SCHEMA/'controller.sql').read_text(encoding='utf-8')
                db.executescript('BEGIN IMMEDIATE;\n' + sql)
                db.execute('UPDATE controller_meta SET version=?', (VERSION,))
                mode = data.get('transport_mode', 'mock')
                require(mode in ('mock','external'), 'invalid_transport_mode')
                db.execute('INSERT INTO runtime_settings VALUES (1,?)', (mode,))
                initial = data.get('state', {})
                require(set(initial) <= set(METRICS) | {'location'}, 'invalid_initial_state')
                values = [initial.get(k, v) for k, v in zip(METRICS, (30, 55, 30, 20))]
                for value in values:
                    require(type(value) in (int, float) and 0 <= value <= 100, 'invalid_metric')
                db.execute('''INSERT INTO sessions(id,advanced_to_ms,wake_mode,location,
                    fatigue,mood,hunger,social_fatigue,sender,recipient)
                    VALUES (?,?,'scheduled',?,?,?,?,?,'persona','user')''',
                    (session, now, initial.get('location', 'unknown'), *values))
                counts = self._ingest(db, session, data.get('messages', []), now, seed=True)
                for fact in data.get('facts', []):
                    self._fact(db, session, fact, now)
                self._fact(db, session, {'id': 'controller-initialized', 'source': 'simulated',
                    'summary': '생활 상태 초기화: 입력값 또는 운영 기본값이며 관측 사실이 아님',
                    'details': {'state': initial}}, now)
                db.commit()
                require(db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok', 'invalid_database')
                db.close()
                if not dry_run:
                    # All controller initializers hold the same lock. Never replace an existing DB.
                    require(not self.path.exists(), 'database_exists')
                    if os.name == 'nt':
                        os.rename(tmp, self.path)  # Windows rename fails if target exists.
                    else:
                        os.link(tmp, self.path)
                    with sqlite3.connect(self.path) as wal:
                        wal.execute('PRAGMA journal_mode=WAL')
                    wal.close()
                return {'ok': True, 'reason': 'seed_preview' if dry_run else 'seeded',
                        'revision': 0, 'messages': counts}
            finally:
                db.close()
                Path(tmp).unlink(missing_ok=True)

    def _ingest(self, db, session, messages, now, seed=False):
        require(isinstance(messages, list), 'invalid_messages')
        count = 0
        for m in messages:
            mid = text(m['id'], 'message_id')
            body = text(m['text'])
            observed = integer(m.get('observed_ms', now), 'observed_ms')
            require(observed <= now, 'future_observation')
            direction = m.get('direction', 'incoming')
            require(direction in ('incoming', 'outgoing'), 'invalid_direction')
            info = dict(m.get('observation', {}))
            info['direction'] = direction
            require(info.get('identity') in ('platform', 'verified_composite', 'uncertain'),
                    'missing_identity_evidence')
            if info['identity'] == 'uncertain':
                info['review_required'] = True
            if seed and direction == 'incoming':
                disposition = m.get('disposition', 'uncertain')
                require(disposition in ('handled', 'unanswered', 'uncertain'), 'invalid_disposition')
                if disposition == 'handled':
                    text(m.get('handled_evidence'), 'handled_evidence')
                    info['handled_evidence'] = m['handled_evidence']
                elif disposition == 'uncertain':
                    info['review_required'] = True
            else:
                disposition = 'unanswered'
            handled = direction == 'outgoing' or disposition == 'handled'
            old = db.execute('SELECT * FROM dm_messages WHERE session_id=? AND id=?', (session, mid)).fetchone()
            if old:
                require(old['text'] == body and decode(old['observation_json'])['direction'] == direction,
                        'message_identity_collision')
                continue
            # The platform timestamp is when the user sent the DM. Keep the separate
            # controller receipt time for measuring listener and backfill latency.
            info['ingested_ms'] = now
            db.execute('INSERT INTO dm_messages VALUES (?,?,?,?,?,NULL,?)',
                       (session, mid, observed, body, 'handled' if handled else 'received', encode(info)))
            if direction == 'incoming':
                db.execute('INSERT INTO input_events VALUES (?,?,?,?,?,?)',
                           (session, 'dm:' + mid, 'dm_received', observed, now if handled else None,
                            encode({'message_id': mid})))
                if not seed:
                    continuations.incoming(db, session, info.get('channel', DEFAULT_CHANNEL), now)
            count += 1
        if count:
            db.execute('UPDATE sessions SET dm_observation_revision=dm_observation_revision+1 WHERE id=?', (session,))
        return count

    def ingest(self, session, batch, now):
        integer(now, 'now_ms')
        require(isinstance(batch,dict),'invalid_ingest_document')
        encode(batch)
        require(batch.get('read_ok') is True, 'read_failed')
        with self.connect(True, operation='ingest', session=session) as db:
            session_row(db, session)
            count = self._ingest(db, session, batch.get('messages', []), now)
            if count:
                bump(db, session)
                if all(isinstance(m, dict) and m.get('direction') == 'incoming' for m in batch.get('messages', [])):
                    revision = session_row(db, session)['revision']
                    db.execute('INSERT OR REPLACE INTO revision_sources VALUES (?,?,?)',
                               (session, revision, 'incoming_dm'))
                    db.execute('DELETE FROM revision_sources WHERE session_id=? AND revision<?',
                               (session, revision - 500))
            return {'ok': True, 'reason': 'ingested', 'inserted': count,
                    'revision': session_row(db, session)['revision']}

    def _fact(self, db, session, f, now):
        world_events.guard_fact(db,session,f)
        fid = text(f['id'], 'fact_id')
        require(not fid.startswith(memory_continuity.PREFIX), 'memory_fact_owned')
        source = f['source']
        require(source in ('canon', 'observed', 'simulated', 'proposed'), 'invalid_fact_source')
        summary = text(f['summary'])
        details = f.get('details', {})
        if source in ('canon', 'observed'):
            text(details.get('evidence'), 'fact_evidence')
        occurred = f.get('occurred_ms')
        if occurred is not None:
            integer(occurred)
            require(occurred <= now or source == 'proposed', 'future_fact')
        old = db.execute('SELECT source,summary FROM facts WHERE session_id=? AND id=?', (session, fid)).fetchone()
        if old:
            require((old['source'], old['summary']) == (source, summary), 'fact_identity_collision')
            return
        db.execute('INSERT INTO facts VALUES (?,?,?,?,?,?,?)',
                   (session, fid, occurred, now, source, summary, encode(details)))

    def tick(self, session, now, after=0, limit=50, view='full', known_core_rules_hash=None):
        integer(now, 'now_ms'); integer(after, 'cursor'); integer(limit, 'limit', 1)
        require(limit <= 200, 'limit_too_large')
        require(view in decision_routing.VIEWS, 'invalid_tick_view')
        require(known_core_rules_hash is None or (isinstance(known_core_rules_hash,str) and
                re.fullmatch(r'[0-9a-f]{64}',known_core_rules_hash)), 'invalid_core_rules_hash')
        compact = view in ('dm', 'gate')
        with self.connect() as db:
            state = session_row(db, session)
            event_columns = 'id,kind' if compact else '*'
            events = db.execute(f'''SELECT rowid AS cursor,{event_columns} FROM input_events
                WHERE session_id=? AND processed_ms IS NULL AND rowid>? ORDER BY rowid LIMIT ?''',
                (session, after, limit+1)).fetchall()
            activity = rowdict(db.execute("SELECT * FROM activities WHERE session_id=? AND status='in_progress'", (session,)).fetchone())
            today = datetime.fromtimestamp(now/1000, KST).date()
            monday = (today-timedelta(days=today.weekday())).isoformat()
            plan_columns = 'week_start' if compact else '*'
            plan = rowdict(db.execute(f'SELECT {plan_columns} FROM weekly_plans WHERE session_id=? AND week_start=?', (session, monday)).fetchone())
            # Confirmed plans stay visible a week ahead so later decisions do not schedule over
            # them; tentative proposals keep the 24h window.
            appointments = [rowdict(r) for r in db.execute('''SELECT * FROM appointments WHERE session_id=?
                AND end_ms>? AND ((status='confirmed' AND start_ms<?) OR (status='proposed' AND start_ms<?))
                ORDER BY start_ms LIMIT ?''',
                (session, now, now+APPOINTMENT_CONFIRMED_WINDOW_MS, now+APPOINTMENT_PROPOSED_WINDOW_MS,
                 APPOINTMENT_TICK_LIMIT))]
            interaction = in_person.snapshot(db,session,now)
            appointment_work = appointment_lifecycle.view(db,session,now,activity)
            extra_ids = set(appointment_work['review_ids'])
            if interaction['active']:
                extra_ids.add(interaction['appointment_id'])
            extra_ids -= {a['id'] for a in appointments}
            for aid in sorted(extra_ids):
                appointments.append(rowdict(db.execute('SELECT * FROM appointments WHERE session_id=? AND id=?',
                                                       (session,aid)).fetchone()))
            jobs = [rowdict(r) for r in db.execute('''SELECT j.*,f.passed_ms AS flow_passed_ms
                FROM dm_jobs j LEFT JOIN dm_flow_passes f ON f.session_id=j.session_id AND f.job_id=j.id
                WHERE j.session_id=? AND j.status NOT IN ('confirmed','cancelled') ORDER BY j.due_ms,j.id''', (session,))]
            for j in jobs: j['channel'] = group_channel(db, session, j['group_id'])
            channel_blocks = {r['channel']: r['blocked_until_ms'] for r in db.execute(
                'SELECT * FROM channel_blocks WHERE session_id=? AND blocked_until_ms>?', (session, now))}
            channel_pacing = {r['channel']: r['next_send_after_ms'] for r in db.execute(
                'SELECT * FROM channel_pacing WHERE session_id=?', (session,))}
            messages = [rowdict(r) for r in db.execute("SELECT * FROM dm_messages WHERE session_id=? AND status<>'handled' ORDER BY observed_ms,id", (session,))]
            if view == 'gate':
                intents = []
                intent_due_ids = [r['id'] for r in db.execute('''SELECT id,review_at_ms,expires_ms
                    FROM action_intents WHERE session_id=? AND status='pending' ORDER BY review_at_ms,id''',
                    (session,)) if action_intents.effective_due(r,activity,now)<=now]
            else:
                intents = action_intents.snapshot(db,session,now)
                intent_due_ids = [i['id'] for i in intents if i['status']=='pending' and i['effective_review_ms']<=now]
            continuation_due_ids = continuations.due_ids(db, session, now)
            morning = morning_contact.snapshot(db,session,now)
            proactive_group_ids = [r[0] for r in db.execute(PROACTIVE_ACTIVE_SQL,(session,))]
            unanswered = any(m['status'] in ('received','deferred','queued') for m in messages)
            proactive = proactive_check.snapshot(
                db, session, now, activity=activity, unanswered_incoming=unanswered,
                proactive_group_ids=proactive_group_ids,
                stored_after_ms=state.get('proactive_check_after_ms'))
            reasons = []
            if morning['due']: reasons.append('morning_contact_due')
            if proactive['due']: reasons.append('proactive_check_due')
            if intent_due_ids or continuation_due_ids: reasons.append('decision_due')
            if events: reasons.append('unprocessed_events')
            if not plan: reasons.append('weekly_plan_missing')
            if not activity: reasons.append('no_current_activity')
            elif activity['planned_end_ms'] <= now: reasons.append('activity_due')
            if state['next_decision_ms'] is not None and state['next_decision_ms'] <= now and 'decision_due' not in reasons: reasons.append('decision_due')
            if any(m['status']=='deferred' and m['reply_due_ms']<=now for m in messages): reasons.append('reply_due')
            if appointment_work['appointment_due']: reasons.append('appointment_due')
            if appointment_work['preparations']: reasons.append('appointment_prepare_due')
            if appointment_work['review_ids']: reasons.append('appointment_review_due')
            draft_review_groups = []
            stale_groups = []
            if not activity or activity['attention'] != 'unavailable':
                draft_review_groups = [r[0] for r in db.execute('''
                SELECT DISTINCT g.id FROM dm_jobs j JOIN reply_groups g ON g.session_id=j.session_id AND g.id=j.group_id
                LEFT JOIN group_reviews gr ON gr.session_id=g.session_id AND gr.group_id=g.id
                JOIN activities a ON a.session_id=g.session_id AND a.attention='unavailable'
                WHERE j.session_id=? AND j.status='pending' AND g.status='active'
                  AND a.status IN ('completed','interrupted')
                  AND a.start_ms>=COALESCE(gr.reviewed_ms,g.created_ms)
                ORDER BY g.id''',(session,))]
                stale_groups = [r[0] for r in db.execute('''
                SELECT DISTINCT g.id FROM reply_groups g JOIN dm_jobs j
                  ON j.session_id=g.session_id AND j.group_id=g.id
                WHERE g.session_id=? AND g.status='active' AND j.status='pending'
                  AND g.observation_revision<>?
                  AND NOT EXISTS (SELECT 1 FROM dm_jobs blocked
                    WHERE blocked.session_id=g.session_id AND blocked.group_id=g.id
                      AND blocked.status='attempting')
                ORDER BY g.id''',(session,state['dm_observation_revision']))]
            if draft_review_groups:
                reasons.append('draft_needs_review')
            if not activity or activity['attention'] != 'unavailable':
                stale_groups = sorted(set(stale_groups) | set(in_person.stale_groups(db,session)))
            if stale_groups:
                reasons.append('draft_stale')
            rules_path=Path(__file__).parent/'CORE_RULES.md'
            if not rules_path.is_file():
                rules_path=Path(__file__).parent.parent/'CORE_RULES.md'
            rules=rules_path.read_text(encoding='utf-8') if rules_path.is_file() else ''
            snapshot = {'ok': True, 'reason': 'snapshot', 'revision': state['revision'], 'now_ms': now,
                'core_rules':rules, 'core_rules_hash':hashlib.sha256(rules.encode()).hexdigest(),
                'core_rules_path':str(rules_path.resolve()),
                'core_rules_changed':known_core_rules_hash != hashlib.sha256(rules.encode()).hexdigest(),
                'morning_contact':morning,
                'proactive_check':proactive,
                'weekly_plan_present':bool(plan),
                'state': state, 'activity': activity, 'weekly_plan': plan, 'appointments': appointments,
                'interaction':interaction,'appointment_lifecycle':appointment_work,
                'action_intents': intents, 'action_intent_due_ids': intent_due_ids,
                'continuations': continuations.snapshot(db,session),
                'continuation_due_ids': continuation_due_ids,
                'action_intent_followups': action_intents.followups(db,session,now) if view!='gate' else [],
                'conversation_policy': conversation_flow.policy(db,session) if view!='gate' else {},
                'events': [rowdict(r) for r in events[:limit]], 'has_more': len(events)>limit,
                'next_cursor': events[min(len(events),limit)-1]['cursor'] if events else after,
                'messages': messages, 'jobs': jobs, 'needs_decision': reasons,
                'draft_review_group_ids': draft_review_groups,
                'draft_stale_group_ids': stale_groups,
                'send_blocked': (state['send_blocked_until_ms'] or 0)>now,
                'dispatch_hold': any(j['status']=='attempting' for j in jobs),
                'delivery_uncertain_job_ids': [j['id'] for j in jobs if j['status'] in ('submitted','unknown')],
                'proactive_group_ids': proactive_group_ids,
                'conversation_mode': conversation_mode(db,session,now),
                'channel_blocked_until_ms': channel_blocks,
                'channel_next_send_after_ms': channel_pacing,
                'conversation_lease_until_ms': (lambda r: r[0] if r and r[0]>now else None)(
                    db.execute('SELECT until_ms FROM conversation_leases WHERE session_id=?',(session,)).fetchone()),
                'conversation_lease_owner': (lambda r: r[1] if r and r[0]>now else None)(
                    db.execute('SELECT until_ms,owner FROM conversation_leases WHERE session_id=?',(session,)).fetchone())}

            snapshot['work_manifest'] = decision_routing.work_manifest(snapshot,now)
            if view in ('full','life') or (view=='dm' and snapshot['work_manifest']['life_required']):
                snapshot['activity_history'] = activity_history.snapshot(db,session,now)
            if view!='gate':
                snapshot['world_events'] = world_events.snapshot(db,session,now,candidates=view in ('full','life'))
            if view != 'gate':
                memory_policy = memory_continuity.policy(db, session)
                if memory_policy['mode'] != 'off':
                    snapshot['memory_policy'] = memory_policy
                if memory_policy['mode'] == 'enforce':
                    snapshot['memory_context'] = memory_continuity.snapshot(db, session)
            return decision_routing.project(snapshot,view)

    def _weekly(self, db, session, plan, now):
        monday = date.fromisoformat(plan['week_start'])
        require(monday.weekday() == 0, 'week_must_start_monday')
        remote = plan.get('remote_date')
        if remote:
            day = date.fromisoformat(remote)
            require(monday <= day < monday+timedelta(days=5), 'invalid_remote_day')
        reason = text(plan['reason'])
        seed = text(plan['seed'])
        old = db.execute('SELECT * FROM weekly_plans WHERE session_id=? AND week_start=?', (session,monday.isoformat())).fetchone()
        if remote and (not old or old['remote_date'] != remote):
            require(date.fromisoformat(remote)>=datetime.fromtimestamp(now/1000,KST).date(), 'remote_day_in_past')
        if old and old['remote_date'] != remote:
            text(plan.get('change_reason'), 'change_reason')
        db.execute('''INSERT INTO weekly_plans VALUES (?,?,?,?,?) ON CONFLICT(session_id,week_start)
            DO UPDATE SET remote_date=excluded.remote_date,reason=excluded.reason''', (session,monday.isoformat(),remote,seed,reason))

    def _appointment(self, db, session, a):
        start, end = integer(a['start_ms']), integer(a['end_ms'])
        require(end > start, 'invalid_appointment_interval')
        require(a['status'] in ('proposed','confirmed','cancelled','completed','missed'), 'invalid_appointment_status')
        require(type(a['fixed']) is bool, 'invalid_fixed')
        appointment_lifecycle.milestones({'details_json':encode(a.get('details',{})),'start_ms':start})
        if 'review_at_ms' in a.get('details',{}):
            integer(a['details']['review_at_ms'],'appointment_review_at_ms')
        db.execute('''INSERT INTO appointments VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(session_id,id)
            DO UPDATE SET start_ms=excluded.start_ms,end_ms=excluded.end_ms,status=excluded.status,
            fixed=excluded.fixed,location=excluded.location,details_json=excluded.details_json''',
            (session,text(a['id']),start,end,a['status'],int(a['fixed']),text(a['location']),encode(a.get('details',{}))))

    def _activity(self, db, session, a, now):
        aid = text(a['id'])
        old = db.execute('SELECT * FROM activities WHERE session_id=? AND id=?', (session,aid)).fetchone()
        advanced = session_row(db,session)['advanced_to_ms']
        if old:
            require(old['status'] in ('planned','in_progress'), 'terminal_activity')
            allowed = {'id','status','actual_end_ms','planned_end_ms','reason'}
            require(set(a) <= allowed, 'activity_identity_immutable')
            status = a.get('status',old['status'])
            transitions = {'planned': ('planned','in_progress','cancelled'),
                           'in_progress': ('in_progress','completed','interrupted')}
            require(status in transitions[old['status']], 'invalid_activity_transition')
            end = a.get('actual_end_ms')
            planned = integer(a.get('planned_end_ms',old['planned_end_ms']))
            require(planned>old['start_ms'], 'invalid_activity_interval')
            if status in ('completed','interrupted'):
                integer(end)
                require(max(old['start_ms'],advanced) <= end <= now, 'invalid_activity_end')
            else:
                require(end is None, 'unexpected_actual_end')
            text(a.get('reason'), 'activity_change_reason')
            db.execute('UPDATE activities SET status=?,actual_end_ms=?,planned_end_ms=? WHERE session_id=? AND id=?', (status,end,planned,session,aid))
        else:
            start, end = integer(a['start_ms']), integer(a['planned_end_ms'])
            require(start>=advanced, 'activity_rewrites_history')
            require(end>start, 'invalid_activity_interval')
            status = a.get('status','in_progress')
            require(status in ('planned','in_progress','completed'), 'invalid_new_activity_status')
            actual = a.get('actual_end_ms')
            if status == 'completed':
                integer(actual); require(start <= actual <= now, 'invalid_activity_end')
            else:
                require(actual is None, 'unexpected_actual_end')
            details = dict(a.get('details',{}))
            text(details.get('reason'), 'activity_reason')
            rates = details.get('rates_per_hour', {})
            require(set(rates)<=set(METRICS), 'invalid_rates')
            for v in rates.values(): require(type(v) in (float,int) and -100<=v<=100, 'invalid_rate')
            db.execute('INSERT INTO activities VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (session,aid,text(a['category']),text(a['label']),status,start,end,actual,
                 text(a['location']),a.get('destination'),a.get('attention','light'),
                 a.get('interruptibility','anytime'),a.get('visibility','personal'),'simulated',encode(details)))

    def _validate_timeline(self, db, session, now, old_state):
        rows = [dict(r) for r in db.execute("SELECT * FROM activities WHERE session_id=? AND status<>'cancelled' ORDER BY start_ms,id",(session,))]
        previous = None
        for r in rows:
            if r['status']=='in_progress':
                require(r['start_ms']<=now<r['planned_end_ms'], 'active_activity_not_current')
            if previous:
                end_prev = previous['actual_end_ms'] if previous['actual_end_ms'] is not None else previous['planned_end_ms']
                require(end_prev<=r['start_ms'], 'activity_overlap')
                origin = previous['destination'] if previous['location']=='in_transit' else previous['location']
                require(r['location'] in ('in_transit',origin) or origin=='unknown', 'missing_travel')
            elif r['start_ms']>=old_state['advanced_to_ms']:
                require(r['location'] in ('in_transit',old_state['location']) or old_state['location']=='unknown', 'missing_travel')
            if r['location']=='in_transit': text(r['destination'], 'destination')
            previous = r
        appointments = list(db.execute("SELECT * FROM appointments WHERE session_id=? AND status='confirmed' ORDER BY start_ms",(session,)))
        for left,right in zip(appointments,appointments[1:]):
            require(left['end_ms']<=right['start_ms'], 'appointment_overlap')
        for r in rows:
            if r['status'] not in ('planned','in_progress'): continue
            linked = decode(r['details_json']).get('appointment_id')
            for a in appointments:
                if a['fixed'] and r['start_ms']<a['end_ms'] and r['planned_end_ms']>a['start_ms']:
                    require((linked==a['id'] and r['location']==a['location']) or
                            in_person.approaching(r,a) or
                            in_person.mobile_activity(db,session,r,a),'fixed_appointment_conflict')

    def _dm_decision(self, db, session, d, state, now, interrupted_sleep_due=None, conversation_refs=None):
        action = d['action']
        morning_contact.validate_tag(d)
        if action in ('reply','initiate','revalidate_group'):
            active = db.execute("SELECT attention FROM activities WHERE session_id=? AND status='in_progress'", (session,)).fetchone()
            require(active is None or active['attention'] != 'unavailable', 'activity_unavailable')
        if action == 'resolve_message':
            mid=text(d['message_id'])
            r=db.execute('SELECT * FROM dm_messages WHERE session_id=? AND id=?',(session,mid)).fetchone()
            require(r is not None and r['status'] in ('received','deferred'), 'message_not_reviewable')
            info=decode(r['observation_json'])
            text(d.get('evidence'), 'review_evidence')
            require(d.get('identity') in ('platform','verified_composite'), 'invalid_resolved_identity')
            info.update(review_required=False, identity=d['identity'], review_evidence=d['evidence'])
            db.execute('UPDATE dm_messages SET observation_json=? WHERE session_id=? AND id=?',(encode(info),session,mid))
            return
        if action in ('cancel_group','revalidate_group','close_partial_group'):
            gid = text(d['group_id'])
            require(continuations.owner(db,session,gid) is None, 'continuation_group_owned')
            group = db.execute('SELECT * FROM reply_groups WHERE session_id=? AND id=?',(session,gid)).fetchone()
            require(group is not None and group['status']=='active', 'group_not_active')
            jobs = list(db.execute('SELECT * FROM dm_jobs WHERE session_id=? AND group_id=?',(session,gid)))
            require(not any(j['status']=='attempting' for j in jobs),'group_dispatch_unresolved')
            passed = {r[0] for r in db.execute('''SELECT f.job_id FROM dm_flow_passes f JOIN dm_jobs j
                ON j.session_id=f.session_id AND j.id=f.job_id WHERE f.session_id=? AND j.group_id=?''',(session,gid))}
            text(d.get('reason'), 'group_change_reason')
            if action == 'revalidate_group':
                parts = [j['text'] for j in sorted(jobs, key=lambda job: job['part_index'])]
                target_ids = [r['message_id'] for r in db.execute(
                    'SELECT message_id FROM reply_targets WHERE session_id=? AND group_id=? ORDER BY message_id',
                    (session,gid))]
                claims = validate_conversation_review(db, session, d, parts, target_ids, group_channel(db,session,gid))
                checked = conversation_flow.validate_plan(db,session,d,parts,target_ids,now,
                                                          conversation_refs,revalidate=True)
                for job in jobs:
                    if job['status'] in ('pending', 'failed'):
                        self._set_job_claims(db, session, job['id'], job['text'], claims)
                db.execute('UPDATE reply_groups SET observation_revision=? WHERE session_id=? AND id=?',(state['dm_observation_revision'],session,gid))
                in_person.capture_group(db,session,gid,group_channel(db,session,gid))
                db.execute('''INSERT INTO group_reviews(session_id,group_id,reviewed_ms) VALUES (?,?,?)
                    ON CONFLICT(session_id,group_id) DO UPDATE SET reviewed_ms=excluded.reviewed_ms''',(session,gid,now))
                conversation_flow.reserve(db,session,gid,checked,now)
            elif action == 'close_partial_group':
                require(bool(passed), 'group_not_partially_sent')
                db.execute("UPDATE dm_jobs SET status='cancelled',updated_ms=? WHERE session_id=? AND group_id=? AND status IN ('pending','failed')",(now,session,gid))
                db.execute("UPDATE reply_groups SET status='completed' WHERE session_id=? AND id=?",(session,gid))
                db.execute("UPDATE dm_messages SET status='handled',reply_due_ms=NULL WHERE session_id=? AND id IN (SELECT message_id FROM reply_targets WHERE session_id=? AND group_id=?)",(session,session,gid))
                db.execute('UPDATE reply_targets SET active=0 WHERE session_id=? AND group_id=?',(session,gid))
            else:
                # A flow-passed group must not replay the source, even without a receipt.
                require(not passed, 'partially_sent_group')
                db.execute("UPDATE dm_jobs SET status='cancelled',updated_ms=? WHERE session_id=? AND group_id=? AND status IN ('pending','failed')",(now,session,gid))
                db.execute("UPDATE reply_groups SET status='cancelled' WHERE session_id=? AND id=?",(session,gid))
                db.execute('UPDATE reply_targets SET active=0 WHERE session_id=? AND group_id=?',(session,gid))
                db.execute("UPDATE dm_messages SET status='received' WHERE session_id=? AND id IN (SELECT message_id FROM reply_targets WHERE session_id=? AND group_id=?)",(session,session,gid))
            conversation_flow.sync_group(db,session,gid,now)
            return
        if action == 'initiate':
            # Proactive DM: a reply group that has no reply_targets rows at all.
            for retired in morning_contact.retire_failed_groups(db,session,d,now):
                conversation_flow.sync_group(db,session,retired,now)
            require('message_ids' not in d, 'initiate_has_targets')
            gid = text(d['group_id'])
            parts = d['parts']
            require(isinstance(parts,list) and bool(parts),'invalid_parts')
            require(len(parts) <= MAX_PARTS, 'parts_limit_exceeded')
            due = max(integer(d['due_ms']), now)   # a past due_ms means "as soon as possible"
            text(d.get('reason'), 'dm_reason')
            # Pending incoming messages must be answered (reply/defer->reply/no_reply) first.
            run_id = (d.get('continuation') or {}).get('id')
            # Only targets already owned by this run may coexist with its lookahead.
            require(db.execute("""SELECT 1 FROM dm_messages m WHERE session_id=?
                AND status IN ('received','deferred','queued') AND NOT EXISTS (
                    SELECT 1 FROM reply_targets t JOIN continuation_groups c
                    ON c.session_id=t.session_id AND c.group_id=t.group_id
                    WHERE t.session_id=m.session_id AND t.message_id=m.id AND t.active=1 AND c.run_id=?)""",
                (session,run_id)).fetchone() is None, 'unanswered_incoming_exists')
            active = db.execute(PROACTIVE_ACTIVE_SQL,(session,)).fetchall()
            owners = [continuations.owner(db,session,g['id']) for g in active]
            require(all(run_id is not None and r is not None and r['id']==run_id for r in owners),
                    'initiate_already_active')
            channel = valid_channel(d.get('channel', persona.DEFAULT_CHANNEL))
            claims = validate_conversation_review(db, session, d, parts, [], channel)
            checked = conversation_flow.validate_plan(db,session,d,parts,[],now,conversation_refs)
            db.execute("INSERT INTO reply_groups VALUES (?,?,?,'active',?)",(session,gid,state['dm_observation_revision'],now))
            db.execute('INSERT INTO group_reviews VALUES (?,?,?)',(session,gid,now))
            db.execute('INSERT INTO group_channels VALUES (?,?,?)',(session,gid,channel))
            in_person.capture_group(db,session,gid,channel)
            self._insert_jobs(db,session,gid,parts,due,now,[],claims)
            conversation_flow.reserve(db,session,gid,checked,now)
            return
        ids = d.get('message_ids',[])
        require(bool(ids) and len(ids)==len(set(ids)), 'invalid_reply_targets')
        rows = []
        for mid in ids:
            r = db.execute('SELECT * FROM dm_messages WHERE session_id=? AND id=?',(session,mid)).fetchone()
            require(r is not None and r['status'] not in ('handled','queued'), 'message_not_replyable')
            info = decode(r['observation_json'])
            require(info['direction']=='incoming' and not info.get('review_required'), 'message_requires_review')
            rows.append(r)
        text(d.get('reason'), 'dm_reason')
        if action=='defer':
            due = integer(d['due_ms']); require(due>now,'reply_due_not_future')
            for r in rows:
                db.execute("UPDATE dm_messages SET status='deferred',reply_due_ms=? WHERE session_id=? AND id=?",(due,session,r['id']))
        elif action=='no_reply':
            for r in rows:
                db.execute("UPDATE dm_messages SET status='handled',reply_due_ms=NULL WHERE session_id=? AND id=?",(session,r['id']))
        elif action=='reply':
            gid = text(d['group_id'])
            parts = d['parts']
            require(isinstance(parts,list) and bool(parts),'invalid_parts')
            require(len(parts) <= MAX_PARTS, 'parts_limit_exceeded')
            due = integer(d['due_ms'])
            # Never earlier than a deferred reply time; a past due_ms otherwise means "as soon as possible".
            prior_due = [r['reply_due_ms'] or 0 for r in rows
                         if not (interrupted_sleep_due and message_channel(r) == 'slack'
                                 and r['reply_due_ms'] == interrupted_sleep_due)]
            require(due>=max([0]+prior_due),'reply_due_advanced')
            due = max(due, now)
            # A reply goes back to the messenger the messages came from.
            channels = {message_channel(r) for r in rows}
            require(len(channels)==1, 'mixed_channel_targets')
            channel = channels.pop()
            if 'channel' in d: require(valid_channel(d['channel'])==channel, 'channel_mismatch')
            claims = validate_conversation_review(db, session, d, parts, ids, channel)
            checked = conversation_flow.validate_plan(db,session,d,parts,ids,now,conversation_refs)
            db.execute("INSERT INTO reply_groups VALUES (?,?,?,'active',?)",(session,gid,state['dm_observation_revision'],now))
            db.execute('INSERT INTO group_reviews VALUES (?,?,?)',(session,gid,now))
            db.execute('INSERT INTO group_channels VALUES (?,?,?)',(session,gid,channel))
            in_person.capture_group(db,session,gid,channel)
            for r in rows:
                db.execute('INSERT INTO reply_targets VALUES (?,?,?,1)',(session,gid,r['id']))
                db.execute("UPDATE dm_messages SET status='queued',reply_due_ms=? WHERE session_id=? AND id=?",(due,session,r['id']))
            self._insert_jobs(db,session,gid,parts,due,now,ids,claims)
            conversation_flow.reserve(db,session,gid,checked,now)
        else:
            raise ControlError('invalid_dm_action')

    def _set_job_claims(self, db, session, job_id, body, claims):
        db.execute('DELETE FROM job_self_claims WHERE session_id=? AND job_id=?', (session, job_id))
        for i, claim in enumerate(claims):
            if claim['excerpt'] in body:
                db.execute('INSERT INTO job_self_claims VALUES (?,?,?,?,?)',
                           (session, job_id, i, claim['fact_id'], claim['excerpt']))

    def _insert_jobs(self, db, session, gid, parts, due, now, ids, claims=()):
        if len(parts) > 3:
            schema = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='dm_jobs'").fetchone()[0]
            require(LEGACY_CHECK.search(schema) is None, 'parts_schema_migration_required')
        for i,body in enumerate(parts):
            key = gid+':'+str(i)
            db.execute('''INSERT INTO dm_jobs(session_id,id,dedupe_key,status,recipient,text,
                created_ms,updated_ms,due_ms,group_id,part_index) VALUES (?,?,?,'pending','user',?,?,?,?,?,?)''',
                (session,key,key,text(body),now,now,due,gid,i))
            self._set_job_claims(db,session,key,body,claims)
            for mid in ids: db.execute('INSERT INTO dm_job_messages VALUES (?,?,?)',(session,key,mid))
    def _set_lease(self, session, until, now, owner=None):
        with self.connect(True) as db:
            session_row(db,session)
            db.execute('''INSERT INTO conversation_leases(session_id,until_ms,started_ms,waits,owner) VALUES (?,?,?,1,?)
                ON CONFLICT(session_id) DO UPDATE SET until_ms=excluded.until_ms,waits=waits+1,owner=excluded.owner,
                started_ms=CASE WHEN conversation_leases.until_ms<=? THEN excluded.started_ms ELSE conversation_leases.started_ms END''',
                (session,until,now,owner,now))

    def _renew_wait_lease(self, session, until):
        with self.connect(True) as db:
            db.execute('UPDATE conversation_leases SET until_ms=? WHERE session_id=?', (until,session))

    def end_conversation(self, session, now):
        integer(now,'now_ms')
        with self.connect(True) as db:
            session_row(db,session)
            db.execute('DELETE FROM conversation_leases WHERE session_id=?',(session,))
            return {'ok':True,'reason':'conversation_ended','revision':session_row(db,session)['revision']}

    def release_lease(self, session, owner, now):
        """Drop the conversation lease only if `owner` (an ended wake) set it. Never touches
        a lease owned by another wake or a legacy owner-less wait."""
        integer(now,'now_ms')
        require(isinstance(owner,str) and re.fullmatch(r'[A-Za-z0-9_:-]{1,80}',owner) is not None,'invalid_lease_owner')
        with self.connect(True) as db:
            session_row(db,session)
            row = db.execute('SELECT until_ms,owner FROM conversation_leases WHERE session_id=?',(session,)).fetchone()
            released = row is not None and row['owner'] == owner
            if released:
                db.execute('DELETE FROM conversation_leases WHERE session_id=? AND owner=?',(session,owner))
            received = db.execute("SELECT COUNT(*) FROM dm_messages WHERE session_id=? AND status='received'",(session,)).fetchone()[0]
            return {'ok':True,'reason':'lease_released' if released else 'lease_not_owned' if row is not None else 'no_lease',
                    'released':released,'was_live':bool(released and row['until_ms']>now),
                    'received_count':received,'revision':session_row(db,session)['revision']}

    def wait_dm(self, session, timeout_s, clock, sleep, poll_s=2, owner_guard=None, *, lease_deadline_ms=None,
                lease_owner=None):
        """Blocks (no model tokens) until a new incoming DM or a due life decision.
        Holds the conversation lease meanwhile so the gate does not start a second Muse run."""
        integer(timeout_s,'timeout',1); require(timeout_s<=900,'timeout_too_large')
        start=clock(); deadline=start+timeout_s*1000
        if lease_deadline_ms is not None:
            integer(lease_deadline_ms, 'lease_deadline_ms')
            require(lease_deadline_ms >= deadline, 'invalid_lease_deadline')
        def lease_until(until):
            return min(until, lease_deadline_ms) if lease_deadline_ms is not None else until
        guard = owner_guard or nullcontext
        with guard():
            self._set_lease(session,lease_until(start+WAIT_LEASE_MS),start,lease_owner)
        renewed = start
        first_new = None
        while True:
            with guard():
                now=clock()
                if now-renewed>=WAIT_LEASE_RENEW_MS and now<deadline:
                    self._renew_wait_lease(session,lease_until(now+WAIT_LEASE_MS))
                    renewed=now
                snap=self.tick(session,now,0,1,view='gate')
                new=[m['id'] for m in snap['messages'] if m['status']=='received']
                settling = 0
                if new:
                    first_new = now if first_new is None else first_new
                    if now-first_new < INCOMING_SETTLE_CAP_MS and now < deadline:
                        settling = incoming_settle_remaining_ms(snap['messages'], now)
            if settling:
                sleep(min(poll_s, settling/1000))
                continue
            with guard():
                needs=[r for r in WAIT_WAKE_NEEDS if r in snap['needs_decision']]
                reason=('new_dm' if new else 'decision_needed' if needs
                        else 'timeout' if now>=deadline else None)
                if reason:
                    if reason=='timeout':
                        self.end_conversation(session,now)   # session over; the gate takes over again
                    else:
                        self._set_lease(session,lease_until(now+LEASE_GRACE_MS),now,lease_owner)   # Muse is composing
                    return {'ok':True,'reason':reason,'revision':snap['revision'],'waited_ms':now-start,
                            'new_message_ids':new,'needs_decision':needs,
                            'settle_waited_ms':now-first_new if first_new is not None else 0,
                            'conversation_mode':snap['conversation_mode']}
            sleep(min(poll_s,max(0.0,(deadline-now)/1000)))

    def apply(self, session, d, now, dry_run=False, guard_warnings=False, warning_review=None, telemetry=None,
              extra_warnings=None):
        require(isinstance(d,dict),'invalid_decision_document')
        allowed={'decision_id','expected_revision','advance_to_ms','next_decision_ms',
                 'weekly_plans','appointments','activities','metric_deltas','metric_reason',
                 'facts','relationships','dm_decisions','processed_event_ids','action_intents',
                 'conversation_updates','continuation_ops','world_event_ops','memory_ops','in_person_ops'}
        require(set(d)<=allowed, 'unknown_decision_field')
        encode(d); integer(now,'now_ms')
        decision_id = text(d['decision_id'])
        digest = hashlib.sha256(encode(d).encode()).hexdigest()
        if telemetry is not None:
            telemetry.update(instrumentation.decision_metadata(d), warning_codes=[])
        with self.connect(True, operation='decision_apply', session=session) as db:
            state = session_row(db,session)
            if telemetry is not None:
                telemetry['actual_revision'] = state['revision']
            old = db.execute('SELECT digest FROM decisions WHERE session_id=? AND id=?',(session,decision_id)).fetchone()
            if old:
                require(old['digest']==digest,'decision_identity_collision')
                return {'ok':True,'reason':'already_applied','revision':state['revision']}
            expected = integer(d['expected_revision'])
            # Messages that arrived while composing are answered by the next turn, not by
            # discarding this one. Any other change since preparation is still a conflict.
            late_incoming = expected != state['revision']
            require(not late_incoming or only_incoming_since(db, session, expected, state['revision']),
                    'revision_conflict')
            advance = integer(d['advance_to_ms'])
            submitted_next = integer(d['next_decision_ms'])
            # Clock fields are normalized against the commit clock (handoff
            # 2026-10-26 §2A): the model picks an intended advance and review
            # gap at prepare time, and stage latency must not turn them into
            # rejections. Duplicate identity above used the submitted document,
            # so a re-submit at a later clock still matches. Moving time
            # backwards is never corrected.
            normalized = {}
            if advance > now:
                normalized['advance_to_ms'] = {'from': advance, 'to': now}
                advance = now
            require(state['advanced_to_ms'] <= advance, 'invalid_advance_time')
            next_base = submitted_next
            if submitted_next <= now:
                next_base = now + max(submitted_next - integer(d['advance_to_ms']),
                                      NEXT_DECISION_MIN_MS)
                normalized['next_decision_ms'] = {'from': submitted_next, 'to': next_base}
            old_activity = db.execute("SELECT * FROM activities WHERE session_id=? AND status='in_progress'",(session,)).fetchone()
            pre_unanswered = db.execute(
                "SELECT 1 FROM dm_messages WHERE session_id=? AND status IN ('received','deferred','queued')",
                (session,)).fetchone() is not None
            pre_proactive_ids = [r[0] for r in db.execute(PROACTIVE_ACTIVE_SQL,(session,))]
            pre_proactive_due = proactive_check.snapshot(
                db, session, now, activity=old_activity, unanswered_incoming=pre_unanswered,
                proactive_group_ids=pre_proactive_ids,
                stored_after_ms=state.get('proactive_check_after_ms'))['due']
            interrupted_sleep_due = None
            if old_activity and old_activity['attention'] == 'unavailable' and (
                    old_activity['category'] in ('sleep','수면') or
                    any(word in old_activity['label'] for word in ('수면','취침','잠'))):
                if any(a.get('id') == old_activity['id'] and a.get('status') == 'interrupted'
                       for a in d.get('activities', [])):
                    interrupted_sleep_due = old_activity['planned_end_ms']
            for p in d.get('weekly_plans',[]): self._weekly(db,session,p,now)
            for a in d.get('appointments',[]): self._appointment(db,session,a)
            for a in d.get('activities',[]): self._activity(db,session,a,advance)
            in_person.apply_ops(db,session,d.get('in_person_ops',[]),advance)
            in_person.validate(db,session)
            self._validate_timeline(db,session,advance,state)
            values = {k:state[k] for k in METRICS}
            rows = list(db.execute("SELECT * FROM activities WHERE session_id=? AND status IN ('in_progress','completed','interrupted') ORDER BY start_ms",(session,)))
            for r in rows:
                end = r['actual_end_ms'] if r['actual_end_ms'] is not None else r['planned_end_ms']
                hours = max(0,min(advance,end)-max(state['advanced_to_ms'],r['start_ms']))/3600000
                for k,rate in decode(r['details_json']).get('rates_per_hour',{}).items():
                    values[k]=max(0,min(100,values[k]+hours*rate))
            deltas = d.get('metric_deltas',{})
            require(set(deltas)<=set(METRICS),'invalid_metrics')
            if deltas: text(d.get('metric_reason'),'metric_reason')
            for k,v in deltas.items():
                require(type(v) in (int,float) and -100<=v<=100,'invalid_metric_delta')
                values[k]=max(0,min(100,values[k]+v))
            location = state['location']
            for r in rows:
                if r['start_ms']<=advance:
                    location = r['location']
                    if location=='in_transit' and r['actual_end_ms'] is not None and r['actual_end_ms']<=advance:
                        location = r['destination']
            previous_fact_ids = {f['id'] for f in d.get('facts',[]) if db.execute(
                'SELECT 1 FROM facts WHERE session_id=? AND id=?',(session,f['id'])).fetchone()}
            for f in d.get('facts',[]): self._fact(db,session,f,now)
            world_events.apply_ops(db,session,d.get('world_event_ops',[]),now,phase='events')
            conversation_refs = conversation_flow.apply_updates(db,session,d.get('conversation_updates',[]),now)
            for r in d.get('relationships',[]):
                require(bool(r.get('details',{}).get('evidence_ids')),'missing_relationship_evidence')
                for fid in r['details']['evidence_ids']:
                    require(db.execute('SELECT 1 FROM facts WHERE session_id=? AND id=?',(session,fid)).fetchone() is not None,'relationship_evidence_not_found')
                db.execute('''INSERT INTO relationships VALUES (?,?,?,?) ON CONFLICT(session_id,subject_id,other_id)
                    DO UPDATE SET details_json=excluded.details_json''',(session,text(r['subject_id']),text(r['other_id']),encode(r['details'])))
            delivery_flow.advance_session(db,session)
            continuations.apply_ops(db,session,d.get('continuation_ops',[]),now)
            continuations.maintain(db,session,now)
            for dm in d.get('dm_decisions',[]):
                run = continuations.prepare_group(db,session,dm,now)
                self._dm_decision(db,session,dm,state,now,interrupted_sleep_due,conversation_refs)
                morning_contact.attach(db,session,dm,now)
                if run is not None:
                    continuations.attach(db,session,dm,run,now)
            outbound = any(dm.get('action') in ('reply','initiate') for dm in d.get('dm_decisions',[]))
            if pre_proactive_due or outbound:
                proactive_check.advance(db, session, now)
            continuations.maintain(db,session,now)
            require(not continuations.due_ids(db,session,now), 'continuation_review_required')
            memory_results, memory_report = memory_continuity.apply_for_decision(db, session, d.get('memory_ops', []), now)
            if telemetry is not None and memory_report:
                telemetry.update(memory_status=memory_report['status'],
                                 memory_error_code=memory_report['error_code'],
                                 memory_operation_count=memory_report['operation_count'])
            world_events.apply_ops(db,session,d.get('world_event_ops',[]),now,phase='shares')
            action_intents.apply(db,session,d,now,old_activity,previous_fact_ids)
            for eid in d.get('processed_event_ids',[]):
                event=db.execute('SELECT * FROM input_events WHERE session_id=? AND id=?',(session,eid)).fetchone()
                require(event is not None,'event_not_found')
                if event['kind']=='dm_received':
                    mid=decode(event['payload_json'])['message_id']
                    message=db.execute('SELECT status FROM dm_messages WHERE session_id=? AND id=?',(session,mid)).fetchone()
                    require(message['status']!='received', 'dm_event_without_decision')
                db.execute('UPDATE input_events SET processed_ms=COALESCE(processed_ms,?) WHERE session_id=? AND id=?',(now,session,eid))
            next_time = next_base
            due_times = [r[0] for r in db.execute("SELECT planned_end_ms FROM activities WHERE session_id=? AND status='in_progress'",(session,))]
            due_times += [r[0] for r in db.execute("SELECT reply_due_ms FROM dm_messages WHERE session_id=? AND status='deferred' AND reply_due_ms>?",(session,now))]
            due_times += [r[0] for r in db.execute("SELECT start_ms FROM appointments WHERE session_id=? AND status='confirmed' AND start_ms>?",(session,now))]
            current_activity = db.execute("SELECT * FROM activities WHERE session_id=? AND status='in_progress'",(session,)).fetchone()
            appointment_boundary = appointment_lifecycle.view(db,session,now,current_activity)['next_boundary_ms']
            if appointment_boundary is not None: due_times.append(appointment_boundary)
            intent_due = action_intents.next_due(db,session,now)
            if intent_due is not None: due_times.append(intent_due)
            if due_times: next_time=min(next_time,*due_times)
            db.execute('''UPDATE sessions SET advanced_to_ms=?,next_decision_ms=?,location=?,
                fatigue=?,mood=?,hunger=?,social_fatigue=?,revision=revision+1 WHERE id=?''',
                (advance,next_time,location,*(values[k] for k in METRICS),session))
            db.execute('INSERT INTO decisions VALUES (?,?,?,?)',(session,decision_id,digest,now))
            if memory_report:
                db.execute('INSERT INTO continuity_diagnostics VALUES (?,?,?,?,?)',
                           (session,decision_id,now,memory_report['error_code'],memory_report['operation_count']))
            fact_details = {'decision': d}
            if normalized: fact_details['normalized'] = normalized
            self._fact(db,session,{'id':'decision:'+decision_id,'source':'simulated',
                'summary':'생활 판단 적용','details':fact_details},now)
            group_ids = [dm['group_id'] for dm in d.get('dm_decisions',[]) if 'group_id' in dm]
            plans = {}
            for gid in group_ids:
                row = db.execute('SELECT plan_json,warnings_json FROM conversation_groups WHERE session_id=? AND group_id=?',
                                 (session,gid)).fetchone()
                if row:
                    plans[gid] = {'plan':decode(row['plan_json']) if row['plan_json'] else None,
                                  'warnings':decode(row['warnings_json'])}
            result = {'ok':True,'reason':'conversation_preflight' if dry_run else 'applied',
                      'revision':state['revision'] if dry_run else state['revision']+1,
                      'conversation_refs':conversation_refs,'conversation_groups':plans}
            if normalized: result['normalized'] = normalized
            if late_incoming:
                result['late_incoming_ids'] = [r[0] for r in db.execute(
                    "SELECT id FROM dm_messages WHERE session_id=? AND status='received' ORDER BY observed_ms,id",
                    (session,))]
            if d.get('memory_ops') or memory_report:
                result['memory_results'] = memory_results
            if memory_report:
                result['memory_report'] = memory_report
            if telemetry is not None:
                telemetry['warning_codes'] = instrumentation.warning_codes(plans)
            if guard_warnings and not dry_run:
                warnings = {gid: plan['warnings'] for gid, plan in plans.items() if plan['warnings']}
                if extra_warnings:
                    # Caller-supplied warnings (e.g. repetition shadow) join the
                    # same receipt as conversation-group warnings and surface
                    # in the returned plans so the reviewer can see them.
                    for gid, extra in extra_warnings.items():
                        if not extra:
                            continue
                        entry = plans.setdefault(gid, {'plan': None, 'warnings': []})
                        entry['warnings'] = list(entry['warnings']) + list(extra)
                        warnings[gid] = list(warnings.get(gid, [])) + list(extra)
                if warnings:
                    # Receipt binds human/model review to this exact draft and current warnings.
                    # A rollback preserves shadow semantics without silently committing warnings.
                    receipt = hashlib.sha256(encode({'decision':digest,'warnings':warnings}).encode()).hexdigest()
                    if warning_review != receipt:
                        db.rollback()
                        return {**result, 'reason':'warning_review_required', 'committed':False,
                                'revision':state['revision'], 'warning_review':receipt}
            if dry_run:
                db.rollback()
            return result

    def only_incoming_since(self, session, since, current):
        with self.connect() as db:
            return only_incoming_since(db, session, since, current)

    def event_import(self, session, document, now, instruction_ref, dry_run=False):
        if dry_run: return world_events.preview(self,session,document,now,instruction_ref)
        with self.connect(True, operation='event_import', session=session) as db:
            return world_events.import_batch(db,session,document,now,instruction_ref)[0]

    def event_inspect(self, session, eid, now, operator=False):
        with self.connect() as db:
            return world_events.inspect(db,session,eid,now,operator)

    def event_list(self, session, now, offset=0, limit=20, operator=False):
        with self.connect() as db:
            return world_events.listing(db,session,now,offset,limit,operator)

    def event_cancel(self, session, eid, now, reason):
        with self.connect(True, operation='event_cancel', session=session) as db:
            return world_events.cancel(db,session,eid,now,reason)

    def intent_inspect(self, session, iid, now):
        with self.connect() as db:
            state=session_row(db,session)
            return {'ok':True,'reason':'action_intent','revision':state['revision'],
                    'intent':action_intents.inspect(db,session,iid,now)}

    def recall(self, session, keywords, limit=10, *, offset=0):
        integer(limit,'limit',1); require(limit<=10,'limit_too_large')
        integer(offset,'offset')
        require(len(keywords)<=20,'too_many_keywords')
        patterns = ['%'+text(k).replace('\\','\\\\').replace('%','\\%').replace('_','\\_')+'%' for k in keywords]
        with self.connect() as db:
            state=session_row(db,session)
            if patterns:
                score='+'.join("(summary LIKE ? ESCAPE '\\')" for _ in patterns)
                rows=db.execute(f'SELECT *,({score}) AS score FROM facts WHERE session_id=? AND {world_events.VISIBLE_FACT_SQL} AND {memory_continuity.VISIBLE_FACT_SQL} AND ({score})>0 ORDER BY score DESC,recorded_ms DESC,id LIMIT ? OFFSET ?',(*patterns,session,*patterns,limit+1,offset))
            else:
                rows=db.execute(f'SELECT * FROM facts WHERE session_id=? AND {world_events.VISIBLE_FACT_SQL} AND {memory_continuity.VISIBLE_FACT_SQL} ORDER BY recorded_ms DESC,id LIMIT ? OFFSET ?',(session,limit+1,offset))
            rows=rows.fetchall()
            return {'ok':True,'reason':'recalled','revision':state['revision'],'facts':[rowdict(r) for r in rows[:limit]],
                    'offset':offset,'limit':limit,'has_more':len(rows)>limit,
                    'next_offset':offset+limit if len(rows)>limit else None}

    def memory_context(self, session, channel, selected_scopes=None, **options):
        with self.connect() as db:
            state = session_row(db, session)
            return {'ok': True, 'reason': 'memory_context', 'revision': state['revision'],
                    **memory_continuity.context(db, session, channel, selected_scopes, **options)}

    def memory_history_search(self, session, channel, keywords, selected_scopes=None, **options):
        with self.connect() as db:
            state = session_row(db, session)
            return {'ok': True, 'reason': 'memory_history_search', 'revision': state['revision'],
                    **memory_continuity.history_search(db, session, channel, keywords, selected_scopes, **options)}

    def memory_diagnostics(self, session, decision_id=None, offset=0, limit=20):
        integer(offset, 'memory_offset'); integer(limit, 'memory_limit', 1)
        require(limit <= 100, 'memory_limit_too_large')
        with self.connect() as db:
            state = session_row(db, session)
            where = 'session_id=?'; args = [session]
            if decision_id is not None:
                where += ' AND decision_id=?'; args.append(text(decision_id, 'decision_id'))
            total = db.execute('SELECT count(*) FROM continuity_diagnostics WHERE '+where, args).fetchone()[0]
            rows = db.execute('SELECT * FROM continuity_diagnostics WHERE '+where+
                ' ORDER BY created_ms DESC,decision_id DESC LIMIT ? OFFSET ?', (*args,limit,offset)).fetchall()
            return {'ok':True,'reason':'memory_diagnostics','revision':state['revision'],
                    'items':[{**dict(r),'mode':'shadow','status':'shadow_rejected'} for r in rows],
                    'total':total,'offset':offset,'has_more':offset+len(rows)<total}

    def memory_recall(self, session, keywords, **options):
        with self.connect() as db:
            state = session_row(db, session)
            return {**memory_continuity.recall(db, session, keywords, world_events.VISIBLE_FACT_SQL, **options),
                    'revision': state['revision']}

    def backup(self, target):
        target=Path(target).expanduser().resolve()
        require(not target.exists(),'backup_exists')
        target.parent.mkdir(parents=True,exist_ok=True)
        # Exclusive create prevents accidental overwrite of an existing file.
        with open(target,'xb'): pass
        try:
            with self.connect() as source:
                dest=sqlite3.connect(target)
                try:
                    source.backup(dest)
                    require(dest.execute('PRAGMA integrity_check').fetchone()[0]=='ok','backup_integrity_failed')
                finally: dest.close()
        except Exception:
            target.unlink(missing_ok=True)
            raise
        return {'ok':True,'reason':'backed_up','path':str(target)}

    def maintenance(self, now):
        """Backups use the SQLite API; prune only owned, validated filenames."""
        integer(now,'now_ms')
        root=self.path.parent
        backups=root/'backups'
        backups.mkdir(exist_ok=True)
        require(not backups.is_symlink() and backups.resolve().parent==root,'unsafe_backup_directory')
        with file_lock(self.path,'maintenance'):
            prefix=self.path.stem+'-'
            owned=[]
            for path in backups.iterdir():
                if path.is_symlink() or not path.is_file(): continue
                if not (path.name.startswith(prefix) and path.name.endswith('.db')): continue
                stamp=path.name[len(prefix):-3]
                if stamp.isdigit(): owned.append((int(stamp),path))
            owned.sort(reverse=True)
            created=None
            if not owned or now-owned[0][0]>=7*86400000:
                created=backups/(prefix+str(now)+'.db')
                self.backup(created)
                owned.insert(0,(now,created))
            removed=[]
            # Prune only after a freshly verified backup, never on backup failure.
            if created:
                for _,path in owned[4:]:
                    if path.resolve().parent==backups.resolve():
                        path.unlink(); removed.append(path.name)
            logs=root/'logs'
            if logs.is_dir() and not logs.is_symlink():
                today=datetime.fromtimestamp(now/1000,timezone.utc).date()
                for path in logs.iterdir():
                    if path.is_symlink() or not path.is_file() or path.suffix!='.log': continue
                    try: day=date.fromisoformat(path.stem)
                    except ValueError: continue
                    if path.stem==day.isoformat() and (today-day).days>14 and path.resolve().parent==logs.resolve():
                        path.unlink(); removed.append(path.name)
            with self.connect(True) as db:
                expired_events=world_events.expire(db,now)
            return {'ok':True,'reason':'maintained','expired_world_events':expired_events,'backup':str(created) if created else None,
                    'removed':removed}
