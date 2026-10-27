"""Reply-phrasing repetition detector (design: operations/repetition-guard-plan-20261026.md).

String-level measurement only: no semantic judgment, no network, no model, no
DB writes. Pure functions over the prepared channel context — the same
messages the writing model actually sees — so results can be unit tested with
synthetic conversations.

Modes (config `repetition_guard.mode`): off (default; callers must not change
any existing output), observe (metrics to trace only), shadow (metrics plus a
warning merged into the existing warning-review receipt flow). No enforce mode.
"""
import difflib
import re

from common import ControlError, integer, require

MODES = ('off', 'observe', 'shadow')
DEFAULTS = {'mode': 'off', 'recent_turns': 8, 'min_turns_for_phrase': 2,
            'max_phrases': 12, 'verbatim_chars': 25, 'phrase_hit_turns': 3,
            'edge_turns': 3}
NOTE = ('참고용 관측값. 이 구절·시작·마무리를 그대로 다시 쓰지 않는다. '
        '금지어 목록이 아니며 의미상 필요하면 사용할 수 있다.')
_PLACEHOLDER = re.compile(r'^\[[^\]]+\]$')
_PUNCT = re.compile(r'[^\w\s]', re.UNICODE)
# Verbatim comparison always uses the most recent self turns, per the design.
VERBATIM_TURNS = 4


def settings(cfg):
    raw = cfg.get('repetition_guard') or {}
    require(isinstance(raw, dict), 'invalid_repetition_guard_config')
    result = {**DEFAULTS, **raw}
    require(result['mode'] in MODES, 'invalid_repetition_guard_config')
    for key in ('recent_turns', 'min_turns_for_phrase', 'max_phrases',
                'verbatim_chars', 'phrase_hit_turns', 'edge_turns'):
        integer(result[key], 'repetition_' + key, 1)
    return result


def guard_settings(cfg):
    """Optional config must never make the core reply path unavailable."""
    try:
        return settings(cfg)
    except (ControlError, TypeError, ValueError, KeyError):
        return dict(DEFAULTS)


def normalize(text):
    """Comparison-only form: collapse laughter/sob runs, drop punctuation and
    ellipses, tidy whitespace. Output phrases keep the original wording."""
    value = text or ''
    value = re.sub(r'ㅋ{2,}', 'ㅋ', value)
    value = re.sub(r'ㅎ{2,}', 'ㅎ', value)
    value = re.sub(r'ㅠ{2,}', 'ㅠ', value)
    value = _PUNCT.sub('', value)
    return ' '.join(value.split())


def is_placeholder(text):
    return bool(_PLACEHOLDER.match((text or '').strip()))


def build_turns(messages, recent_turns):
    """Group consecutive outgoing messages into self turns.

    Photo/attachment placeholders (a bracket token alone) are skipped without
    breaking the turn. Returns the most recent `recent_turns` turns; each turn
    is a list of the original balloon texts in order.
    """
    turns = []
    current = []
    for message in messages or []:
        if message.get('direction') != 'outgoing':
            if current:
                turns.append(current); current = []
            continue
        value = message.get('text') or ''
        if is_placeholder(value):
            continue
        if value.strip():
            current.append(value)
    if current:
        turns.append(current)
    return turns[-max(1, recent_turns):]


def _norm_words(text):
    return normalize(text).split()


def _original_phrase(turns, gram):
    """Recover the original wording of a normalized word n-gram from a turn."""
    for turn in turns:
        for balloon in turn:
            words = balloon.split()
            norms = [_norm_words(word) for word in words]
            flat = [part for group in norms for part in group]
            size = len(gram)
            for start in range(0, len(flat) - size + 1):
                if tuple(flat[start:start + size]) == gram:
                    # Map the flat span back to whole original words.
                    index = 0; chosen = []
                    for word, group in zip(words, norms):
                        if group and index + len(group) > start and index < start + size:
                            chosen.append(word)
                        index += len(group)
                    if chosen:
                        return ' '.join(chosen)
    return ' '.join(gram)


def repeated_phrases(turns, opts):
    """Word 2-4 grams appearing in at least min_turns_for_phrase distinct turns.

    Shorter grams contained in a longer qualifying gram are dropped, as are
    grams made only of 1-2 character words. Sorted by (turns, length) desc.
    """
    counts = {}
    for turn_index, turn in enumerate(turns):
        seen = set()
        for balloon in turn:
            words = _norm_words(balloon)
            for size in (2, 3, 4):
                for start in range(0, len(words) - size + 1):
                    gram = tuple(words[start:start + size])
                    if all(len(word) <= 2 for word in gram):
                        continue
                    seen.add(gram)
        for gram in seen:
            counts.setdefault(gram, set()).add(turn_index)
    minimum = opts['min_turns_for_phrase']
    candidates = {gram: len(found) for gram, found in counts.items() if len(found) >= minimum}

    def contained(short, long):
        size = len(short)
        return any(long[i:i + size] == short for i in range(len(long) - size + 1))

    kept = [gram for gram in candidates
            if not any(other is not gram and len(other) > len(gram) and contained(gram, other)
                       for other in candidates)]
    kept.sort(key=lambda gram: (-candidates[gram], -len(gram),
                                -sum(len(word) for word in gram), gram))
    return [{'text': _original_phrase(turns, gram), 'norm': ' '.join(gram),
             'turns': candidates[gram]} for gram in kept[:opts['max_phrases']]]


def _edge_key(text, opener):
    """First/last word 3-gram; balloons shorter than 3 words are not comparable."""
    words = _norm_words(text)
    if len(words) < 3:
        return None
    return tuple(words[:3]) if opener else tuple(words[-3:])


def analyze(channel, messages, opts):
    """Build the public cues and the private draft-check data for one channel."""
    return analyze_turns(channel, build_turns(messages, opts['recent_turns']), opts)


def analyze_turns(channel, turns, opts):
    """Same as analyze(), starting from already-grouped self turns."""
    phrases = repeated_phrases(turns, opts)
    edge = turns[-opts['edge_turns']:]
    cues = {'channel': channel, 'turns_considered': len(turns),
            'recent_phrases': [{'text': phrase['text'], 'turns': phrase['turns']}
                               for phrase in phrases],
            'recent_openers': [turn[0][:25] for turn in edge],
            'recent_closers': [turn[-1][-30:] for turn in edge],
            'note': NOTE}
    check = {'phrases': phrases,
             'opener_keys': [key for key in (_edge_key(turn[0], True) for turn in edge)
                             if key is not None],
             'closer_keys': [key for key in (_edge_key(turn[-1], False) for turn in edge)
                             if key is not None],
             'recent_texts': [normalize(' '.join(turn)) for turn in turns[-VERBATIM_TURNS:]],
             'prev_turn_chars': sum(len(balloon) for balloon in turns[-1]) if turns else 0,
             'turns_considered': len(turns)}
    return {'cues': cues, 'check': check}


def check_drafts(parts, check, opts):
    """Compare one draft (reply/initiate parts) with the recent self turns.

    Only text parts count; photo placeholders are ignored. Returns metrics
    (trace-safe numbers/booleans) plus the matched original phrases, which
    belong in warnings and private outputs only, never in trace.
    """
    texts = [part for part in parts or [] if isinstance(part, str)]
    metrics = {'verbatim_max': 0, 'phrase_hits': 0, 'opener_repeat': False,
               'closer_repeat': False, 'draft_chars': sum(len(part) for part in texts),
               'prev_turn_chars': check['prev_turn_chars'],
               'turns_considered': check['turns_considered'], 'matched_phrases': []}
    if not texts or not check['turns_considered']:
        return metrics
    normalized = [normalize(part) for part in texts]
    for part in normalized:
        if not part:
            continue
        for recent in check['recent_texts']:
            if not recent:
                continue
            match = difflib.SequenceMatcher(None, part, recent, autojunk=False).find_longest_match()
            metrics['verbatim_max'] = max(metrics['verbatim_max'], match.size)
    joined = ' '.join(normalized)
    for phrase in check['phrases']:
        if phrase['turns'] >= opts['phrase_hit_turns'] and phrase['norm'] in joined:
            metrics['phrase_hits'] += 1
            metrics['matched_phrases'].append(phrase['text'])
    opener = _edge_key(texts[0], True)
    closer = _edge_key(texts[-1], False)
    metrics['opener_repeat'] = opener is not None and opener in check['opener_keys']
    metrics['closer_repeat'] = closer is not None and closer in check['closer_keys']
    return metrics


def exceeds(metrics, opts):
    return (metrics['verbatim_max'] >= opts['verbatim_chars'] or metrics['phrase_hits'] > 0
            or metrics['opener_repeat'] or metrics['closer_repeat'])


def evaluate(state, decision, opts):
    """Aggregate draft checks over a full (expanded) decision.

    Returns (metrics, warnings) where warnings maps group_id to shadow warning
    entries in the existing warning shape. Groups without parts or without a
    prepared check for their channel are skipped.
    """
    checks = state.get('repetition_check') or {}
    aggregate = {'verbatim_max': 0, 'phrase_hits': 0, 'opener_repeat': False,
                 'closer_repeat': False, 'draft_chars': 0, 'prev_turn_chars': 0,
                 'turns_considered': 0}
    warnings = {}
    for dm in decision.get('dm_decisions', []):
        if not isinstance(dm, dict) or dm.get('action') not in ('reply', 'initiate'):
            continue
        channel = dm.get('channel') or (dm.get('conversation_review') or {}).get('context', {}).get('channel')
        check = checks.get(channel)
        if check is None:
            continue
        metrics = check_drafts(dm.get('parts', []), check, opts)
        aggregate['verbatim_max'] = max(aggregate['verbatim_max'], metrics['verbatim_max'])
        aggregate['phrase_hits'] += metrics['phrase_hits']
        aggregate['opener_repeat'] = aggregate['opener_repeat'] or metrics['opener_repeat']
        aggregate['closer_repeat'] = aggregate['closer_repeat'] or metrics['closer_repeat']
        aggregate['draft_chars'] += metrics['draft_chars']
        aggregate['prev_turn_chars'] = max(aggregate['prev_turn_chars'], metrics['prev_turn_chars'])
        aggregate['turns_considered'] = max(aggregate['turns_considered'], metrics['turns_considered'])
        if exceeds(metrics, opts) and dm.get('group_id'):
            warnings.setdefault(dm['group_id'], []).append({
                'reason': 'repetition_detected',
                'verbatim_max': metrics['verbatim_max'], 'phrase_hits': metrics['phrase_hits'],
                'opener_repeat': metrics['opener_repeat'], 'closer_repeat': metrics['closer_repeat'],
                'phrases': metrics['matched_phrases']})
    return aggregate, warnings
