"""Real SQLite/claim lifecycle, fake model/Dropbox only; no production services."""
import json
import multiprocessing
import os
from pathlib import Path
import time
import unittest
from unittest.mock import patch

from common import ControlError, file_lock
import decision_driver as driver
import gate
import model_adapter
import muse_executor
import role_executor
import test_muse_executor as fixtures
import test_role_executor as role_fixtures
import test_memory_exchange_bridge as exchange_fixtures


class DriverTests(unittest.TestCase):
    def setUp(self):
        self.c = fixtures.MuseExecutorTests('test_next_turn_uses_new_receipts_and_full_original_documents')
        self.c.setUp()
        self.addCleanup(self.c.tearDown)
        self.c.cfg['decision_driver'] = {'enabled': True, 'adapter': 'decision', 'poll_seconds': 1}
        self.c.cfg['model_adapters'] = {'decision': {'backend': 'file', 'model': 'one-model'}}
        self.requests = []

    def record(self):
        return gate.load_json(driver.settings(self.c.cfg)['path'] / 'current.json', {})

    def pending(self, worker):
        return worker.record['pending']['request']

    def response(self, worker, *, mutate=None):
        req = self.pending(worker)
        record = muse_executor.MuseExecutor(self.c.runner).load()
        output = {'request_id': req['request_id'], 'input_sha256': req['input_sha256'],
                  'input_read': True, 'action': 'commit', 'conversation_ended': True}
        if record['prepared']['snapshot']['work_manifest']['primary'] == 'dm':
            output['decision'] = self.c.compact_draft()
        else:
            output = {k: v for k, v in output.items() if k != 'conversation_ended'}
            output['action'] = 'close'
        if mutate:
            mutate(output)
        self.requests.append(req['request_id'])
        path = worker.folder / ('decision-model-response-' + req['request_id'] + '.json')
        driver.save(path, model_adapter.result(req, output))

    def run_driver(self, exchange=None, **kwargs):
        return driver.run(self.c.cfg, 'wake-1', clock=lambda: self.c.clock, sleep=self.c.sleep,
                          session=self.c.runner, exchange=exchange or self.response, **kwargs)

    def ready(self):
        self.c.ingest()
        self.c.recent()

    def test_off_does_not_claim_spawn_or_create_files(self):
        self.c.cfg['decision_driver'] = {'enabled': False}
        with patch.object(gate, 'claim_work', side_effect=AssertionError('claim')):
            result = driver.run(self.c.cfg)
        self.assertEqual(result['reason'], 'off')
        self.assertFalse(result['completed'])
        self.assertFalse((self.c.state_dir / 'decision-driver').exists())

    def test_status_is_read_only_and_does_not_renew_claim(self):
        before = (self.c.state_dir / 'wake_ack.json').read_bytes()
        with patch.object(gate, 'claim_work', side_effect=AssertionError('claim')):
            self.assertEqual(driver.status(self.c.cfg)['phase'], 'idle')
        self.assertEqual((self.c.state_dir / 'wake_ack.json').read_bytes(), before)

    def test_single_model_commits_sends_and_finishes_without_new_session(self):
        self.ready()
        result = self.run_driver()
        self.assertTrue(result['completed'], result)
        self.assertEqual(result['wake_id'], 'wake-1')
        self.assertTrue(result['terminal'])
        with self.c.store.connect() as db:
            self.assertEqual(db.execute('SELECT status FROM dm_jobs').fetchone()[0], 'confirmed')
        self.assertEqual(gate.load_json(self.c.state_dir / 'wake_ack.json', {})['phase'], 'finished')
        self.assertEqual(len(set(self.requests)), len(self.requests))
        self.assertFalse(list(driver.settings(self.c.cfg)['path'].glob('decision-model-input-*.json')))

    def test_busy_retries_same_paid_response_without_second_model(self):
        self.ready()
        original = muse_executor.MuseExecutor.respond
        calls = []
        def busy(executor, response):
            calls.append(response['request_id'])
            if len(calls) < 3:
                raise ControlError('busy')
            return original(executor, response)
        with patch.object(muse_executor.MuseExecutor, 'respond', busy):
            result = self.run_driver()
        self.assertTrue(result['completed'], result)
        self.assertEqual(len(set(calls[:3])), 1)
        self.assertEqual(self.requests.count(calls[0]), 1)
        with self.c.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM dm_jobs').fetchone()[0], 1)

    def test_commit_is_not_completion_and_finish_is_recovered(self):
        self.ready()
        original = self.c.runner.finish
        calls = []
        def blocked(turn):
            calls.append(turn)
            if len(calls) == 1:
                raise ControlError('busy')
            return original(turn)
        with patch.object(self.c.runner, 'finish', blocked):
            result = self.run_driver()
        self.assertTrue(result['completed'], result)
        self.assertGreaterEqual(len(calls), 2)
        self.assertEqual(len(self.requests), len(set(self.requests)))
        with self.c.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM dm_jobs').fetchone()[0], 1)

    def test_pending_file_polls_do_not_consume_step_budget(self):
        self.ready()
        self.c.cfg['decision_driver']['max_steps'] = 12
        count = []
        def delayed(worker):
            count.append(1)
            self.assertFalse(driver.status(self.c.cfg)['completed'])
            if len(count) >= 20:
                self.response(worker)
        result = self.run_driver(delayed)
        self.assertTrue(result['completed'], result)
        self.assertGreaterEqual(len(count), 20)
        self.assertLess(result['steps'], 12)

    def test_deadline_preserves_unanswered_dm_and_records_incomplete(self):
        self.ready()
        gate.write_json(self.c.state_dir / 'wake.json', {'wake_id': 'wake-1',
                        'claim_deadline_ms': self.c.clock + 3500})
        result = self.run_driver(lambda worker: None)
        self.assertFalse(result['completed'])
        self.assertTrue(result['terminal'])
        self.assertEqual(result['reason'], 'driver_deadline')
        self.assertEqual(self.c.snapshot()['messages'][0]['status'], 'received')
        self.assertEqual(self.c.snapshot()['jobs'], [])

    def test_restart_reuses_input_and_paid_response(self):
        self.ready()
        class Crash(BaseException):
            pass
        def paid_then_crash(worker):
            self.response(worker)
            raise Crash()
        with self.assertRaises(Crash):
            self.run_driver(paid_then_crash)
        old = self.record()
        self.assertFalse(old['terminal'])
        paid_id = old['pending']['request']['request_id']
        def followup(worker):
            self.assertNotEqual(self.pending(worker)['request_id'], paid_id)
            self.response(worker)
        result = self.run_driver(followup)
        self.assertTrue(result['completed'], result)
        self.assertEqual(result['job_id'], old['job_id'])

    def test_lock_contention_is_pending_and_never_claims(self):
        folder = driver.settings(self.c.cfg)['path']
        folder.mkdir(parents=True)
        with file_lock(folder / 'driver', 'decision-driver', private=True):
            with patch.object(gate, 'claim_work', side_effect=AssertionError('claim')):
                result = self.run_driver()
        self.assertEqual(result['reason'], 'driver_busy')
        self.assertEqual(result['next'], 'status')
        self.assertFalse(result['completed'])

    def test_replaced_wake_is_not_reclaimed_or_acked(self):
        self.ready()
        def replace(worker):
            for name in ('wake', 'gate_state'):
                gate.write_json(self.c.state_dir / (name + '.json'), {'wake_id': 'other'})
            gate.write_json(self.c.state_dir / 'wake_ack.json', {'wake_id': 'other', 'phase': 'started'})
        result = self.run_driver(replace)
        self.assertFalse(result['completed'])
        self.assertEqual(result['reason'], 'stale_claim')
        self.assertEqual(gate.load_json(self.c.state_dir / 'wake_ack.json', {})['wake_id'], 'other')

    def test_wrong_response_binding_does_not_apply_or_auto_rewrite(self):
        self.ready()
        result = self.run_driver(lambda worker: self.response(worker,
                                    mutate=lambda out: out.update(request_id='wrong')))
        self.assertFalse(result['completed'])
        self.assertEqual(result['reason'], 'executor_input_mismatch')
        self.assertEqual(self.c.snapshot()['jobs'], [])

    def test_completed_job_replay_does_not_claim_or_send_again(self):
        self.ready()
        first = self.run_driver()
        with patch.object(gate, 'claim_work', side_effect=AssertionError('claim')):
            second = self.run_driver(lambda worker: self.fail('model'))
        self.assertEqual(first['job_id'], second['job_id'])
        with self.c.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM dm_jobs').fetchone()[0], 1)

    def test_no_claim_is_skipped_not_completed_work(self):
        with patch.object(gate, 'claim_work', return_value=(10, None)):
            result = driver.run(self.c.cfg, clock=lambda: self.c.clock, sleep=self.c.sleep)
        self.assertEqual(result['phase'], 'skipped')
        self.assertFalse(result['completed'])

    def test_unknown_next_is_not_silently_completed(self):
        self.ready()
        with patch.object(muse_executor.MuseExecutor, 'prepare', return_value={'ok': True, 'next': 'surprise'}):
            result = self.run_driver()
        self.assertFalse(result['completed'])
        self.assertEqual(result['reason'], 'driver_unknown_next')

    def test_warning_approval_preserves_delivery_group_tracking(self):
        self.ready()
        self.c.store.apply('main', self.c.draft(), self.c.clock)
        folder = driver.settings(self.c.cfg)['path']; folder.mkdir(parents=True)
        state = {'job_id': 'test', 'mode': 'muse', 'wake_id': 'wake-1', 'steps': 0, 'pending': None,
                 'started_ms': self.c.clock, 'terminal': False}
        worker = driver.DecisionDriver(self.c.cfg, state, clock=lambda: self.c.clock, session=self.c.runner)
        saved = {'response': {'action': 'approve_warning'},
                 'warning': {'decision': {'dm_decisions': [{'group_id': 'g1'}]}}}
        gate.write_json(self.c.state_dir / 'wake_ack.json', {'wake_id': 'wake-1', 'phase': 'finished'})
        with patch.object(muse_executor.MuseExecutor, 'load', return_value=saved):
            result = worker.accept_result({'ok': True, 'committed': True, 'after': {'next': 'done'}})
        self.assertFalse(result['completed'])
        self.assertEqual(result['delivery']['pending'], 1)

    def test_crash_after_final_ack_replays_without_new_model_even_after_deadline(self):
        self.ready()
        class Crash(BaseException):
            pass
        original = driver.DecisionDriver.finish
        def crash(worker, phase, reason, completed=False):
            if completed:
                raise Crash()
            return original(worker, phase, reason, completed)
        with patch.object(driver.DecisionDriver, 'finish', crash):
            with self.assertRaises(Crash):
                self.run_driver()
        self.c.sleep(2000)
        with patch.object(gate, 'claim_work', side_effect=AssertionError('claim')):
            result = self.run_driver(lambda worker: self.fail('already finished'))
        self.assertTrue(result['completed'], result)

    def test_model_timeout_retries_same_request_with_bounded_attempts(self):
        self.ready()
        self.c.cfg['model_adapters']['decision'] = {'backend': 'command', 'argv': ['fake-model']}
        ids = []
        def timeout(opts, request, seconds, transport):
            ids.append(request['request_id'])
            raise ControlError('model_timeout')
        with patch.object(model_adapter, 'invoke', timeout):
            result = self.run_driver()
        self.assertFalse(result['completed'])
        self.assertEqual(result['reason'], 'driver_transport_exhausted')
        self.assertEqual(len(ids), 2)
        self.assertEqual(len(set(ids)), 1)
        self.assertEqual(self.c.snapshot()['messages'][0]['status'], 'received')

    def test_start_is_detached_and_reports_pending_not_done(self):
        config = Path(self.c.tmp.name) / 'automation.json'
        config.write_text(json.dumps(self.c.cfg))
        fake = type('Process', (), {'pid': 12345})()
        with patch.object(driver.subprocess, 'Popen', return_value=fake) as spawn:
            result = driver.start(config, 'wake-1')
        self.assertEqual(result['next'], 'status')
        self.assertFalse(result['completed'])
        self.assertTrue(spawn.call_args.kwargs['start_new_session'])
        self.assertEqual(spawn.call_args.kwargs['stdin'], driver.subprocess.DEVNULL)

    def test_duplicate_start_attaches_without_spawning_another_worker(self):
        config = Path(self.c.tmp.name) / 'automation.json'
        config.write_text(json.dumps(self.c.cfg))
        folder = driver.settings(self.c.cfg)['path']
        folder.mkdir(parents=True)
        driver.save(folder / 'launch.json', {'pid': os.getpid(), 'wake_id': 'wake-1'})
        with patch.object(driver.subprocess, 'Popen', side_effect=AssertionError('spawn')):
            result = driver.start(config, 'wake-1')
        self.assertEqual(result['reason'], 'driver_running')
        self.assertFalse(result['completed'])

    def test_command_model_result_is_journaled_before_apply(self):
        self.ready()
        self.c.cfg['model_adapters']['decision'] = {'backend': 'command', 'argv': ['fake-model']}
        def invoke(opts, req, timeout, transport):
            out = {'request_id': req['request_id'], 'input_sha256': req['input_sha256'],
                   'input_read': True, 'action': 'commit', 'decision': self.c.compact_draft(),
                   'conversation_ended': True}
            record = muse_executor.MuseExecutor(self.c.runner).load()
            if record['prepared']['snapshot']['work_manifest']['primary'] != 'dm':
                out.pop('decision'); out.pop('conversation_ended'); out['action'] = 'close'
            return model_adapter.result(req, out)
        original = muse_executor.MuseExecutor.respond
        def applied(executor, response):
            p = driver.settings(self.c.cfg)['path'] / ('decision-model-response-' + response['request_id'] + '.json')
            self.assertTrue(p.is_file())
            return original(executor, response)
        with patch.object(model_adapter, 'invoke', invoke), patch.object(muse_executor.MuseExecutor, 'respond', applied):
            self.assertTrue(self.run_driver()['completed'])

    def test_unconfirmed_delivery_is_explicit_handoff_not_false_completion(self):
        self.ready()
        with patch.object(self.c.runner.dispatch, 'send_due', return_value={'ok': True, 'reason': 'media_not_ready'}):
            with patch.object(self.c.runner, '_end', wraps=self.c.runner._end):
                # Existing close can hand off repair work; no new model should be created merely to send.
                result = self.run_driver()
        self.assertFalse(result['completed'])
        self.assertEqual(result['phase'], 'handed_off')
        self.assertEqual(result['delivery'].get('pending'), 1)

    def test_dropbox_pass_only_moves_files_driver_consumes_result(self):
        self.ready()
        self.c.cfg['decision_driver']['dropbox'] = True
        fake = exchange_fixtures.FakeDropbox()
        download = fake.list_folder
        def files(path):
            if path.endswith('/out'):
                for remote, (raw, _) in list(fake.files.items()):
                    if '/in/decision-model-input-' not in remote:
                        continue
                    req = json.loads(raw)
                    record = muse_executor.MuseExecutor(self.c.runner).load()
                    out = {'request_id': req['request_id'], 'input_sha256': req['input_sha256'],
                           'input_read': True, 'action': 'commit', 'conversation_ended': True,
                           'decision': self.c.compact_draft()}
                    if record['prepared']['snapshot']['work_manifest']['primary'] != 'dm':
                        out.pop('decision'); out.pop('conversation_ended'); out['action'] = 'close'
                    response = remote.replace('/in/', '/out/').replace('-input-', '-response-')
                    fake.put(response, json.dumps(model_adapter.result(req, out)).encode())
            return download(path)
        fake.list_folder = files
        import memory_exchange_bridge
        with patch.object(memory_exchange_bridge, 'RealDropbox', return_value=fake):
            result = driver.run(self.c.cfg, 'wake-1', clock=lambda: self.c.clock, sleep=self.c.sleep,
                                session=self.c.runner)
        self.assertTrue(result['completed'], result)
        self.assertTrue(any(call[0] == 'download' for call in fake.calls))

    @unittest.skipUnless('fork' in multiprocessing.get_all_start_methods(), 'requires POSIX process isolation')
    def test_code_process_finishes_after_starting_caller_returns(self):
        self.ready()
        # Real child process, fake file-model response; the caller does not run the decision loop.
        ctx = multiprocessing.get_context('fork')
        def work():
            driver.run(self.c.cfg, 'wake-1', clock=lambda: self.c.clock, sleep=time.sleep,
                       session=self.c.runner)
        process = ctx.Process(target=work)
        process.start()
        def stop():
            if process.is_alive():
                process.terminate()
            process.join(5)
        self.addCleanup(stop)
        folder = driver.settings(self.c.cfg)['path']
        seen = set()
        end = time.monotonic() + 8
        while time.monotonic() < end and process.is_alive():
            for path in folder.glob('decision-model-input-*.json'):
                req = json.loads(path.read_text())
                if req['request_id'] in seen:
                    continue
                seen.add(req['request_id'])
                output = {'request_id': req['request_id'], 'input_sha256': req['input_sha256'],
                          'input_read': True, 'action': 'close'}
                # Resolve the incoming without a send; all actual model work is mocked here.
                record = muse_executor.MuseExecutor(self.c.runner).load()
                if record['prepared']['snapshot']['work_manifest']['primary'] == 'dm':
                    output.update(action='commit', conversation_ended=True,
                        decision={'advance_to_ms': self.c.clock, 'next_decision_ms': self.c.clock + 3600000,
                                  'dm_decisions': [{'action': 'no_reply', 'message_ids': ['m1'],
                                                    'reason': 'synthetic test only'}],
                                  'processed_event_ids': ['dm:m1']})
                driver.save(folder / ('decision-model-response-' + req['request_id'] + '.json'),
                            model_adapter.result(req, output))
            time.sleep(0.02)
        process.join(1)
        self.assertFalse(process.is_alive(), driver.status(self.c.cfg))
        self.assertEqual(process.exitcode, 0)
        self.assertTrue(driver.status(self.c.cfg)['completed'])


class RoleDriverTests(unittest.TestCase):
    def setUp(self):
        self.r = role_fixtures.RoleExecutorTests('test_dm_routes_context_writer_review_and_commits_exact_text')
        self.r.setUp(); self.addCleanup(self.r.doCleanups)
        self.c = self.r.c
        self.c.cfg['decision_driver'] = {'enabled': True, 'mode': 'roles', 'poll_seconds': 1, 'max_steps': 12}
        self.c.ingest(); self.c.recent()

    def exchange(self, worker):
        state = self.r.worker.load()
        p = state['pending']
        role = p['role']
        record = self.r.worker.executor.load()
        if record['prepared']['snapshot']['work_manifest']['primary'] != 'dm':
            output = ({'decision': {'advance_to_ms': self.c.clock, 'next_decision_ms': self.c.clock + 3600000},
                       'reply_briefs': []} if role == 'context' else
                      {'verdict': 'approve', 'reviews': [], 'post_write': {}, 'conversation_ended': True})
        else:
            output = (self.r.context() if role == 'context' else
                      {'parts': ['안녕']} if role == 'writer' else self.r.review())
        value = model_adapter.result(p['request'], {'input_read': True, **output})
        role_executor.save(self.r.worker.folder / ('role-model-response-' + p['id'] + '.json'), value)

    def test_role_file_chain_and_followup_turn_are_driven_by_code(self):
        with patch.object(role_executor.muse_executor, 'DecisionSession', return_value=self.c.runner):
            result = driver.run(self.c.cfg, 'wake-1', clock=lambda: self.c.clock, sleep=self.c.sleep,
                                session=self.c.runner, exchange=self.exchange)
        self.assertTrue(result['completed'], result)
        self.assertEqual(result['wake_id'], 'wake-1')
        with self.c.store.connect() as db:
            self.assertEqual(db.execute('SELECT status FROM dm_jobs').fetchone()[0], 'confirmed')

    def test_roles_off_fails_before_claim(self):
        self.c.cfg['role_execution']['enabled'] = False
        with patch.object(gate, 'claim_work', side_effect=AssertionError('claim')):
            with self.assertRaisesRegex(ControlError, 'role_execution_off'):
                driver.run(self.c.cfg)


if __name__ == '__main__':
    unittest.main()
