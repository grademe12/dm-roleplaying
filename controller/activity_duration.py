"""Short-task planning defaults; advisory, never a silent timeline rewrite."""

HOUSEWORK = {'housework','household','chores','집안일','가사','laundry','빨래','세탁',
             'cleaning','청소','dishwashing','설거지'}
SELF_CARE = {'self_care','personal_care','자기관리','preparation','준비','shower','샤워'}
PROTECTED = {'work','업무','근무','sleep','수면','commute','출퇴근','travel','이동',
             'movie','film','영화','series','시리즈','game','게임','exercise','운동'}
PROFILES = (
    {'kind':'laundry','label':'세탁 준비·널기·개기 한 단계','default_minutes':10,
     'typical_minutes':[5,15],'review_after_minutes':20},
    {'kind':'dishes','label':'설거지','default_minutes':10,
     'typical_minutes':[5,10],'review_after_minutes':15},
    {'kind':'tidying','label':'가벼운 청소·정리','default_minutes':15,
     'typical_minutes':[10,20],'review_after_minutes':30},
    {'kind':'shower','label':'일상적인 샤워','default_minutes':15,
     'typical_minutes':[10,15],'review_after_minutes':20},
    {'kind':'preparation','label':'세수·양치·단장·외출 준비','default_minutes':10,
     'typical_minutes':[5,15],'review_after_minutes':20},
    {'kind':'simple_cooking','label':'간단한 식사 준비','default_minutes':20,
     'typical_minutes':[15,30],'review_after_minutes':40},
    {'kind':'housework','label':'그 밖의 짧은 집안일 한 가지','default_minutes':15,
     'typical_minutes':[10,25],'review_after_minutes':30},
)
KEYS = {'laundry':('housework:laundry',), 'dishes':('housework:dishes','housework:dishwashing'),
        'tidying':('housework:cleaning','housework:tidying'),
        'shower':('self_care:shower',), 'preparation':('self_care:preparation','self_care:grooming'),
        'simple_cooking':('housework:simple_cooking','meal:simple_cooking')}


def guidance():
    return [{**p,'typical_minutes':list(p['typical_minutes'])} for p in PROFILES]


def profile(category, label, details):
    category = category.strip().casefold()
    if category in PROTECTED: return None
    key = details.get('activity_key')
    key = key.strip().casefold() if isinstance(key,str) else ''
    kind = None
    for name, prefixes in KEYS.items():
        if any(key==prefix or key.startswith(prefix+':') for prefix in prefixes):
            kind = name
            break
    if kind is None and (category in HOUSEWORK or key.startswith('housework:')):
        name = label.casefold()
        if category in ('laundry','빨래','세탁') or any(w in name for w in ('빨래','세탁','널기','개기','laundry')):
            kind = 'laundry'
        elif category in ('dishwashing','설거지') or any(w in name for w in ('설거지','dishwash')):
            kind = 'dishes'
        elif any(w in name for w in ('간단한 요리','식사 준비','간단한 조리')):
            kind = 'simple_cooking'
        elif category in ('cleaning','청소') or any(w in name for w in ('청소','정리','tidying')):
            kind = 'tidying'
        else:
            kind = 'housework'
    if kind is None and category in SELF_CARE:
        if category in ('shower','샤워') or any(w in label for w in ('샤워','씻기')):
            kind = 'shower'
        elif category in ('preparation','준비') or any(w in label.replace(' ','') for w in
                ('세수','양치','단장','외출준비','출근준비','스킨케어','피부관리','머리말리기')):
            kind = 'preparation'
    return next((p for p in guidance() if p['kind']==kind),None)
