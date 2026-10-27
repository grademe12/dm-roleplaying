#!/usr/bin/env python3
"""Prepare/commit boundaries with bounded inputs; policy and transport stay authoritative."""
import argparse
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import re
import time
import uuid
import sqlite3

from common import ControlError, encode, now_ms, require
from dispatch import Dispatcher, ExternalTransport
from store import Store, group_channel
import decision_routing
import decision_contract
import gate
import driver_ownership
import instrumentation
import topic_producer
import prepared_inputs
import memory_cues
import repetition_cues
import episodic_memory
import character_brief
from common import required_text
import session_recovery
import worker_budget
import reply_generator
from decision_drafts import DraftArchive

ROOT = Path(__file__).resolve().parent
MAX_RUN_MS = gate.MAX_CLAIM_RUN_MS
MAX_TURNS = 20
DEFAULT_CONTEXT_LIMIT = prepared_inputs.DEFAULT_CONTEXT_LIMIT


def read_document(value):
    require(value.startswith('@'), 'document_file_required')
    return json.loads(Path(value[1:]).expanduser().read_text(encoding='utf-8-sig'))


class DecisionSession:
    def __init__(self, config, wake_id, clock=now_ms, sleep=time.sleep):
        require(bool(re.fullmatch(r'[A-Za-z0-9_:-]{1,80}', wake_id)), 'invalid_wake_id')
        self.cfg = config
        self.wake = wake_id
        self.state_dir = Path(config['state_dir'])
        self.path = self.state_dir / ('session-' + wake_id + '.json')
        self.session = config['session']
        self.store = Store(config['db'])
        self.clock, self.sleep = clock, sleep
        self.dispatch = Dispatcher(self.store, clock, sleep)
        self.drafts = DraftArchive(self.state_dir, self.session, clock)
        self.last_recovery = None

    @contextmanager
    def owned(self, pickup=False):
        driver_ownership.check(self.cfg)
        with gate.gate_lock(self.state_dir):
            if not gate.claim_is_current(self.state_dir, self.wake, self.clock()):
                gate.rejected(self.state_dir, self.wake, 'session')
                raise ControlError('stale_claim')
            if pickup:
                now = self.clock()
                gate.refresh_claim(self.state_dir, self.wake, 'session', now,
                                   pickup_deadline_ms=now + gate.PICKUP_GRACE_MS)
            else:
                gate.refresh_claim(self.state_dir, self.wake, 'session', self.clock())
            yield

    def wait_guard(self):
        """Ownership guard for the blocking DM wait. Each refresh also rolls the
        pickup deadline, so a session that dies during the wait or right after
        it hands back loses the claim PICKUP_GRACE_MS after its last refresh
        instead of holding it for the full stale window."""
        return self.owned(pickup=True)

    def state(self, turn_id=None):
        state = gate.load_json(self.path, {})
        require(bool(state) and (turn_id is None or state['turn_id'] == turn_id), 'invalid_session_turn')
        return state

    def event(self, phase, turn_id=None, **metadata):
        gate.append_decision_trace(self.state_dir, self.wake, phase, turn_id=turn_id, **metadata)

    def budget(self, state):
        wake = gate.load_json(self.state_dir / 'wake.json', {})
        return worker_budget.remaining(wake, state['started_ms'], self.clock(), MAX_RUN_MS)

    def handoff(self, state, sends):
        # end_conversation only releases the local wait lease, not character/story
        # state. Keep conversation_ended=false and do not invent a goodbye/apply.
        self.store.end_conversation(self.session, self.clock())
        primary = self.store.tick(self.session, self.clock(), view='gate')['work_manifest']['primary']
        phase = ('failed' if state.get('committed_turn') != state['turn_id']
                 and primary in ('dm', 'life', 'repair') else 'finished')
        gate.write_json(self.state_dir / 'wake_ack.json',
                        {'wake_id': self.wake, 'phase': phase, 'at_ms': self.clock(),
                         'reason': 'worker_budget'})
        self.event('ack_' + phase, state['turn_id'], reason='worker_budget')
        return {'ok': True, 'reason': 'worker_budget', 'next': 'done', 'send': sends}

    def context(self, channel):
        require(channel in ('slack', 'instagram'), 'invalid_channel')
        name = 'recent_thread_slack.json' if channel == 'slack' else 'recent_thread.json'
        path = self.state_dir / name
        try:
            raw = path.read_bytes()
            data = json.loads(raw)
            require(isinstance(data, dict) and isinstance(data.get('messages'), list), 'invalid_recent_context')
            require(all(isinstance(m, dict) and isinstance(m.get('id'), str)
                        for m in data['messages']), 'invalid_recent_context')
        except (OSError, ValueError, ControlError):
            return {'ready': False, 'reason': 'context_unavailable', 'messages': [], 'message_ids': []}
        now = self.clock()
        if channel == 'slack':
            health = gate.load_json(self.state_dir / 'slack_health.json', {})
            fresh = (health.get('connected') is True
                     and 0 <= now - health.get('last_heartbeat_ms', 0) <= 120_000
                     and not health.get('alerts', {}).get('ingest_failed'))
        else:
            health = gate.load_json(self.state_dir / 'poller_health.json', {})
            fresh = (0 <= now - data.get('fetched_ms', 0) <= 600_000
                     and not any(key in health.get('alerts', {}) for key in
                                 ('read_failing', 'unknown_sender', 'ingest_failed', 'possible_gap'))
                     and not health.get('consecutive_read_failures', 0))
        return {'ready': bool(fresh), 'reason': 'context_ready' if fresh else 'context_stale',
                'fetched_ms': data.get('fetched_ms'), 'sha256': hashlib.sha256(raw).hexdigest(),
                'message_ids': [m['id'] for m in data['messages']], 'messages': data['messages']}

    def prepare(self, queries=None, known_core_rules_hash=None, context_limit=DEFAULT_CONTEXT_LIMIT, mode='auto',
                document_receipts=None, decision_format='full'):
        self.last_recovery = None
        require(decision_format in decision_contract.FORMATS, 'invalid_decision_format')
        require(type(context_limit) is int and prepared_inputs.MIN_CONTEXT_LIMIT <= context_limit <= prepared_inputs.MAX_CONTEXT_LIMIT,
                'invalid_context_limit')
        queries = [] if queries is None else queries
        require(isinstance(queries, list) and len(queries) <= 8
                and all(isinstance(q, list) for q in queries), 'invalid_recall_queries')
        started = time.monotonic_ns()
        with self.owned():
            state = gate.load_json(self.path, {}) or {
                'started_ms': gate.load_json(self.state_dir / 'wake.json', {}).get('woken_ms', self.clock()),
                'turn_count': 0, 'applied_ids': [], 'turn_id': None}
            if state['turn_id'] is not None and state.get('committed_turn') == state['turn_id']:
                self.last_recovery = {'next': 'finish', 'turn_id': state['turn_id'], 'committed': True}
                raise ControlError('finish_required')
            if state['turn_id'] is None:
                state['turn_id'] = uuid.uuid4().hex
                state.setdefault('document_receipts', {})
            if self.budget(state)['basis'] != 'controller_session' and self.budget(state)['remaining_ms'] < 1000:
                return self.handoff(state, {})
            # Keep the ID even when preparation has to be retried.
            state.update(prepared_revision=None, input_mode=None, input_id=None,
                         input_catalog={}, input_offers={}, context_ids={})
            state.pop('compact_contract', None)
            # Stage locally. Publish only after all reads and validation succeed;
            # a failed refresh must not destroy the previous prepared generation.
            topic_producer.publish_pending(self.store,self.session,self.clock())
            snap = self.store.tick(self.session, self.clock(), view='life',
                                   known_core_rules_hash=known_core_rules_hash)
            manifest = snap['work_manifest']
            if state.get('background_pending') and manifest['primary'] == 'idle' and manifest['background_allowed']:
                manifest.update(primary='background', runbook='runbooks/background.md', view='life')
            if manifest['primary'] in ('dm', 'repair') and not manifest['life_required']:
                decision_routing.project(snap, 'dm')
            channels = {(m.get('observation') or {}).get('channel', 'instagram') for m in snap['messages']}
            channels.update(j.get('channel', 'instagram') for j in snap['jobs'])
            channels.update(r['channel'] for r in snap.get('continuations', []) if r['status'] in ('active','paused'))
            if self.cfg.get('slack'): channels.add('slack')
            if self.cfg.get('instagram_enabled', True): channels.add('instagram')
            contexts = {c: self.context(c) for c in sorted(channels)}
            for channel, context in contexts.items():
                context['source_message_ids']=[m['id'] for m in context['messages']]
                targets = {m['id'] for m in snap['messages']
                           if (m.get('observation') or {}).get('channel', 'instagram') == channel}
                start = max(0, len(context['messages']) - context_limit)
                for index, message in enumerate(context['messages']):
                    if message['id'] in targets: start = min(start, index)
                context['messages'] = context['messages'][start:]
                context['message_ids'] = [m['id'] for m in context['messages']]
                context['source_version'] = prepared_inputs.version(context['messages'])
                context['missing_target_ids'] = [m['id'] for m in snap['messages']
                    if (m.get('observation') or {}).get('channel', 'instagram') == channel
                    and m['id'] not in context['message_ids']]
            recalls = [self.store.recall(self.session, q, limit=prepared_inputs.recall_limit(q)) for q in queries]
            cue_started = time.monotonic_ns()
            with self.store.connect() as db:
                cues = memory_cues.snapshot(db,self.session,self.clock(),contexts)
            for channel, context in contexts.items():
                cues[channel]['recent_context_included'] = len(context['message_ids'])
                cues[channel]['recent_context_available'] = len(context['source_message_ids'])
            cue_elapsed_ms = (time.monotonic_ns()-cue_started)//1_000_000
            # Repetition guard (default off): when off, nothing is computed or
            # stored, so prepare output and state stay byte-identical.
            rep_settings = repetition_cues.guard_settings(self.cfg)
            rep_cues = {}
            rep_check = {}
            if rep_settings['mode'] != 'off':
                for channel, context in contexts.items():
                    built = repetition_cues.analyze(channel, context['messages'], rep_settings)
                    rep_cues[channel] = built['cues']
                    rep_check[channel] = built['check']
                state['repetition_cues'] = rep_cues
                state['repetition_check'] = rep_check
            episode_started = time.monotonic_ns()
            memories = {}
            episode_options = episodic_memory.reply_settings(self.cfg)
            observations = {}
            suspended = state.get('episodic_suspended_turn') == state['turn_id']
            if episode_options.get('diagnostic'):
                self.event('episodic_memory_degraded', state['turn_id'], reason=episode_options['diagnostic'])
            if episode_options['mode'] != 'off' and not suspended:
                for channel, context in contexts.items():
                    targets = [m['text'] for m in snap['messages'] if
                               (m.get('observation') or {}).get('channel', 'instagram') == channel]
                    query = episodic_memory.build_query(targets, context['messages'])
                    observations[channel] = episodic_memory.recall(self.cfg, channel, query, self.clock(),
                                                               recent_ids=context['message_ids'])
                    if observations[channel].get('reason'):
                        self.event('episodic_memory_degraded', state['turn_id'],
                                   channel=channel, reason=observations[channel]['reason'])
                if episode_options['mode'] == 'enforce':
                    memories = observations
                    snap['episodic_memory'] = memories
            state['episodic_memory'] = memories
            episode_elapsed_ms = (time.monotonic_ns()-episode_started)//1_000_000
            recall_stats = [instrumentation.recall_metadata(q, result) for q, result in zip(queries, recalls)]
            final_revision = self.store.tick(self.session, self.clock(), limit=1, view='gate')['revision']
            if final_revision != snap['revision'] or any(r['revision'] != snap['revision'] for r in recalls):
                self.event('session_prepare_failed', state['turn_id'], reason='prepare_inputs_changed',
                           revision=snap['revision'], actual_revision=final_revision,
                           recall_count=len(recalls), recall_stats=recall_stats,
                           recall_output_bytes=len(encode(recalls).encode('utf-8')))
            require(final_revision == snap['revision'] and all(r['revision'] == snap['revision'] for r in recalls),
                    'prepare_inputs_changed')
            documents = {}
            # Shared wording rules apply to replies initiated from life/background too.
            name = 'design/conversation-rules.md'
            documents[name] = required_text(ROOT / name)
            if memories:
                name = 'runbooks/episodic-memory.md'
                documents[name] = required_text(ROOT / name)
            if manifest['primary'] == 'dm':
                name = 'design/reply-context.md'
                documents[name] = required_text(ROOT / name)
            if snap['core_rules_changed']:
                content = Path(snap['core_rules_path']).read_text(encoding='utf-8')
                require(hashlib.sha256(content.encode()).hexdigest() == snap['core_rules_hash'], 'rules_changed')
                documents['CORE_RULES.md'] = content
            if manifest['runbook']:
                documents[manifest['runbook']] = (ROOT / manifest['runbook']).read_text(encoding='utf-8')
            if any(r in manifest['life_required'] for r in
                   ('activity_due','no_current_activity','scheduled_decision','appointment_due',
                    'appointment_prepare_due','appointment_review_due',
                    'action_intent_due','weekly_plan_missing')):
                documents['runbooks/activity-selection.md'] = (ROOT / 'runbooks/activity-selection.md').read_text(encoding='utf-8')
                if any(h['code']=='passive_leisure_concentration'
                       for h in snap.get('activity_history',{}).get('hints',[])):
                    for name in ('design/lifestyle.md','design/hobby-baselines.md'):
                        documents[name] = required_text(ROOT / name)
            if any(r['status'] in ('active','paused') for r in snap.get('continuations', [])):
                documents['runbooks/continuation.md'] = (ROOT / 'runbooks/continuation.md').read_text(encoding='utf-8')
            if manifest['primary'] == 'dm' and manifest['life_required']:
                documents['runbooks/life-boundaries.md'] = (ROOT / 'runbooks/life-boundaries.md').read_text(encoding='utf-8')
            if (snap['interaction']['active'] or snap['interaction'].get('candidate_appointment_id') or
                    any(r in manifest['life_required'] for r in
                    ('appointment_due','appointment_prepare_due','appointment_review_due'))):
                documents['design/in-person.md'] = (ROOT / 'design/in-person.md').read_text(encoding='utf-8')
            state['interaction'] = snap['interaction']
            if (snap.get('world_events') or {}).get('pending'):
                documents['runbooks/world-events.md'] = (ROOT / 'runbooks/world-events.md').read_text(encoding='utf-8')
            if manifest['primary'] == 'dm' or 'memory_policy' in snap:
                documents['runbooks/memory-continuity.md'] = (ROOT / 'runbooks/memory-continuity.md').read_text(encoding='utf-8')
            character_brief.prepare(self, state, snap, documents, contexts)
            state['prepared_revision'] = snap['revision']
            state['memory_cues'] = cues
            state['reply_source'] = {
                'generation': uuid.uuid4().hex,
                'contexts': {channel: {'ids': ctx['message_ids'], 'ready': ctx['ready'],
                                      'version': ctx['source_version'], 'known_ids': ctx['source_message_ids']}
                             for channel, ctx in contexts.items()}}
            state['context_ids'] = {c: ctx['message_ids'] if ctx['ready'] else [] for c, ctx in contexts.items()}
            previous = state.get('last_prepare', {})
            same_turn = previous.get('turn_id') == state['turn_id']
            context_digest = instrumentation.digest({c: {'ready': ctx['ready'], 'ids': sorted(ctx['message_ids'])}
                                                     for c, ctx in contexts.items()})
            target_digest = instrumentation.digest(sorted(
                [(m.get('observation') or {}).get('channel', 'instagram'), m['id']] for m in snap['messages']))
            prepare_index = previous.get('index', 0) + 1 if same_turn else 1
            state['last_prepare'] = {'turn_id': state['turn_id'], 'index': prepare_index,
                                     'revision': snap['revision'], 'context_digest': context_digest,
                                     'target_digest': target_digest}
            result = {'ok': True, 'reason': 'prepared', 'turn_id': state['turn_id'], 'wake_id': self.wake,
                    'snapshot': snap, 'contexts': contexts, 'recalls': recalls, 'documents': documents,
                    'memory_cues': cues,
                    'constraints': {'reply_blocked_attention': 'unavailable',
                                    'execution_budget': self.budget(state),
                                    'life_required': manifest['life_required'],
                                    'next_decision_is_automatic_transition': False,
                                    'context_is_atomic_with_db': False}}
            if rep_cues:
                result['repetition_cues'] = rep_cues
            generator_settings = reply_generator.settings(self.cfg)
            result['constraints']['reply_generator'] = {
                'enabled': generator_settings['enabled'], 'failure_policy': generator_settings['failure_policy'],
                'command': 'generate-reply'}
            if decision_format == 'compact-v1':
                generation = uuid.uuid4().hex
                state['compact_contract'] = {
                    'generation': generation, 'revision': snap['revision'],
                    'contexts': {channel: {'ids': ctx['message_ids'] if ctx['ready'] else [],
                                           'ready': ctx['ready'],
                                           'version': ctx['source_version'],
                                           'known_ids': ctx['source_message_ids']}
                                 for channel, ctx in contexts.items()}}
                frame = {'prepared': result, 'turn_id': state['turn_id'],
                         'decision_generation': generation, 'schema_reader': 'decision_session'}
                result.update(decision_format=decision_format, decision_generation=generation,
                              decision_contract=decision_contract.describe(frame), schema_sections={}, schema_sources={})
                topics = decision_contract.initial_topics(manifest)
                if topics:
                    result['schema_sections'], result['schema_sources'] = decision_contract.sections(ROOT, topics)
            try:
                result['recovery_candidates'] = self.drafts.candidates()
            except OSError:
                result['recovery_candidates'] = []
            try:
                source_sizes = prepared_inputs.configure(state, result, queries, context_limit, mode,
                                                        snap['core_rules_path'], document_receipts)
                state['prepared_memory_fact_ids'] = (sorted(set(
                    [fid for r in recalls for fid in memory_cues.fact_ids(r)] +
                    memory_cues.fact_ids({'memory_context':snap.get('memory_context',{})})))
                    if state['input_mode'] == 'bundled' else [])
                state['prepared_document_hashes'].update(result.get('schema_sources', {}))
            except ControlError as exc:
                self.event('session_prepare_failed', state['turn_id'], reason=exc.reason,
                           revision=snap['revision'], requested_mode=mode)
                raise
            gate.write_json(self.path, state)
            self.event('session_prepare', state['turn_id'], revision=snap['revision'],
                       route=manifest['primary'], recall_count=len(recalls), recall_stats=recall_stats,
                       input_mode=state['input_mode'], source_bytes=source_sizes,
                       memory_cue_bytes=memory_cues.size(cues),
                       memory_cue_elapsed_ms=cue_elapsed_ms,
                       episodic_lookup_elapsed_ms=episode_elapsed_ms,
                       episodic_selected_count=sum(m.get('candidate_count', 0) for m in observations.values()),
                       episodic_incomplete_channels=sum(bool(m.get('incomplete')) for m in observations.values()),
                       recall_output_bytes=len(encode(recalls).encode('utf-8')),
                       output_bytes=len(encode(result).encode('utf-8')), prepare_index=prepare_index,
                       decision_format=decision_format,
                       previous_prepare_same_turn=same_turn,
                       revision_changed=snap['revision'] != previous.get('revision') if same_turn else None,
                       context_changed=context_digest != previous.get('context_digest') if same_turn else None,
                       target_messages_changed=target_digest != previous.get('target_digest') if same_turn else None,
                       elapsed_ms=(time.monotonic_ns() - started) // 1_000_000)
            return result

    def read_input(self, turn_id, input_id, kind, **options):
        return prepared_inputs.read(self, turn_id, input_id, kind, **options)

    def generate_reply(self, turn_id, request, input_receipts=None):
        return reply_generator.generate(self, turn_id, request, input_receipts)

    def read_decision_schema(self, turn_id, generation, topics):
        with self.owned():
            state = self.state(turn_id)
            require(state.get('compact_contract', {}).get('generation') == generation
                    and generation is not None, 'decision_generation_mismatch')
            prepared_inputs.fresh(self, state)
            prepared_inputs.check_documents(self, state['prepared_document_hashes'])
            content, hashes = decision_contract.sections(ROOT, topics)
            state['prepared_document_hashes'].update(hashes)
            gate.write_json(self.path, state)
            return {'ok': True, 'reason': 'decision_schema', 'turn_id': turn_id,
                    'decision_generation': generation, 'sections': content, 'source_hashes': hashes}

    def expand_compact(self, turn_id, decision, generation, input_receipts):
        """Direct prepare/commit path, independent of MuseExecutor's file protocol."""
        require(isinstance(decision, dict), 'invalid_decision_document')
        require(isinstance(generation, str) and re.fullmatch(r'[a-f0-9]{32}', generation),
                'decision_generation_required')
        raw_digest = instrumentation.digest(decision)
        folder = self.state_dir / 'compact-decisions' / self.wake
        path = folder / (generation + '-' + raw_digest + '.json')
        with self.owned():
            state = self.state(turn_id)
            cached = gate.load_json(path, None)
            if cached is not None:
                require(cached['generation'] == generation and cached['turn_id'] == turn_id
                        and cached['raw_digest'] == raw_digest
                        and cached['expanded_digest'] == instrumentation.digest(cached['decision']),
                        'compact_decision_cache_mismatch')
                with self.store.connect() as db:
                    saved = db.execute('SELECT digest FROM decisions WHERE session_id=? AND id=?',
                                       (self.session, cached['decision']['decision_id'])).fetchone()
                if saved is not None:
                    require(saved['digest'] == cached['expanded_digest'], 'decision_identity_collision')
                    return cached['decision']  # Exact committed replay needs no old receipts.
            contract = state.get('compact_contract', {})
            require(contract.get('generation') == generation, 'decision_generation_mismatch')
            prepared_inputs.fresh(self, state)
            prepared_inputs.check_documents(self, state['prepared_document_hashes'])
            channels = {}
            if state['input_mode'] == 'compact':
                require(isinstance(input_receipts, list) and all(isinstance(t, str) for t in input_receipts),
                        'input_receipts_required')
                for token in input_receipts:
                    offer = state['input_offers'].get(instrumentation.digest(token))
                    require(offer is not None, 'invalid_input_receipt')
                    if offer['kind'] == 'context':
                        channels.setdefault(offer['channel'], set()).update(offer['ids'])
            else:
                channels = {channel: set(ctx['ids']) for channel, ctx in contract['contexts'].items()}
            inputs = []
            raw_dms = decision.get('dm_decisions', [])
            if not isinstance(raw_dms, list):
                raise ControlError('invalid_decision_document')
            used_channels = [dm.get('channel') for dm in raw_dms
                             if isinstance(dm, dict) and dm.get('action') in ('reply', 'initiate')]
            for channel, ctx in contract['contexts'].items():
                if channel in used_channels:
                    require(ctx['ready'], 'reply_context_not_ready')
                    current = self.context(channel)
                    require(current['ready'], 'reply_context_not_ready')
                    prepared_inputs.check_context(current, ctx)
                ids = [mid for mid in ctx['ids'] if mid in channels.get(channel, set())]
                inputs.append({'kind': 'context', 'content': {'channel': channel,
                                                             'messages': [{'id': mid} for mid in ids]}})
            frame = {'wake_id': self.wake, 'turn_id': turn_id, 'decision_generation': generation,
                     'prepared': {'snapshot': {'revision': contract['revision']}}, 'inputs': inputs}
            expanded = decision_contract.expand(frame, decision)
            # Private intent copy before apply; never change the model's source file.
            # An uncommitted retry can acknowledge more context. Rebuild only
            # metadata; an already committed decision returned above is frozen.
            folder.mkdir(parents=True, exist_ok=True, mode=0o700)
            gate.write_json(path, {'generation': generation, 'turn_id': turn_id, 'raw_digest': raw_digest,
                                  'expanded_digest': instrumentation.digest(expanded), 'decision': expanded})
            return expanded

    def transport(self, channel):
        require(channel in ('slack', 'instagram'), 'invalid_channel')
        path = ((self.cfg.get('slack') or {}).get('transport_config') if channel == 'slack'
                else self.cfg.get('transport_config'))
        require(isinstance(path, str) and bool(path), 'transport_config_required')
        return ExternalTransport(json.loads(Path(path).read_text(encoding='utf-8-sig')))

    def commit(self, turn_id, decision, warning_review=None, conversation_ended=False, input_receipts=None,
               decision_format='full', decision_generation=None):
        self.last_recovery = None
        try:
            require(decision_format in decision_contract.FORMATS, 'invalid_decision_format')
            if isinstance(decision, dict) and any(dm.get('generator_id') for dm in decision.get('dm_decisions', [])
                                                 if isinstance(dm, dict)):
                with self.owned():
                    decision = reply_generator.attach(self, turn_id, decision)
            if decision_format == 'compact-v1':
                decision = self.expand_compact(turn_id, decision, decision_generation, input_receipts)
            else:
                require(decision_generation is None, 'invalid_decision_format')
            result = self._commit(turn_id, decision, warning_review, conversation_ended, input_receipts,
                                  decision_generation)
        except (ControlError, OSError, ValueError, KeyError, TypeError, sqlite3.Error) as exc:
            reason = exc.reason if isinstance(exc, ControlError) else 'session_input_or_io_error'
            self.last_recovery = session_recovery.advice(
                reason, transient=isinstance(exc, (OSError, sqlite3.Error)))
            metadata = {}
            if isinstance(decision, dict):
                try:
                    metadata = instrumentation.decision_metadata(decision)
                    state = gate.load_json(self.path, {})
                    draft_id = self.drafts.save(self.wake, turn_id, decision, reason,
                                                state.get('prepared_revision'))
                    self.last_recovery.update(saved=True, draft_id=draft_id)
                except (ControlError, OSError, ValueError, KeyError, TypeError, AttributeError):
                    self.last_recovery.update(saved=False, archive_reason='draft_archive_unavailable')
            self.event('apply_rejected', turn_id, reason=reason,
                       exit_code=10 if reason == 'stale_claim' else 2 if reason == 'revision_conflict' else 1,
                       error_type=type(exc).__name__,
                       warning_codes=instrumentation.error_warning_codes(exc) if isinstance(exc, ControlError) else [],
                       recovery=self.last_recovery, **gate.claim_metadata(self.state_dir), **metadata)
            raise
        if result.get('committed'):
            try:
                self.drafts.resolve(self.wake, turn_id, decision)
            except (OSError, ValueError, TypeError):
                pass
        return result

    def recovery_drafts(self, draft_id=None):
        with self.owned():
            if draft_id is None:
                return {'ok': True, 'drafts': self.drafts.candidates()}
            return {'ok': True, 'draft': self.drafts.read(draft_id)}

    def _commit(self, turn_id, decision, warning_review=None, conversation_ended=False, input_receipts=None,
                decision_generation=None):
        require(isinstance(decision, dict), 'invalid_decision_document')
        with self.owned():
            state = self.state(turn_id)
            # Replays of a committed document remain governed by Store's digest/idempotency check.
            with self.store.connect() as db:
                replay = db.execute('SELECT 1 FROM decisions WHERE session_id=? AND id=?',
                                    (self.session, decision.get('decision_id'))).fetchone() is not None
            if not replay:
                reply_generator.require_generated(self.cfg, decision)
                if any(isinstance(dm, dict) and dm.get('generator_id') for dm in decision.get('dm_decisions', [])):
                    reply_generator.attach(self, turn_id, decision, check_only=True)
                if decision_generation is not None:
                    require(state.get('compact_contract', {}).get('generation') == decision_generation,
                            'decision_generation_mismatch')
                require(decision.get('expected_revision') == state.get('prepared_revision'), 'prepare_required')
                character_brief.validate_reply(self, state)
                episodic_memory.validate_reply(self, state)
                prepared_inputs.check_documents(self, state.get('prepared_document_hashes', {}))
                context_ids = state.get('context_ids', {})
                if state.get('input_mode') == 'compact':
                    prepared_inputs.fresh(self, state)
                    context_ids = prepared_inputs.validate(self, state, decision, input_receipts)
                    self.event('input_acknowledged', turn_id, input_id=state['input_id'],
                               receipt_count=len(input_receipts))
                for op in decision.get('in_person_ops',[]):
                    if op.get('action') == 'start':
                        channel = op.get('channel','slack')
                        ctx = self.context(channel)
                        require(ctx['ready'] and set(op.get('evidence_message_ids',[])) <=
                                set(context_ids.get(channel,[])),'in_person_context_not_ready')
                for dm in decision.get('dm_decisions', []):
                    if dm.get('action') in ('reply', 'initiate'):
                        ids = dm.get('message_ids', [])
                        channel = dm.get('channel') or dm.get('conversation_review', {}).get('context', {}).get('channel')
                        require(channel in ('slack', 'instagram'), 'reply_channel_required')
                        ctx = self.context(channel)
                        require(ctx['ready'] and channel in context_ids
                                and set(ids) <= set(context_ids[channel])
                                and set(ids) <= set(ctx['message_ids']), 'reply_context_not_ready')
                        if decision_generation is not None:
                            prepared_context = state['compact_contract']['contexts'][channel]
                            require(prepared_context['ready'], 'reply_context_not_ready')
                            prepared_inputs.check_context(ctx, prepared_context)
            telemetry = instrumentation.decision_metadata(decision)
            telemetry.update(warning_codes=[], warning_review_supplied=warning_review is not None)
            # Repetition guard: observe records metrics only; shadow also hands
            # per-group warnings to Store so they join the existing receipt.
            rep_settings = repetition_cues.guard_settings(self.cfg)
            rep_warnings = None
            if rep_settings['mode'] != 'off' and state.get('repetition_check'):
                rep_metrics, rep_found = repetition_cues.evaluate(state, decision, rep_settings)
                self.event('repetition_checked', turn_id, mode=rep_settings['mode'],
                           **{key: rep_metrics[key] for key in
                              ('verbatim_max', 'phrase_hits', 'opener_repeat', 'closer_repeat',
                               'draft_chars', 'prev_turn_chars', 'turns_considered')})
                if rep_settings['mode'] == 'shadow':
                    rep_warnings = rep_found or None
            self.event('apply_started', turn_id, **telemetry)
            started = time.monotonic_ns()
            try:
                result = self.store.apply(self.session, decision, self.clock(), guard_warnings=True,
                                          warning_review=warning_review, telemetry=telemetry,
                                          extra_warnings=rep_warnings)
            except ControlError as exc:
                telemetry['warning_codes'] = instrumentation.error_warning_codes(exc)
                self.event('apply_finished', turn_id, exit_code=2 if exc.reason == 'revision_conflict' else 1,
                           reason=exc.reason, elapsed_ms=(time.monotonic_ns() - started) // 1_000_000,
                           **telemetry)
                raise
            self.event('apply_finished', turn_id,
                       exit_code=4 if result['reason'] == 'warning_review_required' else 0, reason=result['reason'],
                       result_revision=result['revision'],
                       elapsed_ms=(time.monotonic_ns() - started) // 1_000_000, **telemetry)
            if result['reason'] == 'warning_review_required':
                return {'ok': True, 'reason': result['reason'], 'committed': False, 'commit': result,
                        'turn_id': turn_id, 'next': 'review'}
            try:
                if decision['decision_id'] not in state['applied_ids']:
                    state['applied_ids'].append(decision['decision_id'])
                    state['turn_count'] += 1
                channels = set()
                with self.store.connect() as db:
                    for dm in decision.get('dm_decisions', []):
                        if dm.get('group_id'):
                            channels.add(group_channel(db, self.session, dm['group_id']))
                state.update(committed_turn=turn_id, send_channels=sorted(channels),
                             conversation_ended=bool(conversation_ended))
                gate.write_json(self.path, state)
            except Exception:
                return {'ok': True, 'reason': 'committed', 'committed': True, 'commit': result,
                        'turn_id': turn_id, 'after': {'ok': False, 'reason': 'session_state_write_failed',
                                                    'next': 'retry_same_commit'}}
        # No gate/DB lock is held across network calls or long polling.
        try:
            after = self.finish(turn_id)
        except Exception as exc:
            reason = exc.reason if isinstance(exc, ControlError) else 'post_commit_error'
            self.event('session_after_failed', turn_id, reason=reason)
            after = {'ok': False, 'reason': reason, 'next': 'done' if reason == 'stale_claim' else 'finish'}
        return {'ok': True, 'reason': 'committed', 'committed': True, 'commit': result,
                'turn_id': turn_id, 'after': after}

    def _next(self, state, reason):
        state['turn_id'] = None
        gate.write_json(self.path, state)
        return {'ok': True, 'reason': reason, 'next': 'prepare'}

    def finish(self, turn_id):
        with self.owned():
            state = self.state(turn_id)
            require(state.get('committed_turn') == turn_id, 'commit_required')
            if self.budget(state)['basis'] != 'controller_session' and self.budget(state)['remaining_ms'] < 1000:
                return self.handoff(state, {})
        sends = {}
        for channel in state['send_channels']:
            with self.owned():
                context = self.context(channel)
            if not context['ready']:
                sends[channel] = {'ok': False, 'reason': 'context_stale'}
                continue
            try:
                options = {}
                budget = self.budget(state)
                if budget['basis'] != 'controller_session':
                    options['deadline_ms'] = budget['deadline_ms'] - budget['finish_reserve_ms']
                sends[channel] = self.dispatch.send_due(self.session, self.transport(channel), channel=channel, **options)
            except ControlError as exc:
                # Saving succeeded. Do not regenerate it because dispatch is busy/misconfigured.
                sends[channel] = {'ok': False, 'reason': exc.reason}
            self.event('session_send', turn_id, channel=channel, reason=sends[channel]['reason'])
            if sends[channel]['reason'] == 'worker_budget':
                with self.owned():
                    return self.handoff(self.state(turn_id), sends)
        with self.owned():
            state = self.state(turn_id)
            snap = self.store.tick(self.session, self.clock(), view='dm')
            budget = self.budget(state)
            remaining = budget['remaining_ms']
            if budget['basis'] != 'controller_session' and remaining < 1000:
                return self.handoff(state, sends)
            exhausted = remaining < 1000 or state['turn_count'] >= MAX_TURNS
            urgent = snap['work_manifest']['primary'] in ('dm', 'life', 'repair')
            if urgent and not exhausted:
                self.event('session_after', turn_id, mode=snap['conversation_mode'], reason='decision_needed')
                return {**self._next(state, 'decision_needed'), 'send': sends}
            wait = not exhausted and not state['conversation_ended'] and snap['conversation_mode'] == 'live'
            reason = ('session_limit' if exhausted else 'conversation_ended' if state['conversation_ended']
                      else 'live_wait' if wait else 'mode_' + snap['conversation_mode'])
            self.event('session_after', turn_id, mode=snap['conversation_mode'], reason=reason)
            if not wait:
                return self._end(state, reason, sends)
            timeout = min(600, remaining // 1000)
        self.event('session_wait_started', turn_id, timeout_seconds=timeout)
        options = {}
        if budget['basis'] != 'controller_session':
            options['lease_deadline_ms'] = budget['deadline_ms'] - budget['finish_reserve_ms']
        wake = self.store.wait_dm(self.session, timeout, self.clock, self.sleep, owner_guard=self.wait_guard,
                                  lease_owner=self.wake, **options)
        self.event('session_wait_finished', turn_id, reason=wake['reason'], waited_ms=wake['waited_ms'])
        # The refresh that hands the wait back still carries the pickup deadline:
        # if the session dies here, the gate reclaims the claim PICKUP_GRACE_MS
        # later. The next ordinary command (prepare/commit/close) refreshes
        # without it and clears the deadline.
        with self.owned(pickup=True):
            state = self.state(turn_id)
            if self.budget(state)['basis'] != 'controller_session' and self.budget(state)['remaining_ms'] < 1000:
                return self.handoff(state, sends)
            if wake['reason'] != 'timeout':
                return {**self._next(state, wake['reason']), 'wait': wake, 'send': sends}
            return self._end(state, 'wait_timeout', sends)

    def _end(self, state, reason, sends, allow_repair_handoff=False):
        # Caller owns the gate. Recheck the DB at handoff; never mark pending work done.
        if self.budget(state)['basis'] != 'controller_session' and self.budget(state)['remaining_ms'] < 1000:
            return self.handoff(state, sends)
        if self.clock() - state['started_ms'] >= MAX_RUN_MS or state['turn_count'] >= MAX_TURNS:
            reason = 'session_limit'
        self.store.end_conversation(self.session, self.clock())
        snap = self.store.tick(self.session, self.clock(), view='gate')
        urgent = ('dm', 'life') if allow_repair_handoff else ('dm', 'life', 'repair')
        if reason != 'session_limit' and snap['work_manifest']['primary'] in urgent:
            return {**self._next(state, 'decision_needed'), 'send': sends}
        if (reason != 'session_limit' and not state.get('background_checked')
                and snap['work_manifest']['background_allowed']):
            state.update(background_checked=True, background_pending=True)
            return {**self._next(state, 'background_check'), 'send': sends,
                    'background_allowed': True}
        gate.write_json(self.state_dir / 'wake_ack.json',
                        {'wake_id': self.wake, 'phase': 'finished', 'at_ms': self.clock()})
        self.event('ack_finished', state['turn_id'], reason=reason)
        return {'ok': True, 'reason': reason, 'next': 'done', 'send': sends}

    def close(self, turn_id):
        with self.owned():
            state = self.state(turn_id)
            return self._end(state, 'no_further_work', {}, allow_repair_handoff=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--wake-id', required=True)
    sub = parser.add_subparsers(dest='action', required=True)
    prepare = sub.add_parser('prepare')
    prepare.add_argument('--recall', help='@JSON array of keyword arrays; optional, at most 8')
    prepare.add_argument('--known-core-rules-hash')
    prepare.add_argument('--context-limit', type=int, default=DEFAULT_CONTEXT_LIMIT)
    prepare.add_argument('--mode', choices=('auto', 'bundled', 'compact'), default='auto')
    prepare.add_argument('--document-receipts', help='@JSON array of document receipts actually received in this wake')
    prepare.add_argument('--decision-format', choices=decision_contract.FORMATS, default='full')
    schema = sub.add_parser('read-decision-schema')
    schema.add_argument('--turn-id', required=True)
    schema.add_argument('--decision-generation', required=True)
    schema.add_argument('--topic', action='append', dest='topics', required=True)
    generate = sub.add_parser('generate-reply')
    generate.add_argument('--turn-id', required=True)
    generate.add_argument('--request', required=True)
    generate.add_argument('--input-receipts')
    bundle = sub.add_parser('read-bundle')
    bundle.add_argument('--turn-id', required=True)
    bundle.add_argument('--input-id', required=True)
    bundle.add_argument('--document-receipts', help='@JSON array of reusable document receipts')
    bundle.add_argument('--input-receipts', help='@JSON array of inputs already read and retained for this input_id')
    for kind in ('context', 'document', 'state', 'recall', 'memory-context', 'memory-history-search'):
        reader = sub.add_parser('read-' + kind)
        reader.add_argument('--turn-id', required=True)
        reader.add_argument('--input-id', required=True)
        if kind == 'context':
            reader.add_argument('--channel', choices=('slack', 'instagram'), required=True)
            page = reader.add_mutually_exclusive_group()
            page.add_argument('--message-id', action='append', dest='message_ids')
            page.add_argument('--offset', type=int, default=0)
            reader.add_argument('--limit', type=int, default=DEFAULT_CONTEXT_LIMIT)
        elif kind == 'document':
            reader.add_argument('--name', required=True)
        elif kind in ('memory-context','memory-history-search'):
            reader.add_argument('--channel',choices=('slack','instagram'),required=True)
            reader.add_argument('--scope',action='append',dest='scopes')
            reader.add_argument('--offset',type=int,default=0)
            reader.add_argument('--limit',type=int,default=6)
            if kind=='memory-context':
                reader.add_argument('--key')
                reader.add_argument('--history',action='store_true')
            else:
                reader.add_argument('--keywords',nargs='+',required=True)
                reader.add_argument('--order',choices=('newest','oldest'),default='newest')
                reader.add_argument('--before-ms',type=int)
        elif kind == 'recall':
            query = reader.add_mutually_exclusive_group(required=True)
            query.add_argument('--index', type=int)
            query.add_argument('--keywords', nargs='*')
            reader.add_argument('--memory',action='store_true')
            reader.add_argument('--limit',type=int,help='1..10; defaults to 10, content is never clipped to a byte budget')
            reader.add_argument('--order',choices=('newest','oldest'),default='newest')
            reader.add_argument('--offset',type=int,default=0)
            reader.add_argument('--before-ms',type=int)
        else:
            reader.add_argument('--section', action='append', dest='sections')
            reader.add_argument('--offset', type=int, default=0)
            reader.add_argument('--limit', type=int)
    commit = sub.add_parser('commit')
    commit.add_argument('--turn-id', required=True)
    commit.add_argument('--decision', required=True)
    commit.add_argument('--warning-review')
    commit.add_argument('--conversation-ended', action='store_true')
    commit.add_argument('--input-receipts', help='@JSON array of receipts from compact input reads')
    commit.add_argument('--decision-format', choices=decision_contract.FORMATS, default='full')
    commit.add_argument('--decision-generation')
    finish = sub.add_parser('finish')
    finish.add_argument('--turn-id', required=True)
    close = sub.add_parser('close')
    close.add_argument('--turn-id', required=True)
    sub.add_parser('recovery-drafts')
    recover = sub.add_parser('read-draft')
    recover.add_argument('--draft-id', required=True)
    args = parser.parse_args(argv)
    runner = None
    try:
        path = Path(args.config).expanduser().resolve()
        cfg = json.loads(path.read_text(encoding='utf-8-sig'))
        def resolve(value):
            p = Path(value).expanduser()
            return str(p if p.is_absolute() else path.parent / p)
        for key in ('db', 'state_dir', 'transport_config'):
            if cfg.get(key): cfg[key] = resolve(cfg[key])
        if (cfg.get('slack') or {}).get('transport_config'):
            cfg['slack']['transport_config'] = resolve(cfg['slack']['transport_config'])
        if isinstance(cfg.get('reply_generator'), dict):
            for key in ('key_file', 'rules_file'):
                if cfg['reply_generator'].get(key):
                    cfg['reply_generator'][key] = resolve(cfg['reply_generator'][key])
        if isinstance(cfg.get('episodic_memory'), dict):
            for key in ('path', 'key_file'):
                if isinstance(cfg['episodic_memory'].get(key), str) and cfg['episodic_memory'][key]:
                    cfg['episodic_memory'][key] = resolve(cfg['episodic_memory'][key])
        if isinstance(cfg.get('character_documents'), dict):
            if isinstance(cfg['character_documents'].get('path'), str) and cfg['character_documents']['path']:
                cfg['character_documents']['path'] = resolve(cfg['character_documents']['path'])
        runner = DecisionSession(cfg, args.wake_id)
        if args.action == 'prepare':
            result = runner.prepare(read_document(args.recall) if args.recall else None,
                                    args.known_core_rules_hash, args.context_limit, args.mode,
                                    read_document(args.document_receipts) if args.document_receipts else None,
                                    args.decision_format)
        elif args.action == 'read-bundle':
            result = prepared_inputs.read_bundle(runner, args.turn_id, args.input_id,
                read_document(args.document_receipts) if args.document_receipts else None,
                read_document(args.input_receipts) if args.input_receipts else None)
        elif args.action == 'read-decision-schema':
            result = runner.read_decision_schema(args.turn_id, args.decision_generation, args.topics)
        elif args.action == 'generate-reply':
            result = runner.generate_reply(args.turn_id, read_document(args.request),
                read_document(args.input_receipts) if args.input_receipts else None)
        elif args.action.startswith('read-'):
            options = {k:v for k,v in vars(args).items()
                       if k not in ('config', 'wake_id', 'action', 'turn_id', 'input_id')}
            result = runner.read_input(args.turn_id, args.input_id, args.action[5:], **options)
        elif args.action == 'commit':
            result = runner.commit(args.turn_id, read_document(args.decision), args.warning_review,
                                   args.conversation_ended,
                                   read_document(args.input_receipts) if args.input_receipts else None,
                                   args.decision_format, args.decision_generation)
        elif args.action == 'finish':
            result = runner.finish(args.turn_id)
        elif args.action in ('recovery-drafts', 'read-draft'):
            result = runner.recovery_drafts(getattr(args, 'draft_id', None))
        else:
            result = runner.close(args.turn_id)
        print(encode(result))
        return 0
    except (ControlError, OSError, ValueError, KeyError, TypeError, sqlite3.Error) as exc:
        reason = exc.reason if isinstance(exc, ControlError) else 'session_input_or_io_error'
        error = {'ok': False, 'reason': reason}
        if reason in ('episodic_reprepare_required', 'character_reprepare_required'):
            error.update(next='prepare', same_wake=True)
        if runner is not None:
            runner.event('session_action_failed', getattr(args, 'turn_id', None),
                         action=args.action, reason=reason, error_type=type(exc).__name__)
            if runner.last_recovery:
                error['recovery'] = runner.last_recovery
            elif args.action == 'commit':
                error['recovery'] = session_recovery.advice(
                    reason, transient=isinstance(exc, (OSError, sqlite3.Error)))
        print(encode(error))
        return 10 if reason == 'stale_claim' else 2 if reason == 'revision_conflict' else 1


if __name__ == '__main__':
    raise SystemExit(main())
