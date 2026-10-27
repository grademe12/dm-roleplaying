"""Private, bounded recovery copies. Drafts are evidence, never executable jobs."""
import json
import os
from pathlib import Path
import re
import tempfile

from common import encode, require
import instrumentation

MAX_DRAFTS = 50
RETENTION_MS = 7 * 24 * 60 * 60_000


class DraftArchive:
    def __init__(self, state_dir, session, clock):
        self.folder = Path(state_dir) / 'decision-drafts'
        self.session, self.clock = session, clock

    def path(self, draft_id):
        require(isinstance(draft_id, str) and re.fullmatch(r'[a-f0-9]{64}', draft_id), 'invalid_draft_id')
        return self.folder / (draft_id + '.json')

    def identifier(self, wake_id, turn_id, decision):
        return instrumentation.digest([self.session, wake_id, turn_id, decision])

    def files(self):
        return sorted((p for p in self.folder.glob('*.json')
                       if re.fullmatch(r'[a-f0-9]{64}\.json', p.name)),
                      key=lambda p: p.stat().st_mtime_ns, reverse=True)

    def save(self, wake_id, turn_id, decision, reason, prepared_revision):
        draft_id = self.identifier(wake_id, turn_id, decision)
        self.folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        record = {'draft_id': draft_id, 'session': self.session, 'source_wake_id': wake_id,
                  'source_turn_id': turn_id, 'saved_ms': self.clock(), 'reason': reason,
                  'prepared_revision': prepared_revision, 'decision': decision,
                  **instrumentation.decision_metadata(decision)}
        fd, temporary = tempfile.mkstemp(prefix='.draft-', dir=self.folder)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as handle:
                handle.write(encode(record))
            os.replace(temporary, self.path(draft_id))
        finally:
            Path(temporary).unlink(missing_ok=True)
        for index, path in enumerate(self.files()):
            if index >= MAX_DRAFTS:
                path.unlink(missing_ok=True)
                continue
            try:
                saved_ms = json.loads(path.read_text(encoding='utf-8'))['saved_ms']
            except (OSError, ValueError, KeyError, TypeError):
                continue
            if type(saved_ms) is int and self.clock() - saved_ms > RETENTION_MS:
                path.unlink(missing_ok=True)
        return draft_id

    def candidates(self, limit=5):
        result = []
        for path in self.files()[:MAX_DRAFTS]:
            try:
                record = json.loads(path.read_text(encoding='utf-8'))
                if record['session'] != self.session or self.clock() - record['saved_ms'] > RETENTION_MS:
                    continue
                result.append({k: record[k] for k in ('draft_id', 'source_wake_id', 'source_turn_id',
                    'saved_ms', 'reason', 'prepared_revision', 'decision_id', 'decision_digest')})
                if len(result) >= limit:
                    break
            except (OSError, ValueError, KeyError, TypeError):
                continue
        return result

    def read(self, draft_id):
        record = json.loads(self.path(draft_id).read_text(encoding='utf-8'))
        require(record['session'] == self.session, 'draft_session_mismatch')
        require(self.clock() - record['saved_ms'] <= RETENTION_MS, 'draft_expired')
        require(record['draft_id'] == draft_id and
                self.identifier(record['source_wake_id'], record['source_turn_id'], record['decision']) == draft_id,
                'draft_digest_mismatch')
        return {**record, 'can_apply_unchanged': False,
                'next': 'prepare_and_review_changes', 'warning_review_reusable': False}

    def resolve(self, wake_id, turn_id, decision):
        self.path(self.identifier(wake_id, turn_id, decision)).unlink(missing_ok=True)
