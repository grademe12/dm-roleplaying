"""Reuse content, never expired claims/receipts/reviews or old generator bindings."""
import copy
import re

from common import ControlError
import gate
import role_contract


def scope(worker):
    return {k:v for k,v in worker.binding.items() if k != 'wake'}


def writer_key(payload, instructions, options):
    value = copy.deepcopy(payload)
    value.get('reply', {}).get('current', {}).pop('now_ms', None)
    value.get('reply', {}).get('current', {}).get('state', {}).pop('revision', None)
    planned = value.get('reply', {}).get('proposed_state', {})
    # Controller review clocks do not change the wording task. Story/activity dates remain exact.
    for name in ('advance_to_ms', 'next_decision_ms'):
        planned.pop(name, None)
    return role_contract.digest({'input': value, 'instructions': instructions,
                                 'model': {k:v for k,v in options.items() if k != 'timeout_seconds'}})


def folder(worker):
    return worker.folder / 'draft-recovery'


def remember(worker, pending, output):
    key = writer_key(pending['request']['input'], pending['request']['instructions'], pending['options'])
    value = {'scope': scope(worker), 'key': key, 'output': copy.deepcopy(output),
             'source_wake': worker.session.wake, 'created_ms': worker.clock(),
             'expires_ms': worker.clock() + worker.opts['resume_ttl_ms']}
    target = folder(worker)
    target.mkdir(parents=True, exist_ok=True, mode=0o700); target.chmod(0o700)
    # Caller has the role-executor lock; save is atomic/private.
    from muse_executor import private_write
    from common import encode
    private_write(target / (key + '.json'), encode(value))
    return key


def invalidate(worker, draft):
    key=draft.get('recovery_key')
    if key:
        if not re.fullmatch(r'[0-9a-f]{64}', key):raise ValueError('invalid draft recovery key')
        (folder(worker)/(key+'.json')).unlink(missing_ok=True)


def find_draft(worker, payload, instructions, options):
    key = writer_key(payload, instructions, options)
    try:
        value = gate.load_json(folder(worker) / (key + '.json'), None)
        if (not value or value.get('scope') != scope(worker) or value.get('key') != key
                or not worker.clock() < value['expires_ms']):
            return None
        activity = payload.get('reply', {}).get('current', {}).get('activity') or {}
        if activity.get('status') == 'in_progress' and activity.get('planned_end_ms', worker.clock()+1) <= worker.clock():
            changes = payload.get('reply', {}).get('proposed_state', {}).get('activities', [])
            closed = any(item.get('id') == activity.get('id') and item.get('status') in ('completed', 'interrupted')
                         and item.get('actual_end_ms', worker.clock()+1) <= worker.clock() for item in changes)
            successor = any(item.get('status', 'in_progress') == 'in_progress' and item.get('start_ms', worker.clock()+1) <= worker.clock()
                            < item.get('planned_end_ms', 0) for item in changes)
            if not (closed and successor):return None
        # Re-parse the stored wording. Current source/binding and final review are rebuilt by the caller.
        output = role_contract.writer_output(value['output'])
        return {**value, 'output': output}
    except (OSError, ValueError, KeyError, TypeError, ControlError):
        return None  # Cache failure never blocks normal writing.


def find_candidate(worker, record):
    """Old proposals are only data for a newly authorized model's revalidation."""
    try:
        paths = sorted(worker.folder.glob('run-*.json'), key=lambda p:p.stat().st_mtime, reverse=True)[:20]
        with worker.session.store.connect() as db:
            pending = {r[0] for r in db.execute("SELECT id FROM dm_messages WHERE session_id=? "
                       "AND status IN ('received','deferred','queued')", (worker.session.session,))}
            for path in paths:
                if path == worker.path:
                    continue
                state = gate.load_json(path, None)
                if not state or {k:v for k,v in state.get('binding',{}).items() if k != 'wake'} != scope(worker):
                    continue
                if not re.fullmatch(r'[A-Za-z0-9_:-]{1,80}', state['binding'].get('wake', '')):
                    continue
                decision = state.get('decision') or {}
                targets = {mid for dm in decision.get('dm_decisions', [])
                           if dm.get('action') == 'reply' for mid in dm.get('message_ids', [])}
                if not targets or not targets <= pending:
                    continue
                old_path = worker.session.state_dir / 'muse-executor' / state['binding']['wake'] / 'current.json'
                old = gate.load_json(old_path, {})
                timestamp = state.get('updated_ms', old.get('prepared', {}).get('snapshot', {}).get('now_ms', 0))
                if not 0 <= worker.clock() - timestamp <= worker.opts['resume_ttl_ms']:
                    continue
                response = old.get('response') or {}
                applied = old.get('expanded_decision')
                if applied is None:
                    applied = ((old.get('warning') or {}).get('decision', {}) if response.get('action') == 'approve_warning'
                               else response.get('decision', {}))
                if applied.get('decision_id') and db.execute('SELECT 1 FROM decisions WHERE session_id=? AND id=?',
                        (worker.session.session, applied['decision_id'])).fetchone():
                    continue
                if state.get('result', {}).get('committed'):
                    continue
                old_manifest = old.get('prepared', {}).get('snapshot', {}).get('work_manifest', {})
                current_manifest = record['prepared']['snapshot']['work_manifest']
                unchanged = (old.get('prepared', {}).get('snapshot', {}).get('revision') ==
                             record['prepared']['snapshot']['revision'] and
                             old_manifest.get('life_required') == current_manifest.get('life_required'))
                return {'source_wake': state['binding']['wake'], 'decision': copy.deepcopy(decision),
                        'briefs': copy.deepcopy(state.get('briefs', {})),
                        'drafts':{str(i):copy.deepcopy(value['output']) for i,value in state.get('drafts',{}).items()},
                        'cache_disabled':state.get('cache_disabled',False),
                        'life': copy.deepcopy(state.get('life', {})) if unchanged else None,
                        'note': 'Historical uncommitted proposal only. Revalidate against all current inputs. '
                                'No old claim, receipt, revision, time budget or review approval is inherited.'}
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None
