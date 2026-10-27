"""Reduced Muse output shape; expand copies, never semantic judgments."""
import copy
import hashlib
import re

from common import encode, require

FORMATS = ('full', 'compact-v1')
REVIEW_FIELDS = {'previous_self', 'user_uptake', 'topic', 'memory', 'checks'}
API_TOPICS = {
    'activities': '### 활동',
    'facts-plans': '### 주간·일정·기억',
    'dm-actions': '### DM 결정',
    'continuations': '## 혼합형 이어가기 — 제한된 사전 예약 + 보충 판단',
    'intents': '## action_intents — 대화에서 정한 다음 행동',
    'memory': '## 선택형 기억 연속성',
    'in-person': '## 대면 대화 상태',
}


def sha(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def initial_topics(manifest):
    topics = []
    if manifest.get('life_required') or manifest['primary'] == 'life':
        topics.extend(('activities', 'facts-plans'))
    if manifest['primary'] == 'repair':
        topics.append('dm-actions')
    if manifest.get('action_intent_due_ids'):
        topics.append('intents')
    if manifest.get('continuation_due_ids'):
        topics.append('continuations')
    if (manifest.get('interaction_mode') == 'in_person' or manifest.get('in_person_candidate') or
            any(r in manifest.get('life_required',[]) for r in
                ('appointment_due','appointment_prepare_due','appointment_review_due'))):
        topics.append('in-person')
    return topics


def sections(root, topics):
    """Verbatim source sections, not generated summaries or document receipts."""
    require(isinstance(topics, list) and 1 <= len(topics) <= len(API_TOPICS)
            and all(isinstance(t, str) and t in API_TOPICS for t in topics)
            and len(topics) == len(set(topics)), 'invalid_decision_schema_topics')
    source = (root / 'API.md').read_text(encoding='utf-8')
    lines = source.splitlines(keepends=True)
    result = {}
    for topic in topics:
        heading = API_TOPICS[topic]
        starts = [i for i, line in enumerate(lines) if line.strip() == heading]
        require(len(starts) == 1, 'decision_schema_source_changed')
        start, level = starts[0], len(heading.split(' ')[0])
        end = len(lines)
        for i in range(start + 1, len(lines)):
            match = re.match(r'^(#{1,6}) ', lines[i])
            if match and len(match[1]) <= level:
                end = i
                break
        result[topic] = ''.join(lines[start:end])
    return result, {'API.md': sha(source)}


def describe(record):
    """This card describes output syntax; subject rules stay in original docs."""
    schema_read = ({'command': 'decision_session.py read-decision-schema',
                    'turn_id': record['turn_id'], 'decision_generation': record['decision_generation'],
                    'topics': ['activities']}
                   if record.get('schema_reader') == 'decision_session' else
                   {'action': 'read', 'requests': [{'kind': 'decision-schema',
                                                  'options': {'topics': ['activities']}}]})
    return {
        'format': 'compact-v1',
        'scope': 'Only reply/initiate review packaging is reduced; other operation objects retain Store contracts.',
        'decision_required': {'advance_to_ms': 'integer: intended state advance; past the commit time it is clamped to the commit time',
                              'next_decision_ms': 'integer: intended next review time; if past at submission, re-based on the commit time keeping the intended gap'},
        'decision_optional': ['dm_decisions', 'processed_event_ids', 'facts', 'activities',
                              'weekly_plans', 'appointments', 'metric_deltas', 'metric_reason',
                              'relationships', 'conversation_updates', 'continuation_ops',
                              'memory_ops', 'action_intents', 'world_event_ops', 'in_person_ops'],
        'executor_owned_omit': ['decision_id', 'expected_revision',
                                'conversation_review.context', 'conversation_review.draft_parts'],
        'new_dm': {'action': 'reply|initiate', 'channel': 'instagram|slack (explicit)',
                   'message_ids': 'reply only: model selects actual incoming IDs; never inferred',
                   'group_id': 'unique group ID chosen by model', 'due_ms': 'integer',
                   'parts': '1..5 strings; never truncated', 'reason': 'nonempty string',
                   'conversation_review': {
                       'previous_self': 'null or {message_id, point}; model selects ID and explains point',
                       'user_uptake': 'nonempty string for reply; null for initiate',
                       'topic': 'nonempty string',
                       'memory': {'kind': 'general|hobby',
                                  'topic_state_key': 'null for general; topic_state:key for hobby',
                                  'result': 'not_applicable for general; found|missing otherwise'},
                       'checks': {'self_reset': '{passed: boolean, evidence: nonempty string}',
                                  'missed_answer': '{passed: boolean, evidence: nonempty string}',
                                  'conflict': '{passed: boolean, evidence: nonempty string}',
                                  'self_consistency': '{passed: boolean, evidence: nonempty string, '
                                                      'claims: [{excerpt, fact_id}] (0..5)}'}},
                   'conversation_plan': 'Existing plan if required; do not fabricate units or ignore enforce.',
                   'other_fields': 'Existing routine/continuation fields, only when needed'},
        'mechanical_context': 'From acknowledged, delivered context only: last <=30 IDs per channel, '
                              'including model-selected previous_self if older. Targets copied from message_ids.',
        'recovery_actions': 'revalidate_group uses the full existing review shape; '
                            'request dm-actions schema. No saved draft is regenerated automatically.',
        'other_dm_actions': 'defer/no_reply/cancel_group/close_partial_group/resolve_message retain their '
                            'existing shapes; request dm-actions schema when needed.',
        'schema_topics': API_TOPICS,
        'schema_read': schema_read,
        'semantic_guards': 'Model supplies all checks, evidence, claims, uptake, topic, plan and memory. '
                           'Missing fields and failed checks remain errors. Original subject rules apply.',
        'prepared_revision': record['prepared']['snapshot']['revision'],
    }


def expand(record, decision):
    require(isinstance(decision, dict), 'invalid_decision_document')
    require(not ({'decision_id', 'expected_revision'} & decision.keys()), 'compact_managed_field')
    result = copy.deepcopy(decision)
    result['expected_revision'] = record['prepared']['snapshot']['revision']
    # Stable across crash/replay, different for a changed draft or reprepare.
    identity = [record['wake_id'], record['turn_id'], result]
    if record.get('decision_generation'):
        identity.append(record['decision_generation'])
    result['decision_id'] = 'compact:' + sha(encode(identity))
    dms = result.get('dm_decisions', [])
    require(isinstance(dms, list), 'invalid_decision_document')
    for dm in dms:
        require(isinstance(dm, dict), 'invalid_decision_document')
        if dm.get('action') not in ('reply', 'initiate'):
            continue
        channel = dm.get('channel')
        require(channel in ('instagram', 'slack'), 'reply_channel_required')
        review = dm.get('conversation_review')
        require(isinstance(review, dict) and set(review) == REVIEW_FIELDS, 'invalid_compact_review')
        parts = dm.get('parts')
        require(isinstance(parts, list), 'invalid_parts')
        # Only pages actually included in this packet (or retained in the same
        # acknowledged context). Do not use IDs from an unreturned manifest/DB.
        delivered = []
        for item in record['inputs']:
            content = item.get('content', {})
            if item.get('kind') == 'context' and content.get('channel') == channel:
                for message in content.get('messages', []):
                    mid = message['id']
                    if mid not in delivered:
                        delivered.append(mid)
        previous = review['previous_self']
        recent = delivered[-30:]
        if previous is not None:
            require(isinstance(previous, dict) and set(previous) == {'message_id', 'point'},
                    'invalid_review_previous_self')
            require(previous['message_id'] in delivered, 'review_previous_self_not_in_context')
            if previous['message_id'] not in recent:
                recent = [previous['message_id'], *recent[-29:]]
        targets = dm.get('message_ids', [])
        # No target selection, inferred previous_self, positive check or memory
        # default is supplied here. Store still validates the entire decision.
        dm['conversation_review'] = {
            'context': {'channel': channel, 'recent_message_ids': recent,
                        'target_message_ids': copy.deepcopy(targets),
                        **{key: review[key] for key in ('previous_self', 'user_uptake', 'topic')}},
            'memory': review['memory'], 'checks': review['checks'], 'draft_parts': copy.deepcopy(parts)}
    return result
