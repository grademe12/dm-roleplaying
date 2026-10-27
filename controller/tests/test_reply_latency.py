"""Real SQLite tests for compact reads, mandatory boundaries and deferred work."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

import test_controller as baseline
from common import ControlError
import decision_routing
import slack_delivery

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('polling_configuration', ROOT.parent/'tools/configure_slack_polling.py')
polling = importlib.util.module_from_spec(spec)
spec.loader.exec_module(polling)


class ReplyLatencyTests(unittest.TestCase):
    setUp = baseline.ControllerTests.setUp
    tearDown = baseline.ControllerTests.tearDown
    snapshot = baseline.ControllerTests.snapshot
    decision = baseline.ControllerTests.decision
    review = baseline.ControllerTests.review
    ingest = baseline.ControllerTests.ingest
    activity = baseline.ControllerTests.activity
    reply = baseline.ControllerTests.reply

    def free(self, end=None):
        plan={'week_start':'2026-10-12','seed':'persisted','reason':'ordinary existing plan'}
        self.store.apply('main', self.decision(activities=[self.activity(attention='free',end=end)],
                                             weekly_plans=[plan]),self.clock)

    def sleep(self, seconds):
        self.clock += int(seconds*1000)

    def test_compact_tick_omits_bodies_but_full_compatibility_remains(self):
        self.free();self.ingest(text='a real current question')
        full=self.snapshot();dm=self.store.tick('main',self.clock,view='dm')
        self.assertTrue(full['core_rules'])
        for field in ('core_rules','weekly_plan'):
            self.assertNotIn(field,dm)
            self.assertIn(field,dm['projection']['omitted_fields'])
        self.assertEqual(dm['messages'],full['messages'])
        self.assertEqual(dm['core_rules_hash'],full['core_rules_hash'])
        self.assertEqual(dm['work_manifest']['primary'],'dm')
        self.assertLess(len(json.dumps(dm)),len(json.dumps(full)))

    def test_core_hash_requires_initial_and_changed_document_read(self):
        first=self.store.tick('main',self.clock,view='dm')
        self.assertTrue(first['core_rules_changed'])
        same=self.store.tick('main',self.clock,view='dm',known_core_rules_hash=first['core_rules_hash'])
        self.assertFalse(same['core_rules_changed'])
        changed=self.store.tick('main',self.clock,view='dm',known_core_rules_hash='0'*64)
        self.assertTrue(changed['core_rules_changed'])
        self.assertTrue(Path(first['core_rules_path']).is_file())
        with self.assertRaises(ControlError):self.store.tick('main',self.clock,view='dm',known_core_rules_hash='wrong')

    def test_gate_projection_keeps_routing_metadata_without_message_body(self):
        self.free();self.ingest(text='private marker')
        snap=self.store.tick('main',self.clock,view='gate')
        self.assertNotIn('private marker',json.dumps(snap))
        self.assertEqual(snap['messages'][0]['status'],'received')
        self.assertEqual(snap['work_manifest']['primary'],'dm')

    def test_due_intent_and_activity_boundary_survive_compact_tick(self):
        self.free(end=self.clock+5000)
        intent={'action':'create','id':'exercise','target_key':'exercise:walk',
                'summary':'산책','self_chosen':True,'review_at_ms':self.clock+4000,
                'expires_ms':self.clock+3600000,'reason':'selected next action'}
        self.store.apply('main',self.decision(action_intents=[intent]),self.clock)
        self.clock+=6000;self.ingest()
        snap=self.store.tick('main',self.clock,view='dm')
        self.assertEqual(snap['work_manifest']['primary'],'dm')
        self.assertIn('activity_due',snap['work_manifest']['life_required'])
        self.assertEqual(snap['action_intent_due_ids'],['exercise'])
        self.assertIn('action_intent_due',snap['work_manifest']['life_required'])
        self.assertEqual(snap['action_intents'][0]['id'],'exercise')
        with self.assertRaisesRegex(ControlError,'active_activity_not_current'):
            self.store.apply('main',self.decision(),self.clock)

    def test_due_intent_guard_is_not_bypassed_by_projected_read(self):
        self.free()
        self.store.apply('main',self.decision(action_intents=[{'action':'create','id':'walk',
            'target_key':'exercise:walk','summary':'산책','self_chosen':True,
            'review_at_ms':self.clock+1000,'expires_ms':self.clock+3600000,'reason':'selected'}]),self.clock)
        self.clock+=2000
        self.store.tick('main',self.clock,view='dm')
        with self.assertRaisesRegex(ControlError,'intent_review_required'):
            self.store.apply('main',self.decision(),self.clock)

    def test_unknown_and_stale_groups_remain_visible(self):
        self.free();self.ingest();self.reply()
        with self.store.connect(True) as db:
            db.execute("UPDATE dm_jobs SET status='unknown' WHERE session_id='main'")
        dm=self.store.tick('main',self.clock,view='dm')
        self.assertFalse(dm['dispatch_hold'])
        self.assertEqual(dm['delivery_uncertain_job_ids'],['g1:0'])
        self.assertEqual(dm['work_manifest']['unresolved_job_ids'],['g1:0'])
        self.assertEqual(dm['jobs'][0]['status'],'unknown')

    def test_stale_draft_is_still_a_dm_route(self):
        self.free();self.ingest();self.reply();self.ingest('m2')
        snap=self.store.tick('main',self.clock,view='dm')
        self.assertIn('g1',snap['draft_stale_group_ids'])
        self.assertEqual(snap['work_manifest']['primary'],'dm')

    def test_due_intent_still_interrupts_wait(self):
        self.free()
        self.store.apply('main',self.decision(action_intents=[{'action':'create','id':'walk',
            'target_key':'exercise:walk','summary':'산책','self_chosen':True,
            'review_at_ms':self.clock+2000,'expires_ms':self.clock+3600000,'reason':'selected'}]),self.clock)
        result=self.store.wait_dm('main',30,lambda:self.clock,self.sleep)
        self.assertEqual(result['reason'],'decision_needed')
        self.assertIn('decision_due',result['needs_decision'])

    def test_gate_avoids_detail_queries_without_losing_due_intents(self):
        self.free()
        self.store.apply('main',self.decision(action_intents=[{'action':'create','id':'walk',
            'target_key':'exercise:walk','summary':'산책','self_chosen':True,
            'review_at_ms':self.clock+1000,'expires_ms':self.clock+3600000,'reason':'selected'}]),self.clock)
        self.clock+=2000
        expected=self.snapshot()['action_intent_due_ids']
        with patch('store.action_intents.snapshot',side_effect=AssertionError('full intent read')), \
             patch('store.action_intents.followups',side_effect=AssertionError('followup read')), \
             patch('store.conversation_flow.policy',side_effect=AssertionError('policy read')):
            snap=self.store.tick('main',self.clock,view='gate')
        self.assertEqual(snap['action_intent_due_ids'],expected)
        self.assertIn('decision_due',snap['needs_decision'])

    def test_trace_correlates_compact_tick_without_logging_content(self):
        self.ingest(text='private marker')
        output=subprocess.check_output([sys.executable,str(ROOT/'ctl.py'),'--db',str(self.path),
            'tick','--view','dm','--wake-id','wake-test'],text=True)
        result=json.loads(output);self.assertTrue(result['turn_id'])
        records=[json.loads(line) for f in (self.path.parent/'logs').glob('*.log') for line in f.read_text().splitlines()]
        record=records[-1]
        self.assertEqual(record['turn_id'],result['turn_id'])
        self.assertEqual(record['wake_id'],'wake-test')
        self.assertEqual(record['work_route'],'dm')
        self.assertNotIn('private marker',json.dumps(records))
        self.assertEqual(record['output_bytes'],len(output.strip().encode('utf-8')))

    def runtime_config(self):
        cfg={'python':sys.executable,'ctl':str(ROOT/'ctl.py'),'db':str(self.path),
             'session':'main','slack':{'backfill_seconds':60,'token_path':'DO_NOT_CHANGE'},'send_enabled':True}
        path=self.path.parent/'automation.json';path.write_text(json.dumps(cfg))
        settings={**slack_delivery.DEFAULTS,'workspace_id':'W','channel_id':'C','self_user_id':'U',
                  'app_id':'A','event_type':'persona_delivery','correlation_enabled':True,
                  'history_min_interval_ms':60000}
        slack_delivery.configure(self.store,'main',settings,self.clock)
        return path,settings

    def test_invalid_trace_label_is_not_written_to_log(self):
        result=subprocess.run([sys.executable,str(ROOT/'ctl.py'),'--db',str(self.path),
            'tick','--view','dm','--turn-id','private marker'],capture_output=True,text=True)
        self.assertEqual(result.returncode,1)
        logs=''.join(f.read_text() for f in (self.path.parent/'logs').glob('*.log'))
        self.assertNotIn('private marker',logs)

    def test_polling_controller_failure_leaves_configuration_unchanged(self):
        path,_=self.runtime_config();before=path.read_bytes()
        actual=polling.controller
        def fail(cfg,command,document=None):
            if command=='slack-delivery-configure':raise RuntimeError('configure failed')
            return actual(cfg,command,document)
        with patch.object(polling,'controller',side_effect=fail):
            with self.assertRaises(RuntimeError):polling.configure(path,apply=True,internal_app_confirmed=True)
        self.assertEqual(path.read_bytes(),before)

    def test_polling_preview_does_not_write_and_requires_internal_confirmation(self):
        path,settings=self.runtime_config();before=path.read_bytes()
        with self.assertRaises(ValueError):polling.configure(path)
        out=polling.configure(path,internal_app_confirmed=True)
        self.assertFalse(out['applied']);self.assertEqual(path.read_bytes(),before)
        with self.store.connect() as db:self.assertEqual(slack_delivery.settings(db,'main'),settings)

    def test_polling_apply_preserves_identity_flags_and_existing_budget(self):
        path,settings=self.runtime_config()
        slack_delivery.history_budget(self.store,'main',self.clock)
        out=polling.configure(path,apply=True,internal_app_confirmed=True)
        self.assertTrue(out['applied']);self.assertEqual(path.stat().st_mode&0o777,0o600)
        cfg=json.loads(path.read_text());self.assertEqual(cfg['slack']['backfill_seconds'],15)
        self.assertEqual(cfg['slack']['token_path'],'DO_NOT_CHANGE');self.assertTrue(cfg['send_enabled'])
        with self.store.connect() as db:
            current=slack_delivery.settings(db,'main')
            due=db.execute('SELECT next_allowed_ms FROM slack_history_budget').fetchone()[0]
        self.assertEqual(current,{**settings,'history_min_interval_ms':15000})
        self.assertEqual(due,self.clock+60000)
        backup=Path(out['backup_dir']);self.assertEqual((backup/'automation.json').stat().st_mode&0o777,0o600)
        self.assertEqual(json.loads((backup/'delivery-settings.json').read_text()),settings)

    def test_polling_file_commit_failure_restores_old_db_settings(self):
        path,settings=self.runtime_config();before=path.read_bytes()
        with patch.object(polling.os,'replace',side_effect=OSError('failed')):
            with self.assertRaises(OSError):polling.configure(path,apply=True,internal_app_confirmed=True)
        self.assertEqual(path.read_bytes(),before)
        with self.store.connect() as db:self.assertEqual(slack_delivery.settings(db,'main'),settings)

    def test_new_fifteen_second_budget_still_respects_retry_after(self):
        _,settings=self.runtime_config()
        slack_delivery.configure(self.store,'main',{**settings,'history_min_interval_ms':15000},self.clock)
        first=slack_delivery.history_budget(self.store,'main',self.clock)
        self.assertEqual(first['retry_after_ms'],15000)
        slack_delivery.history_budget(self.store,'main',self.clock,90000)
        blocked=slack_delivery.history_budget(self.store,'main',self.clock+15000)
        self.assertFalse(blocked['allowed']);self.assertEqual(blocked['retry_after_ms'],75000)
