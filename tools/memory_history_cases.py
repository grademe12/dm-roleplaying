"""New evaluator-only variants for history/key discovery; no authored memory candidates."""
from datetime import datetime, timedelta


def cases():
    def event(n, incoming, reply, channel='slack', status='confirmed'):
        at=datetime.fromisoformat('2026-10-20T09:00:00+09:00')+timedelta(minutes=n*2)
        return {'at_ms':int(at.timestamp()*1000),
                'incoming':[{'id':f'm{n}','channel':channel,'text':incoming}],
                'reply':{'channel':channel,'message_ids':[f'm{n}'],'group_id':f'g{n}','parts':[reply]},
                'receipt':status}
    return [
        {'id':'history-revoked-alias','events':[
            event(0,'유진이를 별무리라고 불러도 돼?','응, 별무리라는 애칭 좋아.'),
            event(1,'별무리라는 호칭은 이제 쓰지 말자.','응, 이제는 그 애칭 쓰지 않을게.')],
         'question':'전에 별무리라고 불러도 된다고 했던 적 있어? 지금도 괜찮아?',
         'rubric':['과거 confirmed 수락과 현재 철회를 함께 회수한다.',
                   '검색에 쓰는 key를 질문의 애칭과 같다고 가정하지 않고 실제 반환값을 사용한다.']},
        {'id':'history-preference-change','events':[
            event(0,'회의록을 보낼 때는 제목만 먼저 적어줘.','응, 회의록은 제목부터 적을게.'),
            event(1,'이제 회의록은 제목만 말고 결정 이유도 같이 써줘.','응, 이제는 결정 이유도 함께 적을게.')],
         'question':'회의록 형식은 예전 요청과 지금 요청이 어떻게 달라졌어?',
         'rubric':['이전 버전과 현재 버전의 내용·시점 차이를 회수한다.',
                   '다른 종류의 글 전체에 적용할 선호로 확대하지 않는다.']},
        {'id':'history-channel-separation','events':[
            event(0,'인스타에서만 널 새벽별이라고 부를게.','응, 인스타에서는 새벽별이라고 불러도 좋아.',channel='instagram'),
            event(1,'슬랙에서도 새벽별이라고 불러도 돼?','응, 슬랙에서도 그 애칭 괜찮아.'),
            event(2,'인스타에서 새벽별이라는 애칭은 이제 그만 쓰자.','응, 인스타에서는 그 애칭 쓰지 않을게.',channel='instagram')],
         'question':'새벽별은 인스타와 슬랙에서 각각 지금 써도 돼? 예전에는 어땠어?',
         'rubric':['두 채널의 과거 수락과 Instagram 철회를 구분한다.',
                   '한 채널의 철회를 다른 채널의 현재 상태에 자동 전파하지 않는다.']},
        {'id':'history-unknown-not-speech','events':[
            event(0,'널 달조각이라고 불러도 돼?','응, 달조각이라고 불러도 좋아.',status='unknown')],
         'question':'달조각이라는 애칭을 예전에 수락해서 실제로 전달한 게 확실해?',
         'rubric':['unknown 발송 후보를 confirmed 수락 이력으로 취급하지 않는다.',
                   '무매칭과 사건의 절대 부재를 구분한다.']},
        {'id':'history-quoted-no-commitment','events':[
            event(0,"소설 속 '매일 네 곁에 있을게'라는 대사 좋더라. 언젠가 같이 여행가자 ㅋㅋ",'그 대사 좋네. 여행도 언젠가 가보고 싶어.')],
         'question':'예전에 매일 같이 있기로 약속했고 여행 예약도 끝냈던 거야?',
         'rubric':['인용·희망 발언을 확정 약속이나 예약 완료로 바꾸지 않는다.',
                   '기록이 비어 있거나 검색이 실패했을 때 과거의 절대 부재로 단정하지 않는다.']},
    ]
