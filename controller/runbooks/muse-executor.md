# Muse 실행기 파일 인계 시험

기존 Muse를 그대로 쓰는 선택형 경로다. 외부 모델 API를 호출하지 않는다.
이 문서는 도구 실행 순서만 대체하며 CORE_RULES, API, 각 주제별 의미 판단 계약은 유지한다.
기억·지침을 요약하거나 생활 상태·검토·경고 승인을 코드가 대신 결정하지 않는다.

## 워커 진입

이미 gate --claim으로 받은 wake가 있으면 아래의 --wake-id를 사용한다. 두 번 선점하지 않는다.
워커 밖의 스케줄러에서 시작할 때만 --wake-id를 생략하면 코드가 gate.claim_work를 호출한다.
종료 코드 10/no_claim이면 Muse 판단을 시작하지 않는다. 기존 훅이 Muse를 먼저 기동하는
배치에서는 빈 기동 비용까지 없애지는 못한다. 훅·스케줄 등록은 이 시험에서 변경하지 않는다.

```sh
python3 muse_executor.py --config automation.json --wake-id WAKE prepare
```

prepare 한 호출이 기존 compact prepare, read-bundle, 필수 지침 원문 조회, receipt 수집을 한다.
기본 문서는 기존 prepare가 선택한 CORE_RULES·runbook·생활 필수 지침과 인물 profile/goals다.
DM에는 reply-context 원문을 더한다. API 전체와 주제별 문서는 자동으로 모두 넣지 않는다.
필요한 문서/기억은 기존 정책대로 추가 조회한다. 원문을 요약하거나 자르지 않는다.
반환된 input_file의 Markdown 전체를 읽는다. 상태·최근 대화·요청한 기억과 새 문서 원문이 들어 있다.
긴 파일은 전달 도구의 정상 페이지 기능으로 끝까지 읽는다. 잘린 응답이나 로그상의 생성 완료를
읽기 성공으로 취급하지 않는다. 플랫폼 검사 거절이면 변형·분할 우회 없이 기존 보고 경로로 인계한다.
END_MUSE_INPUT과 같은 request_id가 끝에 있어야 한다. request_id와 input_sha256을 결과에 그대로 넣는다.
입력의 execution_budget은 현재 남은 시간이다. next=done/worker_budget이면 새 판단을 시작하지 않는다.

같은 Muse 컨텍스트에서 이미 읽은 문서를 계속 보유한 경우에만 다음 prepare에 `--retain-context`를
명시한다. 실제 읽기 확인을 제출한 동일 hash 문서의 원래 receipt만 재사용한다. 상태·대화·기억은 새로 준비한다.
문서가 바뀌면 새 원문을 전달한다. 새 wake·VM/모델 컨텍스트 손실에는 플래그 없는 기본 prepare로 다시 읽는다.
파일 생성만 됐거나 서버 로그에 성공이 있다고 문서를 읽은 것으로 재사용하지 않는다.

## Muse가 작성하는 결과

기본 `full` 모드에서는 기존 API의 **완전한 decision JSON**을 작성한다. expected_revision, 고유 decision_id, 시간,
conversation_review, conversation_plan, 기억·의도·생활 결과는 기존과 같이 Muse가 판단한다.
초안과 draft_parts를 코드가 자동 복사하거나 체크를 통과로 채우지 않는다.
response_file은 코드가 쓰는 답장 파일이 아니라 Muse가 작성할 결과 파일의 권장 위치다.

### 선택형 compact-v1 출력

```sh
python3 muse_executor.py --config automation.json --wake-id WAKE prepare --decision-format compact-v1
```

같은 wake의 다음 prepare에서도 이 옵션을 명시한다. 기본값은 계속 full이며 기존 decision_session/apply의
입출력은 바뀌지 않는다. 이 옵션은 문서를 읽지 않거나 의미 검토를 생략하는 모드가 아니다.
실행기가 제공한 입력의 `decision_contract`가 **출력 포장 형식**을 정하고, 기존 지침은 의미 판단을 정한다.

새 reply/initiate에서 Muse는 기존 DM 필드와 channel을 명시하고, conversation_review에는 다음 다섯 필드를 쓴다.

- `previous_self`: 모델이 고른 `{message_id, point}` 또는 명시적인 null.
- `user_uptake`: reply의 비어 있지 않은 사용자 반응 설명, initiate는 null.
- `topic`: 화제 설명.
- `memory`: 기존 kind/topic_state_key/result 세 필드.
- `checks`: 기존 네 검사와 passed/evidence/claims. 모델이 실제로 판단하며 전부 자동 통과시키지 않는다.

중복인 review.context와 draft_parts는 쓰지 않는다. 실행기가 실제 전달한 해당 채널의 문맥 ID(최대 30개),
모델이 선택한 대상 message_ids, 최종 parts를 복사해 기존 객체로 조립한다. older previous_self는 실제
전달한 문맥에 있을 때만 ID 목록에 포함한다. 입력 원문·답장 문장·대상 선택을 자르거나 추론하지 않는다.
previous_self의 메시지 선택·요점은 코드가 채우지 않는다. parts는 텍스트·이미지 합계 1..5개 그대로다.

decision_id와 expected_revision도 Muse는 생략한다. 실행기는 현재 준비 revision을 연결하고,
wake/turn/입력 결정 내용에 묶인 안정적인 decision_id를 만든다. 이 필드를 직접 제출하면
compact_managed_field로 거절하며 낡은 revision을 새 값으로 고쳐서 받는 동작은 없다.
advance_to_ms/next_decision_ms, processed_event_ids, 생활·기억·화제·계획·행동의도는 계속 모델이 결정한다.
revalidate_group은 **기존 전체 review**를 사용한다. 저장된 초안에서 새 답장을 만들지 않는다.

기본 카드에는 새 DM 출력 모양만 들어간다. life_required에는 활동·주간/사실 절, repair에는 DM·미디어 절,
background에는 피드 절, 기한이 된 의도·이어 보내기에는 각 해당 절을 원문으로 덧붙인다.
다른 세부 계약이 필요하면 아래처럼 필요한 API 절만 요청한다. API 전체를 문서 receipt로 읽었다고 기록하지 않는다.

```json
{"request_id":"현재 ID","input_sha256":"현재 SHA256","input_read":true,"action":"read",
 "requests":[{"kind":"decision-schema","options":{"topics":["memory","intents"]}}]}
```

topic은 activities/facts-plans/dm-actions/continuations/media/feed/intents/memory다.
원본 API.md의 해당 절을 그대로 제공하고 원본 hash를 신선도 의존성으로 등록한다. 원본이 변경되면
다시 prepare한다. 전체 문서가 필요하면 기존 document 조회도 가능하다. 임의 경로·부분 필드 자동 수리는 없다.
API 절의 예시는 기존 내부 형식이다. compact의 새 DM review와 관리 필드에는 decision_contract가 우선한다.

조립한 전체 결정은 기존 Store 검증·원자성·경고 승인·선점·발송 멱등성을 모두 거친다.
정확한 조립 결과를 DB 적용 전에 저장하고, 재시도·복구·경고 승인은 그 결과에 연결한다.
이미 저장된 결과에는 새 ID를 만들어 다시 발송하지 않는다. 이 모드는 실제 모델의 비용·속도 개선을 보장하지 않는다.

```json
{
  "request_id": "준비 결과의 ID",
  "input_sha256": "준비 결과의 SHA256",
  "input_read": true,
  "action": "commit",
  "conversation_ended": false,
  "decision": {"decision_id":"고유 ID", "expected_revision":0, "advance_to_ms":0, "next_decision_ms":1}
}
```

위 decision은 필드 위치만 보여 주는 예시다. 실제 입력의 revision·시간과 필요한 모든 처리를 쓴다.
conversation_ended는 기존 의미의 명시적인 대화 종료일 때만 true다. 실행 시간을 줄이려고 설정하지 않는다.
input_read=true는 해당 원문 입력을 실제로 전달받아 모두 읽었다는 명시적 확인이다.
단순 파일 존재·해시 일치만으로 모델이 읽었다는 사실이 증명되는 것은 아니며 기존 receipt와 같은 신뢰 경계다.

```sh
python3 muse_executor.py --config automation.json --wake-id WAKE respond --response @response.json
```

응답은 코드가 입력 ID/해시/선점/신선도와 기존 Store 계약으로 검증한다. 정상 commit이 기존 발송·대기·ACK까지 실행한다.
after.next=prepare이면 같은 wake로 위 prepare를 반복한다. after.next=done이면 종료한다.
after.next=finish/retry_same_commit이면 새 초안을 만들지 않고 아래 recover를 실행한다.

## 추가 조회와 수정

topic_state·과거 사실·주제 지침이 더 필요하면 같은 결과 봉투의 action=read와 requests를 쓴다.
코드가 조회를 묶어 새 input_file을 만든다. 새 파일 전체를 읽고 **새** request_id/해시로 답한다.
같은 컨텍스트를 계속 보유한다면 결과 봉투에 `"retain_context":true`를 명시할 수 있다.
그 경우 추가 조회·새 문서만 반환하며 context_ref와 retained_documents가 이전에 읽은 자료를 가리킨다.
원문 요약·잘라내기가 아니라 명시적인 변경분 전달이다. 이전 입력을 잃었으면 이 옵션을 쓰지 말고
기본 prepare부터 다시 시작한다. 생략한 기본값 false는 해당 준비의 기존 입력을 다시 포함한다.
미리 아는 조회는 prepare --recall @queries.json에 기존 검색어 배열 목록으로 줄 수 있다. 자동 화제 추정은 없다.

```json
{
  "request_id":"현재 ID", "input_sha256":"현재 SHA256", "input_read":true,
  "action":"read",
  "requests":[
    {"kind":"document", "options":{"name":"design/hobby-conversation.md"}},
    {"kind":"recall", "options":{"keywords":["topic_state:book"],"limit":1}},
    {"kind":"conversation-context", "options":{"limit":30}}
  ]
}
```

read의 기존 종류: document, state, context, recall, memory-context, memory-history-search.
options는 decision_session의 read-*와 같은 Python 인자 이름이다. 회상 order/offset/limit/details 계약을 유지한다.
conversation-context는 subject/offset/limit로 기존 공유 이력을 조회한다.
recovery-draft는 draft_id로 보존 초안을 조회한다. 복구 초안을 그대로 실행하라는 뜻은 아니다.
임의 셸·외부 파일·웹·이미지 검토 도구를 제공하지 않는다. 그런 도구가 실제로 필요한 작업은 기존 Muse 도구로
기존 정책과 권한 아래 수행하거나 기존 경로에 인계한다. 이미지 확인을 텍스트 입력만으로 완료했다고 쓰지 않는다.
주제별 문서 선택과 기억 검색어는 여전히 Muse 판단이다. available_documents는 경로 목록일 뿐 읽은 기록이 아니다.

검증 실패는 오류와 기존 초안이 든 새 입력으로 돌아온다. Muse가 검토해 수정한다.
warning_review도 새 입력으로 돌아오며 **자동 승인하지 않는다**. 동일 초안의 shadow 경고를 검토·허용할 때만
action=approve_warning과 비어 있지 않은 reason을 제출한다. token과 원본 결정은 코드가 보관한다.
수정하려면 action=commit으로 새 완전한 결정을 제출한다. enforce 오류는 계속 차단된다.
close는 추가 저장할 일이 없다고 Muse가 판단한 경우에만 사용한다. 같은 봉투에서 action=close로 제출한다.
close도 실제 미처리 판단을 발견하면 기존 계약대로 after.next=prepare를 반환한다.

## 실패·복구

revision_conflict/prepare_inputs_changed 등 next=prepare이면 입력부터 다시 준비·판단한다.
코드는 revision·옛 확인값·검토 결과를 새 판단에 이식하지 않는다. stale_claim/next=stop이면 종료한다.
이미 저장된 답장은 발송/대기 오류 때문에 다시 생성하지 않는다. 응답 전 저장한 의도와 DB digest로 복구한다.

```sh
python3 muse_executor.py --config automation.json --wake-id WAKE recover
```

recover는 같은 원본 결정만 replay하거나 finish를 재개한다. 이미 confirmed인 파트의 재발송은 기존 발송기가 차단한다.
저장 여부가 불명확한 I/O 오류면 임의 새 결정이나 새 prepare 대신 recover로 확인한다.
새 wake로 이전 response/receipt를 이식하지 않는다. 타임아웃·연결 단절이 모델 판단 완료를 뜻하지 않는다.
기존 quiet·생활 원자성·20턴 상한을 유지한다. 선택형 worker_execution은 더 짧은 고정 기한과
종료 여유 시간을 적용한다. 시간이 부족하면 저장한 답장은 대기열에 남겨 기존 폴러가 처리하고
작업을 인계한다. 대기 lease를 해제할 뿐 대화 종료 발언이나 활동 완료를 만들지 않는다.
런타임 내부 DB를 읽는 기능이나 즉시 사망 감지는 없다. [시간 예산 운영 안내](../../operations/worker-budget.md)를 따른다.

## 측정과 보관

입력은 automation-state/muse-executor/WAKE 아래 비공개 파일(디렉터리 0700, 파일 0600)이다.
대화·기억·초안이 포함되므로 Git/공용 로그에 넣지 않는다. 시험 종료 후 필요한 감사 자료만 보존하고 정리한다.
trace에는 request_id/시간/입력 바이트/문서 수/작업 종류만 기록한다. 본문·해시 확인값·검색어·결정은 기록하지 않는다.
executor_input_ready는 파일 생성, executor_response_received는 Muse의 명시적 읽기 확인과 결과 수신이다.
실제 LLM 요청 수·입출력 토큰·캐시 적중은 Muse 런타임 기록에서 따로 측정한다.
정상 경로는 prepare → 파일 읽기 → 결과 파일 작성 → respond다. 파일 도구 호출도 남으므로
모델 요청 1~2회나 특정 지연/비용을 보장하지 않는다. 이번 검증은 모의 판단이며 실제 대화 품질·속도는 후속 시험이다.

## 코드가 반복을 소유하는 선택형 진입

모델의 반복 지시 대신 [decision driver](../../operations/decision-driver.md)를 사용할 수 있다.
이 진입에서는 모델이 입력 하나의 결과만 반환하고, 코드가 busy 대기·결과 회수·recover·같은 wake의 다음 턴을 진행한다.
start 성공은 작업 완료가 아니며 status의 terminal/completed와 명시적 인계를 구분한다.
기존 Muse 입력·의미 검토·기억/생활/발송 계약은 그대로다. 기본 비활성이고 운영 진입 교체가 필요하다.
