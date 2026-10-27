"""Short-task budgets reach life context without changing activity timestamps."""
import unittest

import test_activity_history as history_fixture
import activity_duration


class ActivityDurationTests(unittest.TestCase):
    setUpBase = history_fixture.ActivityHistoryTests.setUpBase
    setUp = history_fixture.ActivityHistoryTests.setUp
    tearDown = history_fixture.ActivityHistoryTests.tearDown
    add = history_fixture.ActivityHistoryTests.add
    history = history_fixture.ActivityHistoryTests.history

    def test_short_defaults_are_present_before_any_activity_is_chosen(self):
        h = self.history()
        self.assertEqual(h['today'],[])
        budgets = {b['kind']:b for b in h['duration_guidance']}
        self.assertEqual(budgets['laundry']['default_minutes'],10)
        self.assertEqual(budgets['laundry']['review_after_minutes'],20)
        self.assertEqual(budgets['dishes']['default_minutes'],10)

    def test_laundry_is_flagged_before_the_old_sixty_minute_threshold(self):
        self.add('laundry',self.day+8*3600000,35,category='laundry',label='빨래 준비')
        hint = self.history()['hints'][0]
        self.assertEqual(hint['code'],'long_housework_block')
        self.assertEqual(hint['kind'],'laundry')
        self.assertEqual(hint['minutes'],35)
        self.assertEqual(hint['review_after_minutes'],20)

    def test_dishes_tidy_and_shower_have_distinct_short_budgets(self):
        self.add('dishes',self.day+8*3600000,20,label='설거지')
        self.add('tidy',self.day+9*3600000,35,label='가벼운 방 정리')
        self.add('shower',self.day+10*3600000,25,category='self_care',label='샤워')
        self.add('prep',self.day+11*3600000,25,category='preparation',label='외출준비')
        hints = {h['activity_ids'][0]:h for h in self.history()['hints'] if 'activity_ids' in h}
        self.assertEqual(hints['dishes']['review_after_minutes'],15)
        self.assertEqual(hints['tidy']['review_after_minutes'],30)
        self.assertEqual(hints['shower']['review_after_minutes'],20)
        self.assertEqual(hints['shower']['code'],'long_short_activity')
        self.assertEqual(hints['prep']['review_after_minutes'],20)

    def test_machine_wait_is_visible_but_not_silently_rewritten(self):
        start = self.clock-600000
        self.add('wait',start,180,status='in_progress',phase='waiting',activity_key='housework:laundry')
        with self.store.connect() as db:
            before = dict(db.execute("SELECT * FROM activities WHERE id='wait'").fetchone())
        h = self.history()
        self.assertEqual(h['hints'][0]['phase'],'waiting')
        self.assertEqual(h['hints'][0]['default_minutes'],10)
        with self.store.connect() as db:
            after = dict(db.execute("SELECT * FROM activities WHERE id='wait'").fetchone())
        self.assertEqual(before,after)

    def test_extension_budget_stays_anchored_to_original_start(self):
        start = self.clock-600000
        self.add('wash',start,20,status='in_progress')
        self.assertEqual(self.history()['hints'],[])
        with self.store.connect(True) as db:
            db.execute("UPDATE activities SET planned_end_ms=? WHERE id='wash'",(self.clock+1200000,))
        hint = self.history()['hints'][0]
        self.assertEqual(hint['minutes'],30)
        self.assertEqual(hint['review_due_ms'],start+20*60000)
        self.assertNotEqual(hint['review_due_ms'],self.clock+20*60000)

    def test_normal_long_activities_do_not_get_short_chore_budgets(self):
        for i,category in enumerate(('work','sleep','movie','exercise','game')):
            self.add(category,self.day+i*3*3600000,180,category=category,label=category)
        self.assertEqual(self.history()['hints'],[])
        self.assertIsNone(activity_duration.profile('work','빨래 앱 수정',{'activity_key':'housework:laundry'}))

    def test_explicit_key_can_identify_task_without_guessing_free_text(self):
        self.add('keyed',self.day+8*3600000,20,category='misc',label='집안의 작은 일',
                 activity_key='housework:dishes')
        a = self.history()['today'][0]
        self.assertEqual(a['duration_guidance']['kind'],'dishes')
        self.assertEqual(a['duration_guidance']['default_minutes'],10)

    def test_finished_early_and_long_duration_reason_remain_distinct(self):
        self.add('early',self.day+8*3600000,180,actual_minutes=10)
        self.add('long',self.day+10*3600000,45,duration_reason='실제 많은 물량 처리 '*100)
        h = self.history()
        self.assertEqual(len(h['hints']),2)  # One long task, plus the distinct repeated activity hint.
        hint = next(h for h in h['hints'] if h['code']=='long_housework_block')
        self.assertEqual(hint['activity_ids'],['long'])
        self.assertEqual(len(hint['duration_reason']),240)


if __name__ == '__main__':
    unittest.main()
