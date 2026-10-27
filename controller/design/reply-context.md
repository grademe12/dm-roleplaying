# 답장 초안 작성 전 문맥 검토

이 문서는 판단 모델의 문맥 확인·조회·초안 검토 절차를 담당한다.
문장 작성의 공통 기준은 [conversation-rules](conversation-rules.md),
기억의 공통 기록·조회는 [memory-continuity](../runbooks/memory-continuity.md),
고유 말투는 [profile](character/profile.md)이 정본이다. 검토 항목을 답장의 고정 문장 순서로 쓰지 않는다.

## 1. 이번 대화 확인

- 답할 대상은 tick의 미처리 messages다. observation.channel로 메신저를 구분하고 prepare가 전달한 양방향 최근 대화를 화자·순서를 보존해 읽는다. 기본 30개, context-limit 범위 6~30개다. 직전 4~6개는 바로 앞 연결을 잡는 초점이며 나머지 입력을 생략하는 제한이 아니다.
- 원본 캐시는 Instagram `automation-state/recent_thread.json`, Slack `automation-state/recent_thread_slack.json`이다. Instagram은 fetched_ms 10분 경과·read_failing·possible_gap, Slack은 연결·heartbeat 2분·ingest_failed·backfill_failed 등 기존 상태를 확인한다. 불확실하면 기존 확인·defer 절차를 따르고 ingest는 수신 서비스에 맡긴다.
- 첨부 자리표시는 실제 내용 확인이 아니다. 내용을 보지 않았다면 이미지 내용을 아는 것처럼 답하지 않는다.
- 초안 전에 conversation_review.context에 채널·대상·읽은 최근 메시지 ID·직전 자기 발언과 요점·user_uptake·topic을 기록한다. 직전 자기 발언이 없으면 previous_self는 null이다. 선제 연락에서 실제 답이 없으면 그 상태를 그대로 반영한다.
- 다른 메신저의 관련 맥락은 접근 범위를 확인해 조회한다. 구조화 기억의 channel/scope는 공통 기억 계약을 따른다.

## 2. 현재 상태와 답장 가능 여부

현재 시각·활동·일정·attention·수면 기상 허용을 읽고 답장·defer·no_reply 방향을 정한다.
생활 경계가 필수이면 [life-boundaries](../runbooks/life-boundaries.md)를 따른다. 일반 DM마다 생활 전체를 다시 계획하지 않는다.
호칭과 말투는 확인된 최근 관계와 프로필을 따른다. 이번 톤·주제 선택에 필요한 경우 실제 relationship_state:user를 읽으며 점수를 모르면 추정하지 않는다.
선제 연락을 보낼지의 기준은 [autonomy](autonomy.md), 시간·생활 맥락은 [lifestyle](lifestyle.md)를 따른다.

## 3. 필요한 기억 확인

[memory-continuity의 공통 조회](../runbooks/memory-continuity.md#답장-전-공통-조회)를 따른다.
취미 화제의 topic_state와 자기 이력의 identity 조회는 필수 조건을 유지한다.
주제별 필드·조회 세부는 해당 문서를 읽고, 답에 필요하지 않은 전체 기억을 나열하지 않는다.
조회한 근거·발견 여부는 conversation_review.memory에 기존 형식으로 남긴다.

## 4. 답장 방향을 정한 뒤 문장 작성

[공통 대화 규칙](conversation-rules.md)에 따라 현재 질문·요청과 이어지는 대화에 답한다.
생성기가 켜져 있으면 생성기 계약으로 받은 원문을 사용하고,
수정은 feedback을 통한 허용된 재생성으로 한다. 꺼져 있으면 Muse가 직접 작성한다.
필요한 situation·기억·실제 친밀도·무응답 상태를 확인한 범위에서 전달한다.

일반 일상·취미의 소재·질문 이력과 conversation_plan·preflight는 [conversation-initiative](conversation-initiative.md)가 담당한다.
shadow/enforce 경고는 같은 계약으로 검토하고 passed 표기로 중복 검사를 우회하지 않는다.

## 5. 초안과 근거의 별도 검토

| 기존 점검 | 확인할 내용 |
|---|---|
| self_reset | 앞선 자기 발언을 재소개하거나 이미 형성된 분위기를 초기화했는가? 같은 반응 순서·표현·마무리 콜백이 반복됐는가? 무관한 화제나 미래 만남으로 현재 흐름을 회피했는가? 자연스러운 화제 전환과 구분한다. |
| missed_answer | 사용자의 핵심 답·질문·요청을 놓쳤는가? 설명을 예고만 했거나 요청한 깊이를 제공하지 않았는가? |
| conflict | 직전 발언·현재 상태·조회한 기억과 충돌하는가? 제안·계획·완료·공유를 혼동하거나 지적을 수습하려고 과거 사실을 만들었는가? |
| self_consistency | 지속되는 자기 주장마다 정확한 초안 구절과 실제 fact ID를 연결했는가? 과거 발언과 캐논의 관계를 확인했는가? |

각 점검에 초안과 근거를 대조한 이유를 남긴다. 실패한 부분을 고친 뒤 다시 검토한다.
지속되는 자기 주장이 없으면 self_consistency.claims는 빈 배열이다. 구체적인 주장·발송 기록 계약은 [biographical-continuity](biographical-continuity.md)를 따른다.
통과한 초안 그대로 conversation_review.draft_parts와 parts를 연결한다. 새 검토 필드나 최소 분량을 만들지 않으며 검토를 이유로 인물 말투를 평평하게 바꾸지 않는다.

## 실행 계약과 한계

reply·initiate·revalidate_group은 기존 conversation_review·대상·채널·초안 일치 검사를 따른다.
필수 필드가 없거나 점검이 실패하면 저장이 거부된다. defer·no_reply에는 이 검토가 필요 없다. 형식은 [API](../API.md)를 따른다.
검증기는 형식·참조·초안 일치를 확인하며 의미상 기억 활용이나 대화 품질을 보증하지 않는다.

저장 뒤 새 수신에 대한 초안 보류·재검토·late_incoming_ids의 처리는 [DM 런북](../runbooks/dm.md)과 [CRON_RUNBOOK](../CRON_RUNBOOK.md)을 따른다.
이 문서에 별도 발송·재시도 절차를 만들지 않는다.
