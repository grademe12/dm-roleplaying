"""Real SQLite coverage for bounded life context, not an activity scheduler."""
import json
from datetime import datetime
import unittest
from unittest.mock import patch

import test_controller as baseline
from activity_history import KST, DAY_MS, TODAY_LIMIT, PREVIOUS_LIMIT


class ActivityHistoryTests(unittest.TestCase):
    setUpBase = baseline.ControllerTests.setUp
    tearDown = baseline.ControllerTests.tearDown
    snapshot = baseline.ControllerTests.snapshot
    decision = baseline.ControllerTests.decision
    review = baseline.ControllerTests.review
    ingest = baseline.ControllerTests.ingest

    def setUp(self):
        self.setUpBase()
        self.day = int(datetime(2026,10,18,tzinfo=KST).timestamp()*1000)
        self.clock = self.day+15*3600000

    def add(self, aid, start, minutes=20, category='housework', label='빨래',
            status='completed', actual_minutes=None, **details):
        end = start+minutes*60000
        actual = (start+(minutes if actual_minutes is None else actual_minutes)*60000
                  if status in ('completed','interrupted') else None)
        with self.store.connect(True) as db:
            db.execute('''INSERT INTO activities(session_id,id,category,label,status,start_ms,
                planned_end_ms,actual_end_ms,location,attention,interruptibility,visibility,details_json)
                VALUES ('main',?,?,?,?,?,?,?,'home','light','anytime','personal',?)''',
                (aid,category,label,status,start,end,actual,json.dumps({'reason':'fixture',**details})))

    def history(self):
        return self.store.tick('main',self.clock,view='life')['activity_history']

    def test_today_includes_finished_morning_and_current_afternoon(self):
        self.add('morning',self.day+8*3600000)
        self.add('afternoon',self.clock-600000,30,status='in_progress')
        h = self.history()
        self.assertEqual([a['id'] for a in h['today']],['morning','afternoon'])
        self.assertEqual(h['today'][0]['elapsed_minutes'],20)
        self.assertEqual(h['today'][1]['elapsed_minutes'],10)
        self.assertIn('repeated_activity_today',[x['code'] for x in h['hints']])

    def test_three_hour_chore_is_visible_and_advisory_not_a_write_guard(self):
        self.add('long',self.clock-3600000,180,status='in_progress',activity_key='housework:laundry')
        before = self.snapshot()['revision']
        h = self.history()
        self.assertEqual(h['hints'][0]['code'],'long_housework_block')
        self.assertEqual(h['hints'][0]['minutes'],180)
        self.assertTrue(h['advisory_only'])
        self.assertEqual(self.snapshot()['revision'],before)
        # Existing timeline semantics permit a harmless decision with this activity.
        self.store.apply('main',self.decision(),self.clock)
        self.assertEqual(self.snapshot()['activity']['id'],'long')

    def test_finished_early_uses_actual_time_and_overdue_does_not_invent_execution(self):
        self.add('early',self.day+7*3600000,180,actual_minutes=15)
        self.add('overdue',self.day+8*3600000,20,status='in_progress')
        h = self.history()
        items = {a['id']:a for a in h['today']}
        self.assertEqual(items['early']['elapsed_minutes'],15)
        self.assertEqual(items['overdue']['elapsed_minutes'],20)
        self.assertEqual(items['overdue']['overdue_minutes'],400)
        self.assertNotIn('long_housework_block',[x['code'] for x in h['hints']])
        self.assertIsNone(items['overdue']['actual_end_ms'])

    def test_one_laundry_process_can_have_separate_short_phases(self):
        for i, label in enumerate(('분류해서 세탁기 돌리기','빨래 널기','빨래 개기')):
            self.add('phase'+str(i),self.day+(8+i*2)*3600000,10,label=label,
                     activity_key='housework:laundry',process_id='laundry-1',phase='active')
        self.assertEqual(self.history()['hints'],[])

    def test_new_cycles_with_same_key_are_counted_as_repetition(self):
        for i in range(3):
            self.add('cycle'+str(i),self.day+(8+i*2)*3600000,10,
                     activity_key='housework:laundry',process_id='laundry-'+str(i))
        codes = [x['code'] for x in self.history()['hints']]
        self.assertIn('repeated_activity_today',codes)
        self.assertIn('recent_activity_concentration',codes)

    def test_midnight_crossing_is_split_at_kst_not_utc(self):
        self.add('sleep',self.day-3600000,120,category='sleep',label='수면')
        h = self.history()
        self.assertEqual(h['today_start_ms'],self.day)
        self.assertEqual(h['since_ms'],self.day-2*DAY_MS)
        self.assertEqual(h['categories'][0]['today_minutes'],60)
        self.assertEqual(h['categories'][0]['previous_minutes'],60)

    def test_planned_cancelled_future_and_old_records_are_not_experiences(self):
        self.add('planned',self.day+9*3600000,status='planned')
        self.add('cancelled',self.day+10*3600000,status='cancelled')
        self.add('future',self.clock+60000,status='planned')
        self.add('old',self.day-3*DAY_MS)
        self.add('previous',self.day-DAY_MS+8*3600000,category='hobby',label='읽기')
        self.assertEqual(self.history()['today'],[])
        self.assertEqual([a['id'] for a in self.history()['previous_days']],['previous'])

    def test_required_routines_do_not_create_diversity_quota(self):
        for i in range(3):
            self.add('meal'+str(i),self.day+(7+i*3)*3600000,30,category='meal',label='식사')
        self.assertEqual(self.history()['hints'],[])

    def test_detail_limits_do_not_hide_full_day_counts_and_totals(self):
        for i in range(TODAY_LIMIT+3):
            self.add('today'+str(i),self.day+(60+i)*60000,1)
        for i in range(PREVIOUS_LIMIT+2):
            self.add('prev'+str(i),self.day-DAY_MS+(60+i)*60000,1)
        h = self.history()
        self.assertEqual(len(h['today']),TODAY_LIMIT)
        self.assertEqual(len(h['previous_days']),PREVIOUS_LIMIT)
        self.assertEqual(h['today_count'],TODAY_LIMIT+3)
        self.assertTrue(h['today_has_more'])
        self.assertTrue(h['previous_has_more'])
        self.assertEqual(h['categories'][0]['today_minutes'],TODAY_LIMIT+3)
        self.assertEqual(h['hints'][0]['count'],TODAY_LIMIT+3)

    def test_gate_never_queries_history_and_pure_dm_omits_it(self):
        self.add('current',self.clock-60000,60,category='rest',label='휴식',status='in_progress')
        self.store.apply('main',self.decision(weekly_plans=[
            {'week_start':'2026-10-12','seed':'existing','reason':'existing plan'}]),self.clock)
        with patch('store.activity_history.snapshot',side_effect=AssertionError('unnecessary life read')):
            for view in ('gate','dm'):
                snap = self.store.tick('main',self.clock,view=view)
                self.assertNotIn('activity_history',snap)

    def test_dm_with_required_life_boundary_keeps_history(self):
        self.add('morning',self.day+8*3600000)
        self.ingest('m1')
        snap = self.store.tick('main',self.clock,view='dm')
        self.assertIn('no_current_activity',snap['work_manifest']['life_required'])
        self.assertEqual(snap['activity_history']['today'][0]['id'],'morning')

    def test_large_details_and_unknown_metadata_are_not_forwarded(self):
        self.add('safe',self.day+8*3600000,activity_key=['unexpected'],phase={'unexpected':True},
                 process_id=100,private_blob='x'*100000,repeat_reason='x'*1000)
        h = self.history()
        a = h['today'][0]
        self.assertIsNone(a['activity_key'])
        self.assertIsNone(a['phase'])
        self.assertIsNone(a['process_id'])
        self.assertEqual(len(a['repeat_reason']),240)
        self.assertNotIn('private_blob',json.dumps(h))

    def test_different_passive_labels_and_keys_surface_shared_rest_pattern(self):
        for i,label in enumerate(('소파에서 쉬기','폰 보기','누워서 영상 보기')):
            self.add('passive'+str(i),self.day+(10+i)*3600000,45,category='rest',label=label,
                     activity_key='rest:variant'+str(i))
        before=self.snapshot()['revision']
        hints=self.history()['hints']
        hint=next(h for h in hints if h['code']=='passive_leisure_concentration')
        self.assertEqual((hint['choice_count'],hint['today_minutes']),(3,135))
        self.assertNotIn('repeated_activity_today',[h['code'] for h in hints])
        self.assertEqual(self.snapshot()['revision'],before)
        self.store.apply('main',self.decision(),self.clock)  # advisory, not a forced transition

    def test_short_rest_choices_do_not_reach_review_threshold(self):
        for i in range(3):
            self.add('brief'+str(i),self.day+(10+i)*3600000,20,category='rest',label='rest '+str(i))
        self.assertNotIn('passive_leisure_concentration',[h['code'] for h in self.history()['hints']])

    def test_rest_uses_elapsed_time_and_never_an_overdue_or_future_tail(self):
        self.add('rest1',self.day+10*3600000,30,category='rest',label='rest one')
        self.add('rest2',self.day+11*3600000,30,category='phone',label='phone')
        self.add('rest3',self.day+12*3600000,20,category='rest',label='rest two',status='in_progress')
        self.add('future',self.clock+60000,120,category='rest',status='planned')
        self.assertNotIn('passive_leisure_concentration',[h['code'] for h in self.history()['hints']])

    def test_ninety_minutes_in_three_executed_choices_is_a_review_not_a_quota(self):
        for i in range(3):
            self.add('rest'+str(i),self.day+(10+i)*3600000,30,category='휴식',label='쉬기 '+str(i))
        hint=next(h for h in self.history()['hints'] if h['code']=='passive_leisure_concentration')
        self.assertEqual(hint['today_minutes'],90)
        self.assertTrue(self.history()['advisory_only'])

    def test_one_rest_process_is_not_three_new_choices(self):
        for i in range(3):
            self.add('rest'+str(i),self.day+(10+i)*3600000,45,category='rest',label='rest '+str(i),
                     process_id='same-rest')
        self.assertNotIn('passive_leisure_concentration',[h['code'] for h in self.history()['hints']])

    def test_sleep_and_gameplay_are_not_inferred_passive_from_the_label(self):
        for i,category in enumerate(('sleep','gaming','hobby')):
            self.add('active'+str(i),self.day+(8+i)*3600000,45,category=category,label='누워서 쉬면서 게임')
        self.assertNotIn('passive_leisure_concentration',[h['code'] for h in self.history()['hints']])

    def test_explicit_hobby_key_overrides_generic_rest_category(self):
        for i in range(3):
            self.add('game'+str(i),self.day+(8+i)*3600000,45,category='rest',label='게임 '+str(i),
                     activity_key='hobby:game'+str(i))
        self.assertNotIn('passive_leisure_concentration',[h['code'] for h in self.history()['hints']])

    def test_rest_after_a_day_with_active_hobbies_is_not_a_passive_concentration(self):
        self.add('game',self.day+6*3600000,180,category='gaming',label='known hobby',activity_key='hobby:known')
        for i in range(3):
            self.add('rest'+str(i),self.day+(10+i)*3600000,30,category='rest',label='쉬기 '+str(i))
        self.assertNotIn('passive_leisure_concentration',[h['code'] for h in self.history()['hints']])

    def test_yesterday_rest_does_not_fill_today_review_threshold(self):
        for i in range(3):
            self.add('old-rest'+str(i),self.day-DAY_MS+(10+i)*3600000,60,
                     category='rest',label='rest '+str(i))
        self.add('today',self.day+12*3600000,30,category='rest',label='today rest')
        self.assertNotIn('passive_leisure_concentration',[h['code'] for h in self.history()['hints']])


if __name__ == '__main__':
    unittest.main()
