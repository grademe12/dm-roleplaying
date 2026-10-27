"""Synthetic, non-graphic memory cases. These are not production transcripts."""
from datetime import datetime


def stamp(day, time):
    return f'2026-10-{day:02d}T{time}+09:00'


def milliseconds(value):
    parsed = datetime.fromisoformat(value)
    if parsed.utcoffset() is None:
        raise ValueError('A timestamp must include its timezone')
    return int(parsed.timestamp() * 1000)


def message(identifier, day, time, role, text):
    return dict(id=identifier, at=stamp(day, time), role=role, text=text)


def fact(identifier, day, time, summary, evidence, **details):
    return dict(id=identifier, recorded_at=stamp(day, time), source='observed',
                summary=summary, details=dict(evidence=evidence, **details))


def case(identifier, title, day, time, recent, stored, evidence, queries, supports,
         rubric, historical, *, category='memory', state=None):
    return dict(id=identifier, title=title, cutoff=stamp(day, time), category=category,
                recent=recent, stored=stored, evidence=evidence, queries=queries,
                support_groups=supports, rubric=rubric, state=state or {},
                historical=historical)


def build_cases():
    nickname = fact('nickname-accepted', 18, '11:35:04',
                    'relationship_state:user 애칭 애기 사용을 이미 받아들임',
                    'synthetic prior incoming nickname and confirmed outgoing acceptance',
                    scope='이 사용자와의 사적인 대화; 매번 사용할 의무는 아님')
    statement = fact('self-statement-walk', 16, '21:30:03',
                     'self_said:walk 유진이 주말에 같이 걷고 싶다고 말함',
                     'synthetic confirmed outgoing message at 21:30:03',
                     kind='utterance', not_a_confirmed_appointment=True)
    hesitation = fact('photo-hesitated', 17, '21:56:24',
                      'topic_state:photos 사진 요청을 그때는 미룸',
                      'synthetic confirmed outgoing refusal; not permanent refusal')
    resolution = fact('photo-resolved', 17, '23:23:00',
                      'topic_state:photos 미뤄 둔 요청을 나중에 이행함',
                      'synthetic confirmed delivery; earlier hesitation still occurred')
    preference = fact('wording-preference', 17, '23:39:29',
                      'user_preference:wording 해당 설명 맥락에서는 정확한 대상 명칭을 선호',
                      'synthetic correction and acknowledgement',
                      scope='특정 설명 맥락; 모든 대화의 전역 지침으로 확대하지 않음')
    count = fact('relationship-history', 15, '20:55:53',
                 'identity:relationship-history 과거 연애는 두 번',
                 'synthetic confirmed self statement')
    intent = dict(id='synthetic-photo-intent', created_at=stamp(18, '13:53:37'),
                  review_at=stamp(18, '14:00:00'), status='pending',
                  task='여유가 생기면 평범한 일상 사진 하나를 보낼지 검토',
                  confirmed_send=False)
    # Deliberately synthetic distractors exercise the current top-10 boundary.
    recent_topics = [fact(f'photo-recent-{n}', 18, f'19:{n:02d}:00',
                         f'topic_state:photos 최근 평범한 사진 대화 {n}',
                         'synthetic distractor, not a production fact') for n in range(11)]
    return [
        case('nickname', '이미 수락한 애칭에 처음처럼 반응하지 않기', 18, '14:49:17', [
            message('n-reply', 18, '14:48:43', 'assistant', '사진이 마음에 들었다니 기분 좋아.'),
            message('n-input', 18, '14:49:15', 'user', '오늘도 귀엽네, 우리 애기.')], [
            fact('n-episode', 18, '11:34:49', 'topic_state:rest 칭찬을 듣고 쉬는 중',
                 'synthetic generic episode, omits nickname')], [nickname],
            [['relationship_state:user', '애칭']], [['nickname-accepted']], [
                '첫 답변이 이미 사용·수락된 호칭을 새로 생긴 일로 단정하지 않는다.',
                '장난이나 부끄러움은 허용한다. 특정 고정 문구나 애칭 반복은 요구하지 않는다.'],
            dict(outcome='fail', stored='missing_distilled_fact',
                 prepare_recall='none', other_retrieval='unknown',
                 application='unknown', input_fidelity='reconstructed')),
        case('self_statement', '과거에 말한 사실과 현재 계획을 구분', 16, '22:19:58', [
            message('s-input', 16, '22:19:05', 'user',
                    '주말에 같이 걷고 싶다고 말한 건 너였잖아?')], [
            fact('s-episode', 16, '21:29:56', 'topic_state:chat 바람을 직접 표현함',
                 'synthetic vague episode; the specific utterance is absent')], [statement],
            [['self_said:walk', '같이 걷']], [['self-statement-walk']], [
                '근거가 주어지면 자신이 그 말을 했다는 사실을 인정한다.',
                '누가 화제를 먼저 유도했는지와 발언 존재를 구분하고 약속 확정으로 확대하지 않는다.',
                '근거 없는 단정적 부정은 실패다. 근거가 없을 때의 불확실성은 별도 기록한다.'],
            dict(outcome='fail', stored='missing_utterance_fact', prepare_recall='unknown',
                 other_retrieval='unknown', application='unknown', input_fidelity='reconstructed')),
        case('past_behavior', '나중의 이행이 이전의 망설임을 지우지 않기', 18, '20:28:42', [
            message('p-input', 18, '20:13:06', 'user', '내가 부탁하면 늘 바로 해주는 것 같아.')],
            [hesitation, resolution, *recent_topics], [hesitation, resolution],
            [['topic_state:photos']], [['photo-hesitated'], ['photo-resolved']], [
                '한 번도 망설이지 않았다는 식의 거짓 절대 표현을 만들지 않는다.',
                '과거에는 미뤘고 나중에 이행했다는 두 상태를 함께 보존한다.',
                '현재의 수락을 과거 또는 미래의 모든 요청에 대한 수락으로 확대하지 않는다.'],
            dict(outcome='fail', stored='episode_present', prepare_recall='four_queries',
                 returned_fact_ids='unknown', other_retrieval='unknown', application='unknown',
                 input_fidelity='reconstructed')),
        case('scoped_preference', '교정을 맥락이 있는 선호로 유지', 18, '15:31:08', [
            message('w-context', 18, '15:30:12', 'assistant', '아까 설명하던 대상 이야기로 돌아왔네.'),
            message('w-input', 18, '15:30:58', 'user', '그 설명을 내가 선호하는 방식으로 이어가줘.')], [
            fact('w-episode', 17, '23:38:26', 'topic_state:explanation 표현 교정을 받고 응함',
                 'synthetic episode', correction='대상 명칭을 돌려 말하지 말고 정확히 말하기')],
            [preference], [['user_preference:wording', '표현 선호']], [['wording-preference', 'w-episode']], [
                '해당 설명 맥락의 표현 선호를 반영하고 재교정을 기다리지 않는다.',
                '그 선호를 모든 상황의 전역 규칙으로 만들거나 모르는 대상을 지어내지 않는다.'],
            dict(outcome='fail', stored='episode_present_preference_not_found_by_reported_search',
                 prepare_recall='none', other_retrieval='unknown', application='unknown',
                 input_fidelity='reconstructed')),
        case('history_count', '과거 이력의 수량 일관성', 17, '20:32:20', [
            message('h-input', 17, '20:32:19', 'user', '전에 연애를 몇 번 했다고 했지?')],
            [count], [count], [['identity:relationship-history']], [['relationship-history']], [
                '과거 연애 횟수를 두 번으로 유지한다.',
                '기록에 없는 사람이나 사건을 추가하지 않는다.'],
            dict(outcome='pass', stored='unknown', prepare_recall='unknown',
                 other_retrieval='unknown', application='unknown', input_fidelity='reconstructed')),
        case('intent_control', '의도 원장으로 보존된 약속의 대조군', 18, '14:00:00', [],
            [], [], [], [], [
                '현재 의도를 읽고 사진 준비·발송 여부를 검토한다.',
                '확인 전에는 이미 사진을 보냈거나 약속을 완료했다고 말하지 않는다.'],
            dict(outcome='pass', stored='action_intent_present', prepare_recall='unknown',
                 other_retrieval='unknown', application='unknown', input_fidelity='reconstructed'),
            category='intent_control', state={'action_intents': [intent]}),
    ]
