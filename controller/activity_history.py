"""Bounded, read-only context for choosing a plausible next activity.

Hints are advisory: they never reject a decision, schedule a wake, or rewrite
past activity. Labels/keys are observations of the ledger, not inferred events.
"""
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from common import decode
import activity_duration

KST = timezone(timedelta(hours=9))
DAY_MS = 86400000
TODAY_LIMIT, PREVIOUS_LIMIT, SUMMARY_LIMIT, HINT_LIMIT = 12, 8, 12, 8
ROUTINES = {'work','업무','근무','sleep','수면','meal','식사','commute','출퇴근',
            'travel','이동','self_care','자기관리','shower','샤워'}
HOUSEWORK = activity_duration.HOUSEWORK
PASSIVE = {'rest', '휴식', 'phone', '폰보기', '휴대폰보기', 'screen_time'}
ACTIVE_LEISURE = {'hobby', 'gaming', 'exercise', 'screen', 'music', 'reading', '취미'}


def short(value, limit=100):
    if not isinstance(value,str): return None
    return value.strip()[:limit] or None


def norm(value):
    return ' '.join(value.casefold().split())


def snapshot(db, session, now):
    today = datetime.fromtimestamp(now/1000,KST).replace(hour=0,minute=0,second=0,microsecond=0)
    day_start = int(today.timestamp()*1000)
    since = day_start-2*DAY_MS
    rows = db.execute('''SELECT id,category,label,status,start_ms,planned_end_ms,actual_end_ms,
        attention,location,details_json FROM activities WHERE session_id=? AND start_ms<=?
        AND status IN ('in_progress','completed','interrupted')
        AND (status='in_progress' OR actual_end_ms>? OR start_ms>=?) ORDER BY start_ms,id''',
        (session,now,since,since)).fetchall()
    today_rows, previous_rows = [], []
    categories, repeated, discretionary = {}, defaultdict(list), defaultdict(list)
    long_chores, passive = [], []
    discretionary_today_minutes = 0
    for r in rows:
        details = decode(r['details_json'])
        details = details if isinstance(details,dict) else {}
        key = short(details.get('activity_key'))
        phase = details.get('phase') if details.get('phase') in ('active','waiting') else None
        # Past a planned end, the ledger does not establish continued execution.
        end = min(now,r['actual_end_ms'] if r['actual_end_ms'] is not None else r['planned_end_ms'])
        end = max(r['start_ms'],end)
        item = {k:r[k] for k in ('id','category','label','status','start_ms','planned_end_ms','actual_end_ms','attention','location')}
        item.update(planned_minutes=(r['planned_end_ms']-r['start_ms'])//60000,
                    elapsed_minutes=(end-r['start_ms'])//60000,
                    overdue_minutes=max(0,now-r['planned_end_ms'])//60000 if r['status']=='in_progress' else 0,
                    activity_key=key, phase=phase, process_id=short(details.get('process_id')),
                    repeat_reason=short(details.get('repeat_reason'),240),
                    duration_reason=short(details.get('duration_reason'),240),
                    duration_guidance=activity_duration.profile(r['category'],r['label'],details))
        is_today = r['start_ms']>=day_start or end>day_start or r['status']=='in_progress'
        (today_rows if is_today else previous_rows).append(item)
        category = norm(r['category'])
        group = categories.setdefault(category,{'category':r['category'],'today_count':0,'today_minutes':0,
                                                'previous_count':0,'previous_minutes':0})
        if is_today:
            group['today_count'] += 1
            group['today_minutes'] += max(0,end-max(day_start,r['start_ms']))/60000
        if r['start_ms']<day_start:
            group['previous_count'] += 1
            group['previous_minutes'] += max(0,min(end,day_start)-max(since,r['start_ms']))/60000
        identity = key or category+':'+norm(r['label'])
        routine = category in ROUTINES or (key or '').split(':')[0] in ROUTINES
        if not routine:
            discretionary[identity].append(item)
            if is_today:
                repeated[identity].append(item)
                discretionary_today_minutes += max(0,end-max(day_start,r['start_ms']))/60000
        key_family = (key or '').split(':')[0]
        if (is_today and not routine and key_family not in ACTIVE_LEISURE
                and (category in PASSIVE or key_family in PASSIVE)):
            # Count only today's established execution, never an overdue tail
            # or the planned duration of a just-started rest block.
            minutes = max(0, end-max(day_start,r['start_ms']))/60000
            if minutes > 0:
                passive.append((item, minutes))
        chore = category in HOUSEWORK or (key or '').startswith('housework:')
        duration = item['planned_minutes'] if r['status']=='in_progress' else item['elapsed_minutes']
        budget = item['duration_guidance']
        if is_today and budget and duration>budget['review_after_minutes']:
            long_chores.append({'code':'long_housework_block' if chore else 'long_short_activity',
                                'activity_ids':[r['id']],'label':r['label'],'minutes':duration,'phase':phase,
                                'kind':budget['kind'],'default_minutes':budget['default_minutes'],
                                'review_after_minutes':budget['review_after_minutes'],
                                'review_due_ms':r['start_ms']+budget['review_after_minutes']*60000,
                                'duration_reason':item['duration_reason']})
    hints = list(long_chores)
    passive_choices = len({i['process_id'] or i['id'] for i,_ in passive})
    passive_minutes = sum(minutes for _,minutes in passive)
    if (passive_choices >= 3 and passive_minutes >= 90
            and passive_minutes >= .6*discretionary_today_minutes):
        # Different labels/keys can hide repeated default rest choices. This
        # asks for comparison, not a quota, forced hobby, or reason to wake.
        hints.insert(0,{'code':'passive_leisure_concentration',
                      'activity_ids':[i['id'] for i,_ in passive[-4:]],
                      'choice_count':passive_choices,'today_minutes':round(passive_minutes,1),
                      'discretionary_today_minutes':round(discretionary_today_minutes,1)})
    for key, items in repeated.items():
        processes = {i['process_id'] for i in items}
        if len(items)>1 and not (len(processes)==1 and None not in processes):
            hints.append({'code':'repeated_activity_today','activity_key':key,
                          'count':len(items),'activity_ids':[i['id'] for i in items[-4:]]})
    # Wash/hang/fold in one process are one choice, not three new hobbies/chores.
    choices = {key:len({i['process_id'] or i['id'] for i in items}) for key,items in discretionary.items()}
    total = sum(choices.values())
    for key, count in choices.items():
        if count>=3 and count/total>=0.6:
            hints.append({'code':'recent_activity_concentration','activity_key':key,
                          'count':count,'discretionary_count':total})
    summary = sorted(categories.values(),key=lambda r:(-r['today_minutes'],-r['today_count'],r['category']))
    for r in summary:
        r['today_minutes'] = round(r['today_minutes'],1)
        r['previous_minutes'] = round(r['previous_minutes'],1)
    return {'as_of_ms':now,'timezone':'Asia/Seoul','today_start_ms':day_start,'since_ms':since,
            'today':today_rows[-TODAY_LIMIT:],'previous_days':previous_rows[-PREVIOUS_LIMIT:],
            'today_count':len(today_rows),'previous_count':len(previous_rows),
            'today_has_more':len(today_rows)>TODAY_LIMIT,'previous_has_more':len(previous_rows)>PREVIOUS_LIMIT,
            'categories':summary[:SUMMARY_LIMIT],'categories_has_more':len(summary)>SUMMARY_LIMIT,
            'hints':hints[:HINT_LIMIT],'hints_has_more':len(hints)>HINT_LIMIT,
            'duration_guidance':activity_duration.guidance(),'advisory_only':True}
