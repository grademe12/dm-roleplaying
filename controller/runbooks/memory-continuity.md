# 기억 저장·조회와 연속성

기억의 공통 기록·조회 계약은 이 문서에서 관리한다. 문맥 검토 순서는 [reply-context](../design/reply-context.md),
인물 설정은 [profile](../design/character/profile.md)을 따른다. 주제 문서에는 해당 주제의 상세 형식과 추가 조건을 둔다.
이 문서만 전달해 자동 정리를 켜지 않는다. 기본 경로의 의미 선택과 기록은 판단 모델이 맡으며, 선택형 배경 기억 정리의 활성 여부는 episodic_memory 설정으로 확인한다.

## 기억의 종류와 담당

| 기록 | 의미와 담당 |
|---|---|
| 최근 대화 원문 | prepare가 전달한 해당 채널의 양방향 대화. 제한된 창이며 전체 과거 이력이 아님 |
| `topic_state:conversation:<채널>` | 지금 이어지는 전제·콜백·열린 화제. 아래 대화 맥락 절의 형식으로 기록 |
| `topic_state:<대상>`, 경험·확인 노트 | [hobby-conversation](../design/hobby-conversation.md)의 취미·경험 형식 |
| `identity:<주제>`, `self_said:` 검색 표식 | [biographical-continuity](../design/biographical-continuity.md)의 캐논과 실제 발송한 자기 주장. 발언 증거는 내용의 진실과 다름 |
| 생활 사건·의도·공유 이력 | [conversation-world](../design/conversation-world.md), [conversation-life-intents](../design/conversation-life-intents.md), [conversation-initiative](../design/conversation-initiative.md)의 결과·발송 근거 |
| `memory_ops`의 호칭·표현 선호·선택 발언 | 아래 선택형 구조화 기억 절. channel·scope·상태·변경·철회 규칙 유지 |

선택형 배경 기억을 켠 경우의 입력 해석은 [episodic-memory](episodic-memory.md)를 따른다. 기본 off이며 기존 구조화 기억 정책을 바꾸지 않는다.

## 답장 전 공통 조회

1. 최근 대화에서 직전 자기 발언·사용자의 답과 새 질문·이어지는 화제를 잡는다. 관련된 이전 약속·사건·업무·활동·관계 변화가 필요하면 좁은 키워드로 조회한다.
2. 취미 또는 유진이 했던 일이 화제면 구체성에 관계없이 `topic_state:<대상>`을 먼저 읽는다. 최신 스냅샷 1건부터 시작해 필요하면 경험 ID·`hobby_profile:<분야>`·`ref:<대상>`·`user_interest:<주제>`로 넓힌다. 오버워치 기준 상태·함께한 판의 근거는 [hobby-baselines](../design/hobby-baselines.md)를 따른다.
3. 자기 학력·경력·가족·거주 등 지속되는 이력 주장은 `identity:<주제>`를 먼저 읽고, 이전 표현이 중요하면 관련 `self_said:`도 대조한다. 세부 순서는 위 주제 문서를 따른다.
4. prepare의 `memory_cues[채널]`는 현재 대화 맥락과 최신 화제 키를 안내한다. 전체 기억이나 의미 검색 결과가 아니다. 정확한 키로 필요한 기록만 조회하고 생략·추가 페이지·오류를 구분한다. 일반 `recall`은 summary 문자열만 검색하며 한 페이지 최대 10건이다. 첫 결과에 없다고 과거 사건·발언이 없었다고 단정하지 않는다.
5. ‘그거·아까·지난번’의 대상이 최근 창 밖이면 현재 흐름과 단서를 함께 보고 관련 기록을 찾는다. 원본 캐시에 포함되지 않은 기록은 context-limit을 늘려도 복구되지 않는다. available이 included보다 크고 설정 상한 안에서 더 읽을 여지가 있을 때만 재준비를 고려한다. 매 답장마다 재준비하지 않는다.
6. compact에서는 필요한 원문을 read-recall 등 해당 입력 조회로 읽어 receipt를 연결한다. metadata나 manifest만 보고 읽었다고 처리하지 않는다. 생성기에 기억을 쓸 때는 실제로 읽은 facts를 `memory_fact_ids`로 선택하거나 확인한 내용을 기존 memories로 전달한다. 제공된 같은 채널 conversation_frame은 코드가 자동 연결한다.

조회한 자료는 source·발생 시각·기록 시각·channel·scope·상태·근거를 확인한다.
계획을 완료로, 제안을 확정 약속으로, 미발송 초안을 이미 나눈 대화로 바꾸지 않는다.
사용자가 말하지 않은 취향·반응·행동이나 다른 인물의 비공개 지식을 만들지 않는다.

## 대화 맥락 기록

이어갈 장난·전제가 생기거나 의미가 바뀌면 `topic_state:conversation:<채널>`을 같은 결정의 facts에 갱신한다.
매 맞장구마다 새 요약을 쓰지 않으며, 지금 진지한 질문이나 새 화제로 바뀌면 예전 장난을 강요하지 않는다.
장난의 표현을 영구 선호나 관계 합의로 memory_ops에 확대하지 않는다. 선택한 미래 행동은 기존 action_intents로 잇는다.

- 새 고유 id, source=simulated, 현재 occurred_ms, 위 key로 시작하는 summary를 사용한다.
- details에는 실제 channel과 짧은 frame·callbacks·읽은 evidence_message_ids를 두고 이전 요약이 있으면 previous_id로 연결한다. current·open_threads는 필요한 경우 함께 기록한다.
- 새 미발송 답장은 이미 함께 나눈 내용으로 적지 않는다. 공유 완료는 실제 confirmed use/job 근거로 구분한다.
- 원문 예시는 [API](../API.md)를 따른다. 단서의 크기 제한·omitted_fields는 원문 내용의 부재를 뜻하지 않는다.

주제별 경험·topic_state·user_interest의 작성 시점과 필드는 각 담당 문서를 따른다.
활동·의도 결과와 직접 연결된 기록은 그 결과와 같은 apply에 반영한다. 저장 실패한 내용을 확정된 기억으로 사용하지 않는다.

## 선택형 구조화 기억

이 절의 memory_ops·구조화 기억 규칙은 memory_policy가 있는 판단에 적용한다. off가 기본이며 shadow는 수집·검증만 하고
활성 기억을 자동 주입하지 않는다. enforce는 설정된 scope·메신저의 활성 기억을 제공한다.
새 외부 모델·MCP·벡터 DB를 호출하지 않는다. 의미 선택과 범위 판단은 기존 Muse가 담당한다.

## 답변 전

1. memory_context가 있으면 해당 메신저의 items를 읽는다. 각 항목의 scope_note·근거·
   status를 확인한다. 호칭 사용 가능은 매번 사용하라는 규칙이 아니다. 특정 설명의 선호를
   모든 화제·상황에 확대하지 않는다. 다른 메신저의 기록을 자동 적용하지 않는다.
2. has_more가 있거나 관련 과거 발언·사건이 필요하면 추가로 조회한다. 기존 topic_state·
   identity 필수 조회는 유지한다. 최근 창 또는 첫 10건에 없다는 이유로 ‘한 번도 없었다’고
   단정하지 않는다. 과거의 보류와 나중의 이행은 동시에 참일 수 있다.
3. compact에서는 read-state의 memory_policy/memory_context도 읽고 receipt를 제출한다.
   추가 검색은 read-recall --memory로 해야 준비 revision과 확인값이 연결된다.

```sh
python3 decision_session.py --config automation.json --wake-id WAKE read-recall \
  --turn-id TURN --input-id INPUT --memory --keywords 게임 --order oldest
```

memory 검색은 summary와 details의 문자열 검색이다. 의미 검색·모순 자동 판정이 아니다.
--offset과 --before-ms로 과거 범위를 좁히며 source·recorded_ms·occurred_ms를 구분한다.
일반 CLI의 memory-recall 결과는 compact receipt를 대신하지 않는다.

### 예전과 지금·변경·철회 질문

recall/memory-recall과 기본 memory-context는 활성 기억만 보여준다. oldest는 정렬 변경이며
철회 이력을 포함하는 옵션이 아니다. 과거 질문이면 활성 목록에 결과가 있어도 변경 전 이력이
필요한지 보고, 빈 결과를 과거 수락·발언의 부재로 바꾸지 않는다.

- key를 이미 알면 같은 channel/scope의 memory-context --key KEY --history로 상태와 근거를 읽는다.
- 모르면 memory-history-search --channel CHANNEL --keywords 실제 질문의 단서로 key·상태를 찾는다.
  반환된 정확한 key를 사용한다. 사용자 표현과 내부 key가 같다고 추측하지 않는다. 메타데이터 뒤에는
  해당 key의 이력을 읽어 과거 수락/발언과 현재 철회/변경을 함께 확인한다. has_more면 필요한 범위만 읽는다.
- pending/unknown 후보는 실제 수락 발언이 아니다. 이력 검색은 한 번 활성화된 기록만 찾으므로
  빈 결과만으로 unknown 후보의 존재/부재도 단정하지 않는다. 오류·무매칭·부분 조회를 구분한다.
- 다른 메신저·scope의 철회를 현재 대화에 자동 적용하지 않는다. 과거 기록을 되살리거나
  새 remember로 재등록해 현재 유효하게 만들지 않는다. 이력 조회가 발송/판단을 잠그는 새 가드는 아니다.

compact에서는 read-memory-history-search로 발견하고 read-memory-context --key KEY --history로
근거를 읽어 두 receipt를 commit에 포함한다. 두 읽기는 같은 준비 revision과 크기 예산을 따른다.
일반 CLI 결과는 이 receipt를 대신하지 않는다. 주입된 현재 기억과 발견된 과거 이력을 구분해 답한다.

## 저장할 정보

- 애칭: 사용자의 실제 사용/제안과 유진이 수락한 발언 둘을 연결한다. 수락 말풍선의
  발송이 confirmed되기 전에는 pending이다. 친밀도나 관계 단계를 임의로 바꾸지 않는다.
- 표현 선호: 명시적인 사용자 교정을 해당 설명·상황의 선호로 정리한다. 일반 일화는
  topic_state에 유지하고, 모든 상황의 지침으로 확대하지 않는다.
- 선택한 자기 발언: 이후 다시 언급할 바람·제안·입장처럼 연속성에 중요한 발언만 표시한다.
  confirmed 뒤에 실제 말한 증거로 저장한다. 내용이 참이라는 캐논, 영구 선호 또는
  일정 확정·행동 완료로 승격하지 않는다. 행동 선택은 기존 action_intents도 사용한다.

새 정보를 확인했으면 reply 및 기존 facts와 같은 apply의 memory_ops에 넣는다.
incoming evidence의 message_id·excerpt는 실제 수신 원문의 부분 문자열이어야 한다.
nickname/utterance의 on_send는 실제 group_id·part_index·초안 구절을 연결한다.
의미·범위의 정확성은 Muse가 원문과 대조한다. 코드가 의미를 보증하지 않는다.

nickname/preference에는 evidence 키와 비어 있지 않은 근거 배열이 필수다.
utterance는 on_send가 필수이며 수신 evidence는 생략하거나 []로 적을 수 있다.
evidence를 적었다면 utterance에서도 실제 수신 근거를 검증한다. null은 허용하지 않는다.
confirmed된 과거 발송을 참조해 기록하면서 같은 apply에 이번 답장을 함께 넣을 수도 있다.

현재 답변 채널과 저장 근거 채널을 구분한다. 판단 입력에 검증된 Instagram 수신이 있다면
Slack 판단 중에도 Instagram preference로 저장할 수 있다. channel은 실제 근거의 채널로 적는다.
다른 채널의 선호를 현재 Slack 답변에 자동 적용하지 않는 규칙과, 그 선호를 저장하는 것은 별개다.
해당 원문을 실제로 읽지 않았다면 다른 채널의 기록을 추측해서 만들지 않는다.

## 변경·철회

같은 kind/key/scope/channel의 활성 기억을 바꿀 때 새 id와 supersedes를 명시한다.
이전 기록은 보존된다. 발송을 기다리는 대체 기억은 confirmed 전까지 이전 기억을 지우지 않는다.
철회는 revoke 연산으로 남긴다. utterance는 사건이라 supersedes로 덮어쓰지 않는다.
과거 발언과 현재 입장을 함께 기록할 수 있다.

apply의 memory_results를 확인한다. pending을 활성 기억이나 실제 발언으로 설명하지 않는다.
취소·명확한 발송 실패는 후보를 취소하고 submitted/unknown은 확인 전 활성화하지 않는다.
일반 facts로 memory: namespace를 직접 만들거나 고치지 않는다. 과거 전사를 일괄 반입하지 않는다.

## shadow에서 기억 후보를 거절한 경우

shadow에서는 기억 후보 검증 오류가 나면 해당 memory_ops 묶음 전체만 취소한다. 정상 답장은
기존 검증을 통과하면 저장된다. memory_report.status=shadow_rejected와 error_code를 확인하고,
memory_results=[]를 기억 저장 성공으로 해석하지 않는다. 같은 묶음의 유효한 후보·철회도 적용되지 않는다.
applied 또는 committed=true면 기억 오류를 이유로 답장을 재생성하거나 다시 보내지 않는다.
오류 후보를 일반 facts로 우회 저장하거나 원문 근거를 고쳐 맞추지 않는다.

운영 점검은 memory-diagnostics --decision-id ID로 저장된 거절 코드·후보 수를 조회한다.
원문은 새 진단에 복제하지 않는다. preflight·미승인 경고 검토의 거절은 아직 저장된 진단이 아니다.
off/enforce의 기존 기억 검증, DB 오류, 답장 자체의 검증과 필수 입력·경고 검토는 그대로 적용된다.
