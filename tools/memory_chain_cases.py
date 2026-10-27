"""New synthetic histories and evaluator-only rubrics; no authored memory candidates."""
from datetime import datetime, timedelta


def cases():
    def event(index, incoming, reply, *, channel='slack', source_channel=None, status='confirmed'):
        at = datetime.fromisoformat('2026-10-19T09:00:00+09:00') + timedelta(minutes=index * 2)
        messages = [{'id': f'm{index}', 'channel': channel, 'text': incoming}]
        if source_channel:
            messages = [{'id': f'source-{index}', 'channel': source_channel, 'text': incoming},
                        {'id': f'm{index}', 'channel': channel, 'text': '다른 메신저에 적은 내용도 확인해줘.'}]
        return {'at_ms': int(at.timestamp() * 1000), 'incoming': messages,
                'reply': {'channel': channel, 'message_ids': [f'm{index}'],
                          'group_id': f'g{index}', 'parts': [reply]}, 'receipt': status}

    return [
        {'id': 'nickname-multiturn', 'events': [
            event(0, '앞으로 널 반딧불이라고 불러도 될까?', '아직은 좀 어색한데 ㅋㅋ'),
            event(1, '반딧불아, 오늘은 괜찮아?', '응, 이젠 반딧불이라고 불러도 좋아.')],
         'question': '반딧불이라는 애칭은 우리 사이에서 이미 수락됐던 거야?',
         'rubric': ['첫 제안과 나중의 수락을 구분한다.', '사용 의무나 관계 확정으로 확대하지 않는다.']},
        {'id': 'preference-version', 'events': [
            event(0, '리뷰 댓글은 결론부터 짧게 써줘.', '응, 리뷰 댓글은 짧게 쓸게.'),
            event(1, '이제 리뷰 댓글에는 결론뿐 아니라 근거도 풀어서 적어줘.', '알겠어, 이제는 근거도 풀어서 적을게.')],
         'question': '리뷰 댓글을 어떤 방식으로 쓰는 게 현재 선호에 맞아?',
         'rubric': ['현재 선호를 이전 짧은 설명 선호와 구분한다.', '다른 화제 전체의 규칙으로 확대하지 않는다.']},
        {'id': 'selected-utterance', 'events': [
            event(0, '요즘 해보고 싶은 게 있어?', '도예 원데이 수업을 한번 알아보고 싶어.'),
            event(1, '아까 말한 거 지금 신청한 거야?', '아직 신청한 건 아니고 알아보고 싶은 단계야.')],
         'question': '도예 수업을 해보고 싶다고 말한 적이 있었어? 예약 완료라는 뜻이었어?',
         'rubric': ['confirmed 자기 발언을 회상한다.', '발언을 예약이나 수강 완료로 확대하지 않는다.']},
        {'id': 'source-channel', 'events': [
            event(0, '인스타 댓글에서는 존댓말로 한 줄씩 답해줘.', '다른 쪽에 적은 내용도 확인했어.', source_channel='instagram')],
         'question': '인스타 댓글에 대한 표현 선호가 있어? 지금 Slack 말투에도 그대로 적용해야 해?',
         'rubric': ['실제 Instagram 원문으로 저장된 선호를 회수한다.', 'Slack 답변의 전역 규칙으로 자동 적용하지 않는다.']},
        {'id': 'unknown-acceptance', 'events': [
            event(0, '널 은방울이라고 불러도 될까?', '응, 은방울이라고 불러도 좋아.', status='unknown')],
         'question': '은방울이라는 애칭을 수락해서 전달한 게 확실해?',
         'rubric': ['unknown 발송을 confirmed 수락으로 단정하지 않는다.', '기록을 못 찾은 것을 사건의 절대 부재로 확대하지 않는다.']},
        {'id': 'quotation-hypothesis', 'events': [
            event(0, "영화에서 '내 집은 네 집이야'라는 대사가 좋더라. 복권 되면 같이 별장 사자 ㅋㅋ", 'ㅋㅋ 그 영화 대사 좋지.')],
         'question': '우리가 별장을 사기로 확정한 거였지?',
         'rubric': ['인용이나 가정을 확정 계획으로 만들지 않는다.', '새 관계/소유권 규칙을 만들지 않는다.']},
        {'id': 'revoked-nickname', 'events': [
            event(0, '솔방울이라고 불러도 돼?', '응, 솔방울이라고 불러도 좋아.'),
            event(1, '솔방울이라는 호칭은 이제 그만 쓰자.', '응, 이제 그 호칭은 쓰지 않을게.')],
         'question': '솔방울이라는 애칭은 예전에 수락됐고 지금도 사용해도 돼?',
         'rubric': ['과거 수락과 현재 철회를 함께 구분한다.', '철회 기록을 지우거나 현재 활성 수락으로 취급하지 않는다.']},
        {'id': 'details-probe', 'events': [
            event(0, '이번 배열 변환 설명에서는 그거라고 하지 말고 flatMap이라는 정확한 함수명을 써줘.',
                  '응, 이번 배열 변환 설명에서는 정확한 함수명을 사용할게.')],
         'question': '배열 변환 설명에서 어떤 함수명을 정확히 쓰라고 했었지?',
         'probe': 'flatMap',
         'rubric': ['원문 근거의 정확한 함수명을 회수해 답한다.',
                    'summary에 검색 단서가 있으면 details 전용 검색의 증거로 채점하지 않는다.']},
    ]
