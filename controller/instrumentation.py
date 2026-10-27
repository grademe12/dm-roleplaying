"""Content-free diagnostics. Logging failure must never change a storage result."""
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from common import encode


QUERY_KINDS = frozenset(('topic_state', 'identity', 'self_said', 'hobby_profile',
                         'ref', 'relationship'))


def digest(value):
    return hashlib.sha256(encode(value).encode('utf-8')).hexdigest()


def decision_metadata(decision):
    identifier = decision.get('decision_id')
    # Decision IDs are user/model supplied and have no format restriction in Store.
    if not isinstance(identifier, str) or not re.fullmatch(r'[A-Za-z0-9_:-]{1,80}', identifier):
        identifier = 'sha256:' + digest(identifier)
    expected = decision.get('expected_revision')
    return {'decision_id': identifier, 'decision_digest': digest(decision),
            'draft_digest': digest([{'action': dm.get('action'), 'parts': dm.get('parts')}
                                    for dm in decision.get('dm_decisions', []) if isinstance(dm, dict)]),
            'expected_revision': expected if type(expected) is int else None}


def warning_codes(groups):
    return sorted({warning['reason'] for group in groups.values()
                   for warning in group.get('warnings', [])
                   if isinstance(warning.get('reason'), str)
                   and re.fullmatch(r'[a-z][a-z0-9_]{0,79}', warning['reason'])})


def error_warning_codes(error):
    try:
        detail = json.loads(error.detail or '{}')
        return warning_codes({'error': {'warnings': detail.get('conflicts', [])}})
    except (ValueError, TypeError, AttributeError, KeyError):
        return []


def recall_metadata(keywords, result):
    kinds = {k.split(':', 1)[0] if k.split(':', 1)[0] in QUERY_KINDS else 'other'
             for k in keywords}
    return {'query_kinds': sorted(kinds) if keywords else ['recent'],
            'result_count': len(result['facts']), 'output_bytes': len(encode(result).encode('utf-8'))}


def revision_event(db_path, operation, session, before, after, started_ms, finished_ms):
    """Best effort after COMMIT. Revision numbers order concurrent writers, not file order."""
    try:
        folder = Path(db_path).parent / 'logs'
        folder.mkdir(parents=True, exist_ok=True)
        stamp = datetime.fromtimestamp(finished_ms / 1000, timezone.utc)
        entry = {'event': 'revision_changed', 'at_ms': finished_ms,
                 'started_ms': started_ms, 'finished_ms': finished_ms,
                 'operation': operation, 'session_hash': hashlib.sha256(session.encode()).hexdigest()[:12],
                 'revision_before': before, 'revision_after': after, 'pid': os.getpid()}
        path = folder / ('revision-' + stamp.strftime('%Y-%m-%d') + '.jsonl')
        with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600), 'a', encoding='utf-8') as handle:
            handle.write(encode(entry) + '\n')
    except OSError:
        # No retry/DB write: an unavailable diagnostic sink cannot undo a committed action.
        pass
