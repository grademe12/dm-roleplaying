#!/usr/bin/env python3
"""Durable, code-owned decision loop. Models return proposals, never drive retries.

One driver owns one wake, with either the existing Muse executor or role executor.
`start` detaches the driver; `status` observes it without claiming or renewing work.
File/Dropbox waits and executor contention are pending states, not completion.
"""
import argparse
from contextlib import redirect_stdout
import fcntl
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
import uuid

from common import ControlError, encode, file_lock, integer, now_ms, require
import gate
import driver_ownership
import model_adapter
import muse_executor
import role_executor

HERE = Path(__file__).resolve().parent


def settings(cfg):
    raw = cfg.get('decision_driver') or {}
    require(isinstance(raw, dict), 'invalid_decision_driver')
    if raw.get('enabled') is not True:
        return {'enabled': False}
    opts = {'mode': 'muse', 'poll_seconds': 2, 'max_steps': 128,
            'max_transport_attempts': 2, 'dropbox': False, **raw}
    require(opts['mode'] in ('muse', 'roles') and type(opts['dropbox']) is bool,
            'invalid_decision_driver')
    for name, upper in (('poll_seconds', 30), ('max_steps', 512), ('max_transport_attempts', 4)):
        integer(opts[name], name, 1)
        require(opts[name] <= upper, 'invalid_' + name)
    opts['path'] = Path(opts.get('path', Path(cfg['state_dir']) / 'decision-driver')).resolve()
    if opts['mode'] == 'roles':
        require(role_executor.settings(cfg)['enabled'], 'role_execution_off')
    else:
        model_adapter.options(cfg, opts.get('adapter'))
    return opts


def load_config(path):
    cfg = role_executor.load_config(path)
    raw = cfg.get('decision_driver')
    if isinstance(raw, dict):
        for key in ('path', 'dropbox_config_dir'):
            if raw.get(key):
                value = Path(raw[key]).expanduser()
                raw[key] = str(value if value.is_absolute() else Path(path).resolve().parent / value)
    return cfg


def read_json(path):
    with Path(path).open('rb') as stream:
        raw = stream.read(model_adapter.MAX_RESPONSE_BYTES + 1)
    require(len(raw) <= model_adapter.MAX_RESPONSE_BYTES, 'driver_response_too_large')
    return json.loads(raw.decode('utf-8'))


def save(path, value):
    muse_executor.private_write(path, encode(value))


def alive(pid):
    if type(pid) is not int or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def view(record):
    """Metadata only. A stopped/failed/handed-off job is not completed work."""
    keys = ('job_id', 'wake_id', 'phase', 'reason', 'terminal', 'completed', 'worker_pid',
            'updated_ms', 'steps', 'mode', 'delivery')
    result = {key: record[key] for key in keys if key in record}
    result.update(ok=record.get('phase') not in ('failed', 'stopped'),
                  next='done' if record.get('terminal') else 'status',
                  worker_alive=alive(record.get('worker_pid')))
    if record.get('pending'):
        result['request_id'] = record['pending']['request']['request_id']
    elif record.get('waiting_request_id'):
        result['request_id'] = record['waiting_request_id']
    return result


class DecisionDriver:
    def __init__(self, cfg, record, *, clock=now_ms, sleep=time.sleep, session=None,
                 exchange=None, transport=None):
        self.cfg, self.opts, self.record = cfg, settings(cfg), record
        self.folder = self.opts['path']
        self.path = self.folder / 'current.json'
        self.clock, self.sleep = clock, sleep
        self.session, self.exchange_callback, self.transport = session, exchange, transport
        self.client = None

    def persist(self, **fields):
        self.record.update(fields, updated_ms=self.clock())
        save(self.path, self.record)

    def finish(self, phase, reason, completed=False):
        self.persist(phase=phase, reason=reason, terminal=True, completed=completed)
        return view(self.record)

    def remaining(self):
        if not self.record.get('wake_id'):
            return max(0, self.record['started_ms'] + 30_000 - self.clock())
        wake = gate.load_json(Path(self.cfg['state_dir']) / 'wake.json', {})
        require(wake.get('wake_id') == self.record['wake_id'], 'stale_claim')
        deadline = min(wake.get('claim_deadline_ms', self.record['started_ms'] + gate.MAX_CLAIM_RUN_MS),
                       self.record['started_ms'] + gate.MAX_CLAIM_RUN_MS)
        if self.session.path.is_file():
            return min(deadline - self.clock(), self.session.budget(self.session.state())['remaining_ms'])
        return deadline - self.clock()

    def wait(self, reason):
        request = self.record.get('waiting_request_id') or ((self.record.get('pending') or {}).get('request') or {}).get('request_id')
        wait_key = [reason, request]
        if self.session is not None and self.record.get('last_wait_key') != wait_key:
            self.session.event('driver_wait', reason=reason, request_id=request, job_id=self.record['job_id'])
            self.record['last_wait_key'] = wait_key
        self.persist(phase='waiting', reason=reason)
        require(self.remaining() > 0, 'driver_deadline')
        if self.session is not None:
            ack = gate.load_json(Path(self.cfg['state_dir']) / 'wake_ack.json', {})
            if ack.get('phase') == 'started':
                with self.session.owned():
                    pass
        self.sleep(max(0, min(self.opts['poll_seconds'], self.remaining() / 1000)))

    def release(self, reason):
        """Failure handoff only for our current wake, preserving jobs and decisions."""
        if self.session is not None:
            self.session.event('driver_stopped', reason=reason, job_id=self.record['job_id'])
            try:
                with self.session.owned():
                    self.session.store.end_conversation(self.session.session, self.clock())
                    gate.write_json(Path(self.cfg['state_dir']) / 'wake_ack.json', {
                        'wake_id': self.session.wake, 'phase': 'failed', 'at_ms': self.clock(),
                        'reason': reason, 'source': 'decision_driver'})
            except ControlError as exc:
                if exc.reason not in ('busy', 'stale_claim'):
                    raise
        # Stop advertising abandoned inputs. Preserve paid response files and private journals for inspection.
        if self.opts['mode'] == 'muse' and self.record.get('pending'):
            ident = self.record['pending']['request']['request_id']
            (self.folder / ('decision-model-input-' + ident + '.json')).unlink(missing_ok=True)
        elif self.opts['mode'] == 'roles' and self.session is not None:
            worker = role_executor.RoleExecutor(self.cfg, self.session, self.clock)
            pending = (worker.load() or {}).get('pending')
            if pending:
                (worker.folder / ('role-model-input-' + pending['id'] + '.json')).unlink(missing_ok=True)
        return self.finish('stopped', reason)

    def bind(self):
        if not self.record.get('wake_id'):
            with redirect_stdout(io.StringIO()):
                args = (self.cfg, self.record['invocation_id']) if self.record.get('invocation_id') else (self.cfg,)
                code, claimed = gate.claim_work(*args)
            if code == gate.SILENT:
                return self.finish('skipped', 'no_claim')
            require(code == gate.WAKE and claimed, 'executor_claim_failed')
            self.persist(wake_id=claimed['wake_id'])
        if self.session is None:
            self.session = muse_executor.DecisionSession(self.cfg, self.record['wake_id'], self.clock, self.sleep)
        return None

    def exchange(self):
        if self.exchange_callback is not None:
            self.exchange_callback(self)
            return
        if not self.opts['dropbox']:
            return
        import memory_exchange_bridge as bridge
        import role_exchange_bridge
        profile = (role_exchange_bridge.PROFILE if self.opts['mode'] == 'roles' else
                   bridge.Profile('decision', '/yujin-project/decision-exchange', 'decision_driver.py',
                                  None, 'decision-bridge-state.json', 'decision-bridge.lock'))
        folder = role_executor.settings(self.cfg)['path'] if self.opts['mode'] == 'roles' else self.folder
        try:
            fd = os.open(folder / profile.lock_name, os.O_WRONLY | os.O_CREAT, 0o600)
            with os.fdopen(fd, 'a') as handle:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    raise ControlError('busy') from exc
                if self.client is None:
                    self.client = bridge.RealDropbox(self.opts.get('dropbox_config_dir'))
                result = bridge.run_pass(self.client, folder, folder / profile.state_name,
                                         remote_root=self.opts.get('remote_root', profile.remote_root),
                                         now=self.clock() / 1000, profile=profile)
                if not result['ok']:
                    self.persist(reason='exchange_retry')
        except bridge.DropboxError:
            self.persist(reason='exchange_retry')

    def packet(self, executor, record):
        """Reuse the executor's original acknowledged input, never synthesize decisions."""
        pending = self.record.get('pending')
        if pending and pending['request']['request_id'] == record['request_id']:
            return pending
        self.cleanup()
        opts = model_adapter.options(self.cfg, self.opts.get('adapter'))
        instructions = ('Read the complete original Muse input. Return output as the full Muse response '
                        'envelope specified by response_envelope (including its request_id/input_sha256, '
                        'input_read and action). All semantic judgment/review is yours. Do not execute '
                        'tools, submit to the controller, or drive retries. Use action=read for needed '
                        'sources. Each request is independent: do not set retain_context=true.')
        req = model_adapter.request('persona_decision', record['request_id'], record['input_sha256'],
            instructions, {'original_input': Path(record['input_path']).read_text(encoding='utf-8'),
                           'response_envelope': {'request_id': record['request_id'],
                               'input_sha256': record['input_sha256'], 'input_read': True,
                               'action': 'read|commit|approve_warning|close'}}, opts,
            self.clock() + self.remaining())
        pending = {'request': req, 'options': opts, 'attempts': 0}
        self.persist(pending=pending)
        return pending

    def cleanup(self):
        pending = self.record.get('pending')
        if pending:
            ident = pending['request']['request_id']
            for kind in ('input', 'response'):
                (self.folder / ('decision-model-' + kind + '-' + ident + '.json')).unlink(missing_ok=True)
            self.persist(pending=None)

    def muse_step(self):
        executor = muse_executor.MuseExecutor(self.session)
        record = executor.load() if executor.path.is_file() else None
        if self.record.get('prepare_next') or record is None or record['stage'] == 'invalidated':
            result = executor.prepare(decision_format='compact-v1')
            self.persist(prepare_next=False, pending=None)
            return result
        if record['stage'] in ('committing', 'completed'):
            return executor.recover()
        require(record['stage'] == 'awaiting', 'executor_nothing_to_recover')
        pending = self.packet(executor, record)
        ident = pending['request']['request_id']
        path = self.folder / ('decision-model-response-' + ident + '.json')
        opts = pending['options']
        if not path.is_file():
            require(self.remaining() > 1000, 'driver_deadline')
            if opts['backend'] == 'file':
                save(self.folder / ('decision-model-input-' + ident + '.json'), pending['request'])
                self.exchange()
                if not path.is_file():
                    self.wait('model_response_pending')
                    return None
            else:
                require(pending['attempts'] < self.opts['max_transport_attempts'], 'driver_transport_exhausted')
                pending['attempts'] += 1
                self.persist(pending=pending, phase='model_running')
                response = model_adapter.invoke(opts, pending['request'],
                    min(opts['timeout_seconds'], self.remaining() / 1000), self.transport)
                save(path, response)  # Paid result survives apply/finish failures and driver restarts.
        value = model_adapter.validate_result(read_json(path), pending['request'])
        require(self.clock() < pending['request']['deadline_ms'], 'driver_deadline')
        require(isinstance(value['output'], dict) and not value['output'].get('retain_context', False),
                'driver_independent_input_required')
        return executor.respond(value['output'])

    def accept_result(self, result):
        # Persist before interpreting after.next so a crash never loses a continuation.
        self.persist(result=result)
        require(isinstance(result, dict), 'driver_invalid_result')
        if result.get('committed'):
            executor = muse_executor.MuseExecutor(self.session)
            saved = executor.load()
            response = saved.get('response') or {}
            decision = saved.get('expanded_decision')
            if decision is None:
                decision = ((saved.get('warning') or {}).get('decision', {})
                            if response.get('action') == 'approve_warning' else response.get('decision', {}))
            groups = set(self.record.get('group_ids', []))
            groups.update(item['group_id'] for item in decision.get('dm_decisions', []) if item.get('group_id'))
            self.persist(group_ids=sorted(groups))
        nxt = result.get('after', {}).get('next', result.get('next'))
        if nxt == 'done':
            ack = gate.load_json(Path(self.cfg['state_dir']) / 'wake_ack.json', {})
            handed_off = result.get('after', {}).get('reason', result.get('reason')) == 'worker_budget'
            good = (result.get('ok') is True and ack.get('wake_id') == self.record['wake_id']
                    and ack.get('phase') == 'finished' and not handed_off)
            if good:
                self.cleanup()
                statuses = {}
                with self.session.store.connect() as db:
                    for gid in self.record.get('group_ids', []):
                        for row in db.execute('SELECT status,count(*) AS n FROM dm_jobs '
                                              'WHERE session_id=? AND group_id=? GROUP BY status',
                                              (self.session.session, gid)):
                            statuses[row['status']] = statuses.get(row['status'], 0) + row['n']
                self.persist(delivery=statuses)
                if any(statuses.get(s, 0) for s in ('pending', 'attempting', 'submitted', 'unknown', 'failed')):
                    # Poller/reconciliation own this queue. No new model session just to wait for pixels.
                    return self.finish('handed_off', 'delivery_pending')
            return self.finish('completed' if good else 'stopped',
                               'session_finished' if good else result.get('reason', 'session_handoff'), good)
        if nxt == 'stop':
            return self.finish('stopped', result.get('reason', 'stale_claim'))
        require(nxt in ('muse', 'model', 'run', 'prepare', 'finish', 'retry_same_commit', 'respond'),
                'driver_unknown_next')
        if result.get('ok') is False and nxt not in ('prepare', 'finish', 'retry_same_commit'):
            raise ControlError(result.get('reason', 'driver_step_failed'))
        if self.opts['mode'] == 'muse' and nxt == 'prepare':
            self.cleanup()
            self.persist(prepare_next=True)
        if nxt == 'model':
            ident = result['request_id']
            steps = self.record['steps'] + (ident != self.record.get('waiting_request_id'))
            self.persist(waiting_request_id=ident, steps=steps, result=None)
            self.exchange()
            self.wait('model_response_pending')
        else:
            self.persist(result=None, steps=self.record['steps'] + 1)
        return None

    def drive(self):
        self.persist(worker_pid=os.getpid(), phase='running')
        try:
            while not self.record.get('terminal'):
                try:
                    if self.record.get('result') is not None:
                        result = self.accept_result(self.record['result'])
                        if result is not None:
                            return result
                        continue
                    result = self.bind()
                    if result is not None:
                        return result
                    require(self.record['steps'] < self.opts['max_steps'], 'driver_step_budget')
                    # A crash after the final ACK may be replayed read-only, even after the deadline.
                    cached = muse_executor.MuseExecutor(self.session)
                    saved = cached.load() if cached.path.is_file() else None
                    if (saved and saved.get('stage') == 'completed'
                            and saved.get('result', {}).get('after', {}).get('next') == 'done'):
                        self.persist(result=saved['result'])
                        continue
                    require(self.remaining() > 0, 'driver_deadline')
                    self.persist(phase='running')
                    result = (role_executor.entry(self.cfg, self.record['wake_id'], clock=self.clock,
                                                 transport=self.transport)
                              if self.opts['mode'] == 'roles' else self.muse_step())
                    if result is not None:
                        self.persist(result=result)
                except ControlError as exc:
                    if exc.reason == 'busy':
                        self.wait('executor_busy')
                    elif exc.reason in muse_executor.REPREPARE:
                        self.cleanup()
                        self.persist(prepare_next=True, result=None, steps=self.record['steps'] + 1)
                    elif exc.reason in ('network_error', 'model_timeout', 'model_process_unavailable'):
                        self.wait(exc.reason)
                    else:
                        return self.release(exc.reason)
                except (OSError, sqlite3.OperationalError):
                    # Keep saved responses/commit intent; retry storage contention within the original deadline.
                    self.wait('executor_io_pending')
                except (ValueError, KeyError, TypeError):
                    return self.release('driver_invalid_response')
            return view(self.record)
        except ControlError as exc:
            return self.release(exc.reason)


def binding(cfg, opts):
    return {'db': str(Path(cfg['db']).resolve()), 'session': cfg['session'],
            'state_dir': str(Path(cfg['state_dir']).resolve()), 'mode': opts['mode']}


def status(cfg):
    opts = settings(cfg)
    if not opts['enabled']:
        return {'ok': True, 'reason': 'off', 'next': 'done', 'completed': False, 'terminal': True}
    record = gate.load_json(opts['path'] / 'current.json', None)
    launch = gate.load_json(opts['path'] / 'launch.json', {})
    if alive(launch.get('pid')) and (record is None or launch.get('started_ms', 0) > record.get('updated_ms', 0)):
        return {'ok': True, 'phase': 'starting', 'next': 'status', 'worker_pid': launch['pid'],
                'wake_id': launch.get('wake_id'), 'completed': False, 'terminal': False, 'worker_alive': True}
    if record is None:
        return {'ok': True, 'phase': 'idle', 'next': 'run', 'completed': False, 'terminal': False}
    require(record['binding'] == binding(cfg, opts), 'driver_binding_mismatch')
    return view(record)


def run(cfg, wake=None, invocation_id=None, *, clock=now_ms, sleep=time.sleep,
        session=None, exchange=None, transport=None):
    opts = settings(cfg)
    if not opts['enabled']:
        return status(cfg)
    folder = opts['path']
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    folder.chmod(0o700)
    try:
        with file_lock(folder / 'driver', 'decision-driver', private=True):
            record = gate.load_json(folder / 'current.json', None)
            if record:
                require(record['binding'] == binding(cfg, opts), 'driver_binding_mismatch')
                require(wake is None or record.get('wake_id') in (None, wake) or record['terminal'],
                        'driver_other_wake_running')
            if not record or record['terminal'] and (wake is None or wake != record.get('wake_id')):
                record = {'job_id': uuid.uuid4().hex, 'binding': binding(cfg, opts), 'mode': opts['mode'],
                          'wake_id': wake, 'invocation_id': invocation_id, 'started_ms': clock(),
                          'steps': 0, 'terminal': False, 'completed': False, 'pending': None}
            if record['terminal']:
                return view(record)
            with driver_ownership.owned(cfg):
                return DecisionDriver(cfg, record, clock=clock, sleep=sleep, session=session,
                                      exchange=exchange, transport=transport).drive()
    except ControlError as exc:
        if exc.reason != 'busy':
            raise
        result = status(cfg)
        return {**result, 'ok': True, 'reason': 'driver_busy', 'next': 'status',
                'terminal': False, 'completed': False}


def start(config, wake=None, invocation_id=None):
    """A detached code process owns all waits; callers can safely stop their tool session."""
    cfg = load_config(config)
    opts = settings(cfg)
    if not opts['enabled']:
        return status(cfg)
    folder = opts['path']
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    folder.chmod(0o700)
    with file_lock(folder / 'launch', 'decision-driver-launch', private=True):
        launch = gate.load_json(folder / 'launch.json', {})
        observed = status(cfg)
        if observed.get('terminal') and wake is not None and observed.get('wake_id') == wake:
            return observed
        if alive(launch.get('pid')) or (observed.get('worker_alive') and not observed.get('terminal')):
            require(wake is None or launch.get('wake_id') in (None, wake), 'driver_other_wake_running')
            return {**observed, 'next': 'status', 'reason': 'driver_running',
                    'terminal': False, 'completed': False}
        argv = [sys.executable, str(Path(__file__).resolve()), '--config', str(Path(config).resolve())]
        if wake:
            argv += ['--wake-id', wake]
        if invocation_id:
            argv += ['--invocation-id', invocation_id]
        argv += ['run']
        fd = os.open(folder / 'worker.log', os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, 'a') as log:
            os.fchmod(log.fileno(), 0o600)
            proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                    start_new_session=True, cwd=HERE)
        save(folder / 'launch.json', {'pid': proc.pid, 'wake_id': wake, 'started_ms': now_ms()})
        return {'ok': True, 'reason': 'driver_started', 'next': 'status', 'worker_pid': proc.pid,
                'completed': False, 'terminal': False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--wake-id')
    parser.add_argument('--invocation-id')
    parser.add_argument('action', choices=('start', 'run', 'status'))
    args = parser.parse_args(argv)
    try:
        cfg = load_config(args.config)
        result = (start(args.config, args.wake_id, args.invocation_id) if args.action == 'start' else
                  status(cfg) if args.action == 'status' else run(cfg, args.wake_id, args.invocation_id))
        print(encode(result))
        return 0 if result.get('ok') else 1
    except (ControlError, OSError, ValueError, KeyError, TypeError, sqlite3.Error) as exc:
        reason = exc.reason if isinstance(exc, ControlError) else 'driver_input_or_io_error'
        print(encode({'ok': False, 'reason': reason, 'next': 'inspect', 'completed': False}))
        return 1


if __name__ == '__main__':
    sys.exit(main())
