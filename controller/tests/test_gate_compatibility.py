"""Optional contract test: set INTENT_TEST_GATE_PATH to the actual deployment gate.py."""
import importlib.util
import os
from pathlib import Path
import unittest

import test_action_intents as intents

GATE_PATH = Path(os.environ.get('INTENT_TEST_GATE_PATH',
    str(Path(__file__).resolve().parents[4]/'2026-10-16/gate.py')))
gate = None
if GATE_PATH.is_file():
    spec = importlib.util.spec_from_file_location('intent_test_gate',GATE_PATH)
    gate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gate)


@unittest.skipUnless(gate is not None,'supply INTENT_TEST_GATE_PATH to test the deployment gate')
class GateCompatibilityTests(unittest.TestCase):
    setUp = intents.ActionIntentTests.setUp
    tearDown = intents.ActionIntentTests.tearDown
    snapshot = intents.ActionIntentTests.snapshot
    decision = intents.ActionIntentTests.decision
    review = intents.ActionIntentTests.review
    activity = intents.ActionIntentTests.activity
    create = intents.ActionIntentTests.create
    apply = intents.ActionIntentTests.apply

    def test_actual_gate_wakes_for_intent_using_existing_decision_due(self):
        self.apply([self.create()],activities=[self.activity()],weekly_plans=[{
            'week_start':'2026-10-12','remote_date':None,'seed':'s','reason':'test'}])
        self.clock += 10000
        reasons, detail = gate.evaluate(self.snapshot(),{'last_run_ms':self.clock},self.clock)
        self.assertEqual(reasons,['decision_due'])
        self.assertEqual(gate.decide(reasons,detail,{},self.clock),(True,1))

    def test_actual_gate_does_not_wake_for_intent_during_unavailable_activity(self):
        self.apply([self.create()],activities=[self.activity(attention='unavailable')],weekly_plans=[{
            'week_start':'2026-10-12','remote_date':None,'seed':'s','reason':'test'}])
        self.clock += 10000
        reasons, _ = gate.evaluate(self.snapshot(),{'last_run_ms':self.clock},self.clock)
        self.assertEqual(reasons,[])
