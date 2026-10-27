"""This repository's persona switches: no clock-only quiet, no morning greeting, Slack default."""
from datetime import datetime, timedelta
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch

import persona
import test_controller as baseline
from common import ControlError
from store import KST, conversation_mode

OWN = Path(__file__).resolve().parents[1] / 'persona.py'


def own_values():
    spec = importlib.util.spec_from_file_location('persona_own', OWN)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return {name: getattr(module, name) for name in baseline.REFERENCE_PERSONA}


class PersonaTests(unittest.TestCase):
    setUp = baseline.ControllerTests.setUp
    tearDown = baseline.ControllerTests.tearDown
    snapshot = baseline.ControllerTests.snapshot
    decision = baseline.ControllerTests.decision
    review = baseline.ControllerTests.review
    activity = baseline.ControllerTests.activity
    ingest = baseline.ControllerTests.ingest

    def own(self):
        values = own_values()
        for name, value in values.items():
            self.enterContext(patch.object(persona, name, value))
        return values

    def test_repository_values(self):
        self.assertEqual(own_values(), {'NIGHT_QUIET_UNTIL_HOUR': None, 'MORNING_CONTACT': False,
                                        'DEFAULT_CHANNEL': 'slack'})

    def night(self, hour):
        day = datetime.fromtimestamp(baseline.T / 1000, KST) + timedelta(days=1)
        return int(day.replace(hour=hour, minute=30).timestamp() * 1000)

    def test_awake_free_conversation_stays_live_after_midnight(self):
        self.own()
        self.clock = self.night(2)
        self.store.apply('main', self.decision(activities=[self.activity('late', attention='free')]), self.clock)
        self.ingest('m1')
        with self.store.connect() as db:
            self.assertEqual(conversation_mode(db, 'main', self.clock), 'live')
        with patch.object(persona, 'NIGHT_QUIET_UNTIL_HOUR', 7), self.store.connect() as db:
            self.assertEqual(conversation_mode(db, 'main', self.clock), 'quiet')

    def test_sleep_is_still_quiet(self):
        self.own()
        self.clock = self.night(4)
        self.store.apply('main', self.decision(activities=[self.activity(
            'sleep', category='sleep', label='수면', attention='unavailable')]), self.clock)
        with self.store.connect() as db:
            self.assertEqual(conversation_mode(db, 'main', self.clock), 'quiet')

    def test_morning_greeting_is_disabled(self):
        self.own()
        self.assertEqual(self.snapshot()['morning_contact']['status'], 'disabled')
        dm = {'action': 'initiate', 'routine': 'morning_contact', 'group_id': 'm', 'due_ms': self.clock,
              'parts': ['굿모닝'], 'reason': 'test', 'channel': 'slack'}
        with self.assertRaisesRegex(ControlError, 'morning_contact_disabled'):
            self.store.apply('main', self.decision(dm_decisions=[dm]), self.clock)

    def test_initiate_without_channel_goes_to_slack(self):
        self.own()
        self.ingest('history', direction='outgoing', observation={'identity': 'platform', 'channel': 'slack'})
        dm = {'action': 'initiate', 'group_id': 'p1', 'due_ms': self.clock, 'parts': ['겜 ㄱ?'], 'reason': 'test'}
        dm['conversation_review'] = self.review({**dm, 'channel': 'slack'})
        self.store.apply('main', self.decision(dm_decisions=[dm]), self.clock)
        self.assertEqual([j['channel'] for j in self.snapshot()['jobs']], ['slack'])


if __name__ == '__main__':
    unittest.main()
