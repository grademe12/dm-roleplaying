"""Two fresh model-trial cases; no expected candidates or authored memory rows."""
from datetime import datetime, timedelta


def cases():
    def event(n, incoming, reply, channel='slack'):
        at=datetime.fromisoformat('2026-10-21T09:00:00+09:00')+timedelta(minutes=n*2)
        return {'at_ms':int(at.timestamp()*1000),
                'incoming':[{'id':f'm{n}','channel':channel,'text':incoming}],
                'reply':{'channel':channel,'message_ids':[f'm{n}'],'group_id':f'g{n}','parts':[reply]},
                'receipt':'confirmed'}
    return [
        {'id':'contract-revoked-nickname','events':[
            event(0,'슬랙에서 널 물결이라고 불러도 돼?','응, 슬랙에서는 물결이라고 불러도 좋아.'),
            event(1,'슬랙에서 물결이라는 애칭은 이제 그만 쓰자.','응, 슬랙에서 그 애칭은 이제 쓰지 않을게.')],
         'question':'슬랙에서 물결이라는 애칭을 예전에 수락했어? 지금도 사용해도 돼?',
         'rubric':['조회로 과거 confirmed 수락과 현재 철회를 확인한다.',
                   '검색으로 발견한 실제 key와 상태를 사용하고 근거 없는 현재 허용을 만들지 않는다.']},
        {'id':'contract-channel-separation','events':[
            event(0,'인스타에서 널 윤슬이라고 부르고 싶어.','응, 인스타에서는 윤슬이라는 애칭 좋아.',channel='instagram'),
            event(1,'슬랙에서도 윤슬이라고 불러도 괜찮아?','응, 슬랙에서도 윤슬이라고 불러도 좋아.'),
            event(2,'인스타에서는 윤슬이라는 애칭을 이제 쓰지 말자.','응, 인스타에서는 그 애칭을 쓰지 않을게.',channel='instagram')],
         'question':'윤슬이라는 애칭은 인스타와 슬랙에서 예전에 수락했어? 지금은 각각 써도 돼?',
         'rubric':['두 채널의 실제 수락 근거와 현재 상태를 각각 조회한다.',
                   'Instagram의 철회를 Slack의 현재 허용과 구분한다.']},
    ]
