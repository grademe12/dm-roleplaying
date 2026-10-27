# JSON 입력과 실행 계약

선택형 `worker_execution`과 `gate --claim --invocation-id ID`는
[워커 시간 예산](../operations/worker-budget.md)을 따른다. `prepare.constraints.execution_budget`은
고정 deadline_ms·finish_reserve_ms·remaining_ms·basis를 제공한다. basis는 local_hook_request,
worker_entry_fallback 또는 기존 controller_session이다. 로컬 요청/진입 시각을 런타임 DB의 created_at으로 보고하지 않는다.
시간 부족 시 `next=done, reason=worker_budget`으로 인계할 수 있으며, 저장/발송/활동 완료를 뜻하지 않는다.
Store.wait_dm의 선택 Python 인자 lease_deadline_ms와 Dispatcher.send_due의 deadline_ms는
시간 예산 경로가 대기/발송을 제한하는 내부 옵션이며 기존 독립 CLI 기본값은 그대로다.
선택형 muse_executor의 `prepare --retain-context`와 응답 `retain_context:true`는 같은 Muse 컨텍스트에서
실제 읽고 보유한 동일 hash 원문만 재사용한다. 신규/유실 컨텍스트는 기본 prepare를 사용한다.

선택형 수신 감지 외부화는 [doorbell 연결](../operations/doorbell-wake.md)을 따른다.
`gate.py --check`는 기존 has_work 외에 현재 선점/재시도 정책을 반영한 읽기 전용
`wake_eligible`을 반환한다. 이것은 claim이 아니며 워커는 기존 --claim을 실행해야 한다.
`doorbell.py`는 별도 알림 원장의 snapshot/ack JSON RPC이고 주 DB·발송 권한이 없다.
`wake_receiver.py`만 실제 수신 backfill 후 캡처 seq를 인수하며, `wake_hook.py`와
런타임 셸은 로컬 게이트만 읽고 wake/silent로 끝난다. 기본 비활성이다.

시간은 UTC Unix ms 정수다. tick.now_ms와 revision을 기준으로 결정하며 사용자가 보낸 날짜를 시스템 시각으로 쓰지 않는다. 아래 결정의 숫자는 설명용이므로 실제 tick 값으로 바꿔야 한다.

## seed

examples/seed.json을 참고한다. 기본 transport_mode는 mock이다. 실제용은 external을 명시한다. 초기 수치·장소는 state로 제공하며 빠진 값은 운영 기본값으로 초기화한다. 기본값을 관측 사실로 취급하지 않는다.

메시지는 id, text, direction(incoming/outgoing), observed_ms(없으면 적재 시각), observation을 받는다. observation.identity는 platform, verified_composite, uncertain 중 하나다. 새로 저장된 메시지의 `observation.ingested_ms`는 컨트롤러가 기록한 적재 처리 시각으로 덮어쓴다. `observed_ms`와 비교하면 플랫폼 발송부터 DB 적재까지의 지연을 측정할 수 있다. 기존 메시지에는 이 필드가 없을 수 있다. 플랫폼 메시지 ID를 id로 쓰는 것을 권장하며, ID가 없을 때 Muse가 주변 순서와 스레드를 확인해야 한다. 텍스트만 같다는 이유로 같은 ID를 쓰지 않는다.

seed에 한해 disposition은 handled, unanswered, uncertain이다. handled 수신에는 handled_evidence가 필요하다. 기본 uncertain은 review_required로 보존한다. outgoing은 handled로 적재한다. last_seen 파일 하나만으로 답변 완료를 판정하지 않는다.

dry-run은 임시 DB에서 전체 입력을 검증하고 실제 DB는 만들지 않는다. seed는 기존 DB가 있으면 database_exists로 중단한다. 새 DB가 완성되기 전 오류가 나면 실제 DB 경로에 부분 데이터가 게시되지 않는다.

## ingest

```json
{"read_ok":true,"messages":[{"id":"platform-id","text":"안녕하세요","direction":"incoming","observation":{"identity":"platform","thread_id":"verified-thread"}}]}
```

읽기에 실패했다면 read_ok=false로 전달한다. 읽기 실패를 빈 성공 목록으로 바꾸지 않는다. 같은 ID+본문+방향은 중복 처리하지 않는다. ID가 같은데 본문이 다르면 message_identity_collision이며 자동 덮어쓰지 않는다. 편집된 메시지는 원본과 별도 확인이 필요하다.

## tick

state, activity, weekly_plan, appointments, events, messages, jobs, needs_decision, core_rules가 나온다. 이벤트는 기본 50개이며 `--after CURSOR --limit 50`으로 이어 읽는다. has_more가 참인 경우 확인하지 않은 이벤트까지 processed로 표시하지 않는다. 현재 v0.3의 미처리 메시지와 큐 목록은 전체 반환하므로 큰 미처리 backlog는 운영자가 먼저 정리해야 한다.

조회는 revision을 바꾸지 않는다. 시간 경과 반영도 Muse의 apply가 있어야 저장된다. 호출마다 core_rules와 해시를 제공하므로 크론 간 대화 문맥이 유지되지 않아도 최소 규칙을 읽을 수 있다.

### 판단 세션 입력

`decision_session.py ... prepare --mode auto|bundled|compact`는 tick 원본 API와 별개의 세션 출력 계약이다. auto(기본)는 동일 ID·본문의 snapshot.messages.text를 contexts 참조인 body_ref로 중복 제거하고, UTF-8 JSON 48 KiB 이하를 bundled로 반환한다. 초과하면 compact로 ID·메타·input_manifest만 제공한다. 48 KiB는 자동 선택 권고값이고 명시 bundled 출력이나 compact 목록은 크기 초과만으로 실패하지 않는다.

compact는 같은 turn_id/input_id의 `read-state`, `read-document`, `read-context`, `read-recall`로 원문을 조회한다. 상태 항목 선택·목록 페이지, 문서 이름, 채널별 메시지 ID/페이지, recall index/keywords를 받는다. 각 조회의 content를 읽고 receipt 문자열 배열을 `commit --input-receipts @receipts.json`으로 전달한다. required_state_sections의 필수 상태·필수 문서·대화 범위 누락은 저장 전 거절한다. 선택된 메시지의 내용·순서·새 수신과 준비 revision을 검사한다. 32 KiB와 64회는 권고값이며 원문을 자르거나 초과만으로 실패하지 않는다. 재준비 시 상태·문맥 확인값은 무효화하지만 같은 wake의 동일 해시 문서 receipt는 원래 토큰을 재제출할 수 있다. 이미 저장된 결정의 replay는 원래 idempotency 규칙을 따른다. 상세 명령은 [분리 조회 지침](runbooks/prepared-inputs.md)에 있다.

이 모드는 크기 예산으로 선택하며 플랫폼 검사·실패 종류를 분류하지 않는다. 필수 지침·주제별 검토를 생략하는 재시도 모드가 아니다. 기존 `conversation-context`는 사용/공유 이력 API로 유지한다.

## 판단 세션의 선택형 출력 축소

기존 decision_session.py가 `prepare --decision-format compact-v1` 출력을 지원한다. 파일 실행기를 켤 필요가 없다.
동일 prepare의 decision_generation을 commit에 --decision-generation으로 전달한다. 입력 분리 모드에서는 기존 receipt도 필요하다.
새 reply/initiate의 review 복사 필드와 결정 식별·revision만 세션 코드가 조립한 뒤 **이 문서의 기존 apply 계약**으로
검증한다. compact 결정을 apply에 직접 제출하지 않는다. 의미 검토·기억·계획·생활 판단은 유지한다.
필요한 API 절만 받는 read-decision-schema도 제공하며 전체 문서 읽기 receipt를 만들지 않는다.
기본 full은 유지한다. [기존 진입의 입출력·복구 계약](runbooks/session-compact.md)을 따른다.
선택형 파일 실행기는 별도로 같은 출력 조립기를 사용할 수 있다.

## 선택형 배경 사건 기억

[배경 기억 설정·명령](../operations/episodic-memory.md)의 기본 mode는 off다.
활성 설정에서는 prepare의 snapshot.episodic_memory에 채널별 선택 결과와 처리 지연·범위 메타데이터가 온다.
compact에서는 read-state/read-bundle의 동일 섹션을 읽어 receipt를 commit에 제출한다.
선택 결과는 생성기에 자동 전달하며 해당 버전이 정정·철회되면 memory_inputs_changed로 재준비한다.
독립 sidecar의 episode ID는 기존 facts ID나 memory_ops ID가 아니다. 기존 검토·identity·scope 계약은 유지한다.
배경 워커는 생활 DB를 읽기 전용으로 사용하고 설정·활동·발송을 변경하지 않는다.

## 선택형 기억 연속성

기본 off다. CLI `memory-configure --settings '{"mode":"shadow","scopes":["private_dm"]}'`로
실험 DB에서 명시적으로 설정한다. mode는 off/shadow/enforce다. 설정 변경은 revision을 올린다.
off에서는 수집을 거절하고 pending을 취소한다. shadow는 수집·명시 조회만 하며 자동 주입하지 않는다.
enforce는 tick/prepare에 memory_policy와 scope·channel이 일치하는 memory_context를 제공한다.
자동 목록은 메신저당 최대 3개, 전체 UTF-8 JSON 12 KiB 이하다. 인용을 자르지 않고 항목을 줄이며
has_more/total을 표시한다. gate에는 이 내용이 없다. 같은 결정의 선택형 memory_ops 예:

```json
{"memory_ops":[{"op":"remember","id":"pref-v1","kind":"preference","key":"code-naming",
  "scope":"private_dm","channel":"slack","summary":"해당 코드 설명에서는 정확한 함수명을 선호",
  "scope_note":"이 설명에서만 적용하며 전역 말투 규칙이 아님",
  "evidence":[{"message_id":"actual-incoming-id","excerpt":"정확한 함수명을 써줘"}]}]}
```

종류는 nickname/preference/utterance다. nickname은 수신 근거와 수락 발송,
utterance는 선택 발송 근거가 필수다. `on_send`는
`{"group_id":"actual-reply-group","part_index":0,"excerpt":"actual draft substring"}`이다.
실제 같은 채널의 텍스트 작업과 대조하고 해당 파트의 confirmed 뒤에만 활성화한다.
submitted/unknown은 pending, failed/cancelled는 취소다. preference의 on_send는 선택이다.
nickname/preference는 비어 있지 않은 evidence가 필수다. utterance는 수신 evidence 키를
생략하거나 []로 줄 수 있지만 confirmed 발송 근거는 여전히 필수이며 null은 허용하지 않는다.
evidence는 검증된 실제 수신 ID와 원문 부분 문자열이며 최대 4개다. 각 연산은 UTF-8 JSON 6 KiB 이하,
한 apply 최대 12개다. summary 400자, scope_note 240자, 근거 인용 200자, 발송 인용 400자 이하다.
memory_results의 id/status/reason을 확인한다. **shadow에서만** 기억 처리 중 발생한 ControlError는
memory_ops 묶음 전체를 savepoint로 롤백하고 정상 답장 등 나머지 결정을 계속 검증·저장한다.
이때 memory_results는 []이며 memory_report는
`{"mode":"shadow","status":"shadow_rejected","error_code":"사유 코드","operation_count":1}`이다.
배열이 아닌 memory_ops의 operation_count는 null이다. 입력·결정 digest는 원문 그대로 유지한다.
off/enforce의 기억 검증 오류, DB 오류, 답장·활동·revision 등 기억 외 검증 오류는 기존대로
전체 결정을 실패시킨다. preflight와 미승인 경고 검토는 답장·기억·진단을 모두 롤백한다.
기억 저장 채널은 근거 수신/발송 채널이다. 판단 중인 답변 채널과 달라도 실제 확인한 근거를
해당 채널의 기억으로 저장할 수 있으며, 이를 현재 답변 채널에 자동 적용한다는 뜻은 아니다.

`memory-diagnostics [--decision-id ID] --offset 0 --limit 20`은 저장된 shadow 거절 진단을
최신순으로 조회한다(limit 최대 100). session_id/decision_id/created_ms/error_code/operation_count와
mode/status, total/has_more를 반환한다. 후보·대화 원문은 담지 않으며 조회는 revision을 바꾸지 않는다.
실제 결정이 저장될 때만 진단도 저장된다. 세션 apply_finished trace에도 memory_status,
memory_error_code, memory_operation_count를 남긴다. applied+shadow_rejected는 답장 저장 성공이므로
기억 오류만 보고 답장을 다시 생성·전송하지 않는다. replay 응답은 기존 already_applied 계약이며
이전 거절 내역은 decision-id로 조회한다. 별도 진단 테이블 생성은 첫 DB 연결에서 수행한다.

같은 kind/key/scope/channel은 새 id와 supersedes로 변경한다. 이전 버전은 새 발송 확정 전까지
유지한다. `{"op":"revoke","id":"pref-v1","reason":"사용자 철회"}`는 이력을 남기고 숨긴다.
memory: 사실 namespace는 전용 경로가 소유한다. 일반 facts로 조작할 수 없고, utterance는 자기
주장의 사실 근거가 아니다. 철회·이전 버전·다른 채널/설정 scope·off/shadow 기억도 해당 근거로 거절한다.

`memory-context --channel slack [--scope private_dm] [--key code-naming --history] --offset 0 --limit 6`은
현재 또는 명시 키의 버전 이력을 조회한다. limit은 최대 10이다.
`memory-recall --keywords 함수명 --order oldest --offset 0 --limit 10 [--before-ms MS]`는
summary+details 문자열 검색이다. known_by와 기억의 활성 상태를 검사하며 off에서는 거절한다.
자동 의미 검색·모순 감지는 아니다. 기존 recall은 summary 검색과 기존 순위를 유지한다.
compact의 추가 검색은 `read-recall --memory --keywords ... --order oldest`로 같은 준비 revision과
receipt에 연결한다. 일반 CLI 조회는 compact receipt를 대체하지 않는다.

과거 수락·변경·철회 질문에서 기본 조회가 비어도 이력이 없다는 뜻은 아니다. 두 recall과
기본 memory-context는 활성 기억을 찾는다. key를 알면 memory-context --key KEY --history로
원문 근거와 상태를 함께 읽는다. key를 모르면 다음 발견 조회를 먼저 사용한다.

`memory-history-search --channel slack --keywords 애칭 [--scope private_dm] --offset 0 --limit 6`
은 해당 session/channel/scope의 **한 번 활성화된** active/superseded/revoked 기억에서
key·summary·scope_note·철회 reason·발송 인용·수신 근거 문자열을 검색한다(여러 검색어는 OR).
정확한 key를 미리 몰라도 발견할 수 있으며 의미 검색은 아니다. pending/cancelled 및 활성화 전
철회된 unknown 후보는 발견 목록에 포함하지 않는다. 진단하려면 기존 정확한 key의 이력을 읽는다.
결과 items는 id/key/kind/scope/channel/summary/scope_note/status와 생성·활성화·최종변경 시각,
supersedes/reason인 메타데이터다. 원문 evidence/job 인용은 같은 channel/scope/key의
memory-context --history에서 읽는다. 이력 발견 결과만으로 현재 호칭 허용·선호를 적용하지 않는다.
limit은 최대 10, has_more/total을 제공한다. order는 updated_ms와 id의 newest/oldest 순이며
before_ms는 최종 상태 변경 updated_ms의 exclusive 경계다. 전 상태 전이의 시점별 검색이 아니다.
명시적 진단 조회는 memory-context와 같이 off/shadow에서도 가능하나 자동 주입·legacy recall·
현재 사실 근거 검사는 바꾸지 않는다. 이 조회 자체가 정책을 활성화하지 않는다.

compact에서는 `read-memory-history-search --channel slack --keywords ...`로 발견하고
`read-memory-context --channel slack --key KEY --history`로 근거를 읽는다. 두 명령 모두
--turn-id/--input-id와 --scope/--offset/--limit를 받으며 준비 revision·권고 출력 예산·receipt를
검증한다. 반환 receipt를 commit의 input_receipts에 포함한다. 발견 조회는 --order/--before-ms도 받는다.
[판단 지침](runbooks/memory-continuity.md)을 참고한다.

## apply

```json
{
  "decision_id":"unique-decision-id",
  "expected_revision":4,
  "advance_to_ms":1791946800000,
  "next_decision_ms":1791948600000,
  "activities":[{
    "id":"rest-1","category":"rest","label":"집에서 휴식",
    "location":"home","start_ms":1791946800000,"planned_end_ms":1791948600000,
    "attention":"light","interruptibility":"anytime","visibility":"personal",
    "details":{"reason":"퇴근 후 쉬기","rates_per_hour":{"fatigue":-8}}
  }],
  "dm_decisions":[],
  "processed_event_ids":[]
}
```

최상위 필수값은 decision_id, expected_revision, advance_to_ms, next_decision_ms다. 결정의 같은 ID+같은 내용은 already_applied, 같은 ID+다른 내용은 오류다. revision 충돌은 전체 롤백이며 tick을 다시 읽고 **새 decision_id**로 판단한다. 알 수 없는 최상위 필드를 조용히 무시하지 않는다.

### 활동

새 활동 status는 planned, in_progress(기본), completed다. completed는 재개 시 지난 구간을 보충할 때 쓰며 actual_end_ms가 필요하다. 모든 새 활동 시작은 기존 advanced_to_ms 이상이어야 한다. 하나의 결정에서 지난 활동 완료와 다음 활동 시작을 순서대로 전달한다.

기존 활동 변경은 id, status, actual_end_ms, planned_end_ms, reason만 받는다. completed/interrupted를 다시 열 수 없다. planned→in_progress/cancelled, in_progress→completed/interrupted만 가능하다. 단순 종료 연장은 status를 유지하고 planned_end_ms와 reason을 준다.

attention: free/light/focused/unavailable. interruptibility: anytime/at_break/at_end. visibility: ordinary/personal/private. location=in_transit이면 destination이 필수다. 다른 장소로 이동할 때 이동 활동을 넣는다. 고정 일정 시간에 해당 활동을 수행한다면 details.appointment_id로 연결하고 장소도 맞춘다.

rates_per_hour는 fatigue/mood/hunger/social_fatigue의 시간당 변화량(-100~100)이다. Muse가 정하고 프로그램은 아직 반영하지 않은 구간만 계산한다. 즉시 효과는 metric_deltas와 metric_reason으로 전달한다. 수치는 0~100에 제한되며 비정상 숫자는 거절한다.

`tick.activity_history`는 다음 활동 선택용 읽기 전용 문맥이다. full/life view와 필수 생활 판단이 있는 dm view에 포함하고, 순수 DM/gate에서는 제외한다. 오늘(KST)과 앞선 이틀의 시작한 in_progress/completed/interrupted만 읽으며 planned/cancelled/미래 시작은 실행 이력으로 세지 않는다. today는 최근 12개, previous_days는 최근 8개를 시간순으로 보여주고 count/has_more를 제공한다. categories는 잘린 목록과 독립적으로 전체 조회 구간을 집계한다(최대 12분류). 날짜를 넘긴 활동의 시간은 KST 경계에서 나눈다.

항목에는 원장 ID·분류·라벨·시각·상태·attention/location, planned_minutes, elapsed_minutes, overdue_minutes와 선택 메타데이터가 있다. elapsed는 completed/interrupted의 actual_end 또는 진행 중 활동의 min(now,planned_end)까지이며, 예정 종료 후에도 계속 실행했다고 추정하지 않는다. 과거를 자동 완료/변경하지 않는다. hints는 long_housework_block(해당 짧은 집안일의 작업별 시간 기준 초과), long_short_activity(샤워/준비 등의 시간 기준 초과), repeated_activity_today(같은 키 또는 같은 분류+라벨의 당일 반복), recent_activity_concentration(최근 비필수 활동 선택 3회 이상·60% 이상)을 최대 8개 제공한다. 모두 advisory_only이며 apply 거절·발송 차단·새 기상 사유가 아니다. 분류/라벨의 자유 문장 의미를 완전히 판별하는 규칙도 아니다.

activity_history.duration_guidance는 짧은 작업의 kind/label/default_minutes/typical_minutes/review_after_minutes 목록이며 빈 이력에도 제공된다. 코드 기본값은 빨래 한 단계 10분(재검토 20), 설거지 10분(15), 정리 15분(30), 샤워 15분(20), 준비 10분(20), 간단한 조리 20분(40), 일반 집안일 15분(30)이다. 개별 항목의 duration_guidance에는 인식한 작업 기준이 들어간다. duration_reason은 새 활동의 기존 details에 선택적으로 저장한다. 시간 초과 힌트는 kind/default_minutes/review_after_minutes/review_due_ms/짧은 duration_reason도 포함한다. review_due_ms는 최초 start_ms부터의 기준 시각으로, 연장 시 현재 시각으로 초기화하지 않는다. 자동 기상·소급 종료·계획 시각 덮어쓰기를 하지 않는다. 업무/수면/이동/운동/게임/영화 등 알려진 본래 긴 분류는 짧은 작업 인식에서 제외한다. 짧은 작업은 우선 activity_key, 그 밖에는 알려진 집안일/자기관리 분류 안에서 라벨을 참고해 인식한다.

새 활동의 기존 details에 `activity_key`(일관된 대상, 예: housework:laundry), `process_id`(같은 세탁 과정의 단계 연결), `phase`(active/waiting), `repeat_reason`(새 필요로 같은 활동을 반복하는 이유)을 선택적으로 저장할 수 있다. 오늘 같은 process_id의 후속 단계는 반복 경고에서 제외하고 최근 편중에서도 한 선택으로 센다. 식사·수면·업무 등 알려진 필수 분류는 반복/편중 경고 대상에서 제외한다. 키가 없는 기존 이력도 라벨·시간·분류로 보여준다. 새 스키마/필수 필드/하드 가드는 없다. 실제 선택은 runbooks/activity-selection.md와 생활 지침을 따른다.

### 주간·일정·기억

- weekly_plans: week_start(KST 월요일), remote_date(평일 또는 null), seed, reason. 기존 재택일을 바꿀 때 change_reason도 필요하다. 새 재택일을 과거 날짜로 생성하지 않는다.
- appointments: id, start_ms, end_ms, status(proposed/confirmed/cancelled/completed/missed), fixed(bool), location, details. 확정 일정끼리 겹칠 수 없다. tick(full/life/dm/gate)과 준비 입력의 appointments는 아직 끝나지 않은(end_ms>now) 일정 중 confirmed는 7일 안에 시작하는 것, proposed는 24시간 안에 시작하는 것을 start_ms 순으로 최대 20개 반환한다. 목록에 없다고 원장에 없다는 뜻은 아니다.
- facts: id, source, summary, occurred_ms(선택), details. canon/observed는 details.evidence가 필수다. simulated는 실제 외부 행동으로 기록하지 않는다.
- relationships: subject_id, other_id, details. details.evidence_ids는 이미 저장된 facts ID 또는 같은 apply의 facts ID 목록이다.

### DM 결정

아침 기상 연락은 `persona.MORNING_CONTACT`가 켜진 인물에서만 쓴다(유진은 꺼짐: `tick.morning_contact.status=disabled`, routine 지정 시 `morning_contact_disabled`). 켜진 경우 `initiate`의 선택 필드 `routine: "morning_contact"`로 명시한다. 비어 있지 않은 텍스트 파트 목록·정상 conversation_review/plan을 요구하며, 같은 KST 날짜의 예약/발송은 재등록하지 않는다. `tick.morning_contact`와 `morning_contact_due`가 기록된 주 수면 종료 뒤의 첫 연락 필요성을 제공한다. 회사 도착·첫 근무·수업 시작의 당일 경계를 사용하고 임의 3시간 만료는 없으며 기한이 지난 미시작 파트만 폴러가 취소한다. 이전 선제 그룹은 review_groups로 판단 대상으로 올리고, 확실한 미발송 텍스트 실패는 정상 아침 저장과 함께 정리한다. 일반 reply/initiate를 자동으로 아침 연락에 포함하지 않는다. 새 원장은 기존 DB 연결에서 생성되며 `prepared_inputs`나 분리 조회를 사용하지 않아도 동작한다. 자세한 조건·상태·not_sent 재시도는 [아침 연락](design/morning-contact.md)을 따른다.

| action | 필수 내용 |
|---|---|
| defer | message_ids, due_ms(미래), reason |
| no_reply | message_ids, reason. 의도적 무응답으로 handled 처리 |
| reply | message_ids, group_id, due_ms, parts(문자열 목록, 1~5개), reason, conversation_review |
| cancel_group | group_id, reason. 아직 confirmed 부분이 없는 그룹만 취소 |
| revalidate_group | group_id, reason, conversation_review. 새 수신 정보를 검토하고 기존 초안을 그대로 유지 |
| close_partial_group | group_id, reason. 확인된 일부 답장만으로 충분하다고 판단해 남은 부분 취소 및 수신 처리 완료 |
| resolve_message | message_id, identity(platform/verified_composite), evidence. 불확실한 관찰을 실제로 확인한 뒤 해소 |
| initiate | group_id, due_ms(과거면 즉시), parts(문자열 목록, 1~5개), reason, conversation_review. message_ids를 넣지 않는다. 유진이 먼저 보내는 선제 DM |

새 reply/initiate의 parts는 1~5개이며 6개 이상은 parts_limit_exceeded로 결정 전체가 롤백된다. 각 문자열이 말풍선 1개다. 상세 설명은 필요한 문단을 최대 5개 말풍선에 묶고 최소 분량이나 개수 채우기를 요구하지 않는다. 각 항목의 텍스트 검증과 `conversation_review` 계약은 유지한다. 전송 어댑터가 지원하는 본문 길이를 넘는 내용은 초안 단계에서 나누고, 검토 후 조용히 자르지 않는다.

기존 DB의 `part_index BETWEEN 0 AND 2` 제약은 `migrate_parts.py`로 먼저 변환해야 한다. 미변환 DB에서 4~5개 저장은 `parts_schema_migration_required`이며 결정 전체가 롤백된다. 변환은 조회나 apply에서 자동 실행하지 않는다.

`send-due`는 기본/최대 5개 작업을 처리한다(--limit 1..5). 새 답장도 최대 5개이며, 변경 전 저장된 더 긴 답장은 그대로 보존하고 5개씩 이어 보낸다. 기존 긴 그룹의 재검토는 원래 전체 초안을 대조하며 임의로 자르지 않는다. 기존 발송 간격·순서·새 수신 시 `draft_stale` 검토를 유지한다.

`reply`·`initiate`·`revalidate_group`의 `conversation_review`는 발송 대기열에 넣거나 보류된 초안을 재승인하기 전 필수다. 아래는 `reply` 예시다. `parts`를 검토한 뒤 그대로 `draft_parts`에도 넣는다.

```json
{
  "action":"reply", "message_ids":["user-42"], "group_id":"reply-42",
  "due_ms":1791946800000, "parts":["<검토를 마친 답장>"],
  "reason":"사용자의 답에 반응",
  "conversation_review":{
    "context":{
      "channel":"instagram", "recent_message_ids":["yujin-41","user-42"],
      "target_message_ids":["user-42"],
      "previous_self":{"message_id":"yujin-41","point":"<직전 자기 발언의 핵심>"},
      "user_uptake":"<사용자의 핵심 답과 새 질문>",
      "topic":"<이어지는 화제>"
    },
    "memory":{"kind":"general","topic_state_key":null,"result":"not_applicable"},
    "draft_parts":["<검토를 마친 답장>"],
    "checks":{
      "self_reset":{"passed":true,"evidence":"<직전 자기 발언과 초안의 연결 근거>"},
      "missed_answer":{"passed":true,"evidence":"<사용자 답·질문을 반영한 근거>"},
      "conflict":{"passed":true,"evidence":"<상태·기억과 충돌하지 않는 근거>"},
      "self_consistency":{"passed":true,"evidence":"<자기 이력·과거 발언 대조 근거>","claims":[]}
    }
  }
}
```

`context.recent_message_ids`는 해당 채널 최근 대화를 읽은 순서의 ID(최대 30개)다. 직전 유진 발언이 없으면 `previous_self`는 `null`이다. `reply`의 `target_message_ids`는 실제 `message_ids`와 정확히 같아야 하고 `user_uptake`는 비어 있으면 안 된다. `initiate`는 둘 다 빈 목록이며 `user_uptake`는 `null`이다. `revalidate_group`은 원래 그룹의 대상 ID를 적고, 이미 저장된 작업의 텍스트를 `draft_parts`에 적는다. `memory.kind`는 `general|hobby`다. 취미 화제는 먼저 조회한 `topic_state:<대상>` 키와 결과(`found|missing`)를 적는다. 그 외에는 예시처럼 적는다. 네 점검의 근거는 답장에 노출되지 않는다.

`self_consistency.claims`는 지속되는 자기 이력·취향·경험을 답장에 말할 때만 사용한다(최대 5개). 항목은 `{"excerpt":"<한 말풍선의 초안에 실제 있는 구절>","fact_id":"identity:education"}` 형식이다. 근거 사실은 운영 DB에 이미 있거나 같은 `apply`의 `facts`에 먼저 넣어야 한다. 과거 `self-said:` 발언은 사실 근거 ID로 사용할 수 없다. 해당 주장이 없으면 빈 목록을 쓴다. 컨트롤러는 구절이 한 텍스트 말풍선에 있고 사실 ID가 존재하는지만 검증한다. 캐논의 실제 의미와 과거 발언의 정합성은 Muse가 `design/biographical-continuity.md`에 따라 판단한다. 표시된 주장이 실제로 발송 확인되면 `self-said:<job_id>:<claim_index>` 관측 사실로 자동 기록되며, 이 기록은 발언 증거이지 내용이 참이라는 증거가 아니다.

컨트롤러는 대상 ID·채널·초안 일치, 검토 항목의 존재와 통과를 검사한다. 하나라도 빠지거나 `passed:false`면 결정 전체를 롤백하고 DM 작업을 만들지 않는다. 의미 판단 자체는 Muse 책임이며, 컨트롤러가 외부 최근 대화 파일의 내용이나 근거의 진실성을 자동 판정하지는 않는다. 검토 기록은 결정 이력에 남아 나중에 감사할 수 있다. `conflict`는 기록된 상태와의 충돌을 포함한다.

defer 후 reply를 만들 때 due_ms를 기존 예정 시각보다 앞당기지 않는다. 단, 수면 활동을 같은 `apply`에서 `interrupted`로 끝내면 그 수면 종료 시각까지 미뤘던 Slack 답장은 앞당길 수 있다. 실제로 깰 수 있는지는 게이트의 `sleep_interrupt` 결과를 따라 판단한다. reply의 기본 대상은 미처리 수신이다. 수신 대상 없이 먼저 보내는 DM은 initiate를 쓴다. 현재 활동이 `unavailable`이면 `reply`·`initiate`·`revalidate_group`은 `activity_unavailable`로 거절된다.

`tick.draft_stale_group_ids`는 새 메시지 적재로 관찰 revision이 바뀐 활성 그룹이다. 응답 가능한 활동일 때 `draft_stale`이 `needs_decision`에 올라가며 게이트와 `wait-dm`이 워커를 깨운다. 늦게 적재된 메시지도 초안 내용에 영향을 줄 수 있으므로 발송 시각만으로 자동 통과시키지 않는다. 일부 텍스트가 이미 발송된 그룹은 `cancel_group` 대신 `revalidate_group` 또는 `close_partial_group`으로 처리한다.

initiate 규칙:
- 미처리 수신(received/deferred/queued)이 있으면 `unanswered_incoming_exists`로 거절한다. 먼저 그 메시지에 reply/no_reply를 정한다.
- 일반 initiate는 아직 끝나지 않은 initiate 그룹이 있으면 `initiate_already_active`로 거절한다. 아래 명시적 이어가기 실행에 연결된 그룹만 같은 실행의 예약 그룹과 공존할 수 있다.
- 저장 후 발송 전에 새 수신이 적재되면 reply와 똑같이 `draft_stale`로 보류된다. 다시 판단해 `cancel_group` 또는 `revalidate_group`을 한다.
- tick의 `proactive_group_ids`는 진행 중인 initiate 그룹 ID 목록이다. cancel_group·close_partial_group은 reply 그룹과 같은 방식으로 동작한다.

attempting인 그룹은 외부 요청이 진행 중이므로 변경하지 않는다. submitted/unknown은 영수증 없이 후속 파트를 진행할 수 있다. 문맥 변경 시 미발송 부분만 revalidate하거나 close_partial_group으로 닫는다. 이미 흐름을 진행한 그룹은 cancel_group으로 원래 수신을 다시 풀어 재생하지 않는다. 미확인 원문·부분 전송 내용을 새 그룹에 복사하여 처음부터 다시 보내지 않는다.

processed_event_ids는 ingest가 만든 dm:메시지ID를 사용한다. 메시지가 아직 received이면 처리 완료를 거절하며, defer/reply/no_reply 등 결정을 먼저 내려야 한다. 중간 판단만 한 이벤트는 목록에서 제외한다.

## 혼합형 이어가기 — 제한된 사전 예약 + 보충 판단

사용자의 명시적인 계속 발송 요청에만 사용한다. 별도 `continuation_runs`/`continuation_groups` 원장으로 발송 소유권·세대·상한을 관리하며, 판단 기상은 기존 `decision_due`를 재사용한다. 생활 action_intents의 활동·결과 사실 계약을 이어가기 상태로 대체하지 않는다. 자동 문장 판별은 없고 요청/화제 판단은 Muse가 한다.

`apply.continuation_ops`는 최대 16개의 실행 단위 작업이다. 같은 ID의 작업을 한 apply에 두 번 쓰지 않는다. 실행은 채널당 active/paused 하나이며 종료한 ID는 재사용 불가다.

```json
{
  "continuation_ops":[{
    "action":"start", "id":"continue-unique", "channel":"slack",
    "source_message_id":"request-message-id", "interval_ms":60000,
    "buffer_limit":3, "reason":"사용자가 멈출 때까지 이어서 보내 달라고 요청"
  }]
}
```

이 조각을 필수 apply 필드와 함께 쓴다. source는 같은 채널의 검증된 incoming이어야 한다. `interval_ms`는 기본 60000, 범위 5000~3600000이다. `buffer_limit`는 2 또는 3(기본 3)이며, reply 첫 그룹도 버퍼에 포함한다. 선택 `expires_ms`는 미래 절대 시각, `max_groups`는 양의 정수다. 생략/null이면 각각 만료·총량 상한이 없다. 예약 버퍼 상한은 항상 적용된다. max_groups는 취소된 초안을 포함한 총 생성 그룹 수다.

같은 apply의 각 `reply`/`initiate`에 `"continuation":{"id":"continue-unique","generation":1}`을 추가한다. 그룹은 기존 parts/due_ms/reason/conversation_review/plan 계약을 모두 따른다. 첫 reply를 먼저 처리하고 같은 실행의 후속 initiate를 순서대로 붙인다. 이 실행이 이미 소유한 queued reply 대상만 initiate의 미응답 검사에서 제외한다. 다른 미응답 수신·다른 실행/일반 proactive 그룹의 활성 제약은 유지된다. 채널·generation 불일치, 버퍼/총량 초과 시 apply 전체를 롤백한다.

그룹은 등록 순서대로 보내며 앞 그룹의 시도가 끝나기 전에 다음 그룹을 건너뛰지 않는다. confirmed/submitted/unknown 반환은 흐름상 다음 단계로 진행할 수 있으며, 마지막 파트 시도 종료부터 interval_ms를 기다린다. 영수증 대기는 간격을 늘리지 않는다. quiet이면 기존 quiet 간격보다 짧아지지 않는다. due_ms·공통 직렬화·채널 429·실제 전송 중 차단도 적용하므로 지정 간격은 최소값이다. 이미지 생성 등으로 늦어져도 밀린 그룹을 몰아 보내지 않는다.

이 간격은 같은 실행·같은 generation의 후속 그룹에만 적용한다. 일반 채널 pacing은 유지하며 새 수신의 일반 답장이나 resume의 첫 그룹에 이전 generation의 이어가기 간격을 물리지 않는다.

활성 그룹이 1개 이하이고 review_at_ms에 도달하면 `tick.continuation_due_ids`와 `decision_due`로 보충 판단을 깨운다. work_manifest에는 life_required=`continuation_due`가 붙는다. 새 그룹 연결 시 review_at_ms는 now+interval_ms로 갱신한다. 보충·defer·pause·stop 없이 만기 실행을 남기면 `continuation_review_required`로 apply를 거절한다. tick 조회는 상태를 쓰지 않는다.

후속 작업 형식은 `{"action":"stop","id":"continue-unique","expected_generation":2,"reason":"사용자 중단"}`이다.

| action | 조건/추가 필드 | 결과 |
| --- | --- | --- |
| pause | active/paused | generation 증가, 미발송 취소, 보충 중지 |
| stop | active/paused | generation 증가, 종료, 미발송 취소 |
| resume | paused, source_message_id 필수 | 실제 attempting이 없어야 함. unknown/submitted의 영수증은 기다리지 않음. generation 증가, 새 그룹만 연결 가능 |
| defer | active, 미래 review_at_ms 필수 | 보충 판단만 연기. 이미 예약한 그룹은 계속 발송 |

새 incoming은 채널과 관계없이 같은 세션의 active 실행을 즉시 paused로 만들고 generation을 증가시킨다. 아직 한 파트도 안 보낸 reply의 대상은 received로 반환하고, 일부 confirmed인 reply는 handled로 유지한다. 동일 메시지 재적재와 outgoing은 수신 중단을 발생시키지 않는다. 다만 outgoing 등으로 관찰 revision이 달라진 초안은 발송 전 context_changed로 보류한다. resume 후에는 snapshot보다 1 증가한 generation을 새 그룹에 사용한다. 예전 generation으로 만든 결과는 현재 revision을 넣어 다시 제출해도 거절한다.

발송 직전 만료·unavailable·낡은 문맥과 실제 failed/미디어 실패는 실행을 종료/보류하고 pending/failed 파트와 해당 미디어를 취소한다. unknown/submitted 및 stale attempt 복구는 보류 사유가 아니며 원문 재전송 없이 다음으로 진행한다. 뒤늦게 알려진 미발송도 이미 통과한 흐름을 되돌리지 않는다. 만료는 expired, 총량 도달 후 흐름 완료는 completed가 된다. 실제 attempting 상태·영수증·확인된 파트는 유지하며 claim 후의 외부 요청은 회수할 수 없다. 별도 이유로 중단한 실행을 늦은 reconcile이 재개시키지는 않는다. 실행 소유 그룹에 일반 cancel/revalidate/close_partial/retry를 쓰면 continuation_group_owned로 거절한다.

`tick.continuations`에 실행 상태·generation·설정·총 생성/활성/완료 수·활성 그룹 순서가 나온다. open 실행은 항상 포함하고 종료 이력은 합쳐 최대 32개만 반환한다. gate view에는 ID·채널·상태·generation·활성 수·시간만 남기며 요청/사유 원문은 제외한다. decision_session prepare는 open 실행의 채널 문맥과 `runbooks/continuation.md`를 포함한다. 상세 판단 지침은 해당 runbook을 따른다.

## 결과 정리

```json
{"expected_revision":8,"job_id":"group-1:0","attempt_token":"tick에서 확인한 토큰","result":{"status":"confirmed","provider_message_id":"실제-message-id","evidence":"전송 말풍선 조회 결과와 ID 일치"}}
```

reconcile은 submitted/unknown만 받는다. confirmed는 메시지 ID와 근거, failed는 not_sent=true와 확실한 미발송 근거가 필요하다. 여전히 불명확하면 unknown으로 유지한다.

### DM 흐름 진행과 게시 확인의 분리

`dm_flow_passes(session_id,job_id,passed_ms)`는 전송 시도가 confirmed/submitted/unknown으로 끝났거나 stale attempting에서 복구되어 다음 파트로 진행할 수 있음을 기록한다. 실제 게시 증거가 아니다. 같은 job은 pending일 때만 claim하므로 unknown/submitted 원문을 자동 재전송하지 않는다. 첫 passed_ms는 고정이며 늦은 confirmed/failed 증거로 발송 간격·순서를 되돌리지 않는다. 확실한 실패 뒤 명시적으로 승인된 기존 이미지 재시도만 해당 pass를 제거하고 새 시도를 시작한다.

모든 파트가 흐름을 통과하면 reply_groups.status=completed, reply 대상은 handled가 되어 proactive 슬롯과 이어가기 버퍼를 비운다. 실제 게시 여부는 계속 dm_jobs.status/provider_message_id와 정확한 영수증으로 구분한다. completed만 보고 전달 완료 사실·self-said·media-sent·행동 결과 공유 완료를 만들지 않는다. 미확인·일부 확인된 conversation use와 매핑도 영수증 전까지 그대로 보존한다.

기존 DB 최초 연결에서 테이블/인덱스를 추가하고 기존 confirmed/submitted/unknown 작업의 updated_ms를 최초 pass로 가져온다. recover/claim/apply가 이미 시도를 마친 활성 그룹을 정리하므로 기존 unknown을 거짓 failed로 바꾸거나 수동 reconcile해야 발송을 재개하는 절차는 없다. tick은 delivery_uncertain_job_ids와 원래 작업 상태를 보여주며 dispatch_hold는 실제 attempting만 의미한다. gate/work_manifest는 unknown만으로 repair 판단을 강제하지 않는다. 수신·활동·만료·429 검사는 유지한다.

## 메신저(채널)

발송 확인용 자동 히스토리 스캔은 제거했다. 워커는 미확인 발송 시점으로 되돌아가는 조회를 요청하지 않으며, listener는 과거 요청 파일·발송 조회 cursor를 재개하지 않는다. 일반 수신 백필은 backfill_cursor/retry_ts와 별도 `slack-backfill-scan.json`으로 계속 복구하고, 각 페이지의 수신부터 반영하며 전체 완료 시에만 복구 경계를 전진시킨다. 자동 발송 증거 수집은 Socket 메시지로만 수행한다. 저장된 기존 영수증·수동 증거 확인·실제 attempting·수신 ingest 실패·초안 stale·수면/활동·429 가드는 유지한다. submitted/unknown은 기록과 확인만 유지하고 발송 차단에서 제외한다. 과거 `slack_history_sync.blocked`와 `slack-history-sync`는 호환 상태로만 남으며 발송을 막지 않는다. 원본 유진 저장소의 `2026-10-18/remove-delivery-history-scan/DEPLOY.md`는 이 저장소에 포함되지 않았다. 구버전 운영 환경에 적용할 때는 해당 원본 기록과 현재 [Slack 연결 안내](slack/SETUP.md)를 함께 대조한다.

한 세션 안에 여러 메신저가 있을 수 있다. 친밀도, 기억, 생활 상태, 대화 모드는 세션 하나로 공유한다.
- **수신**: ingest 메시지의 `observation.channel`(예: `instagram`, `slack`). 값이 없으면 `instagram`이다. 채널 값은 소문자·숫자·`-`·`_`, 32자 이하.
- **reply**: 대상 메시지의 채널로 간다. 대상들의 채널이 섞이면 `mixed_channel_targets`, 결정에 `channel`을 넣었는데 다르면 `channel_mismatch`다.
- **initiate**: `channel`(기본 `instagram`)
- **tick**: `jobs[].channel`, `channel_blocked_until_ms`(채널별 429 차단). 기존 `send_blocked`는 instagram 기준이다.
- **send-due**: `--channel` (기본 `instagram`). 그 채널의 작업만 보낸다.
  - 429는 그 채널만 막는다.
  - submitted/unknown은 같은 채널·그룹의 다음 발송을 막지 않는다. 원래 작업의 자동 재전송은 하지 않는다.
  - attempting은 세션 전체에서 하나만 허용한다. 발송 잠금도 세션 단위라 채널끼리 순서대로 보낸다.

## 대화 모드와 대화 세션
- tick의 `conversation_mode`는 세 가지다.
  - `live`: 현재 활동 attention이 free/light이고 최근 5분 안에 수신이 있거나, 수신에 대한 즉시 발송 가능 답장을 최근 5분 안에 저장함
  - `quiet`: 07시 이전, 또는 활동 attention이 focused/unavailable
  - `normal`: 그 밖
- 그룹이 끝난 뒤 Instagram 간격은 live 5초, normal 4분, quiet 10분이다. Slack은 live 5초, normal 20초, quiet 45초다. `unavailable`에서는 간격과 무관하게 발송이 금지된다. 잠자기 등 `unavailable` 활동이 시작된 뒤 남은 초안은 기상 후 `draft_needs_review`로 다시 검토하며, 그 전에는 `draft_stale_activity`로 발송이 보류된다. 같은 그룹의 말풍선 사이 간격은 3~5초다.
- `reply`·`initiate`의 `due_ms`가 현재보다 과거면 현재로 맞춘다(즉시 발송). defer로 정한 예정 시각보다 앞당기는 것은 여전히 `reply_due_advanced`로 거절한다.
- `wait-dm --session main --timeout N`(1~900초, 기본 180): 다음 중 하나가 생길 때까지 기다린다(2초 간격 DB 확인).
  - 새 수신(`new_dm`)
  - 생활 판단 시각 도달(`decision_needed`: decision_due/reply_due/activity_due/appointment_due)
  - 기존 초안이 새 관찰로 보류됨(`decision_needed`: draft_stale)
  - 시간 초과(`timeout`)

  기다리는 동안 `conversation_leases`에 lease(대기 종료 + 3분)를 잡는다. 대기가 끝나면 lease를 3분 연장해 Muse가 답장을 쓰는 동안 유지한다. `timeout`이면 lease를 지운다. tick의 `conversation_lease_until_ms`로 lease를 확인한다.
- `end-conversation --session main`: lease를 지운다.
- `release-lease --session main --owner WAKE_ID`: lease의 `owner`가 그 wake일 때만 지운다(다른 wake나 owner 없는 legacy lease는 건드리지 않음). 결과에 `released`, `was_live`, `received_count`가 있다.
- `conversation_leases` 테이블은 기존 DB에 처음 접속할 때 자동으로 생성된다. `owner` 열(lease를 잡은 wake ID, decision_session이 기록)도 없으면 자동으로 추가된다. tick의 `conversation_lease_owner`로 확인한다.
- 끝난 wake의 lease 정리: wake가 대화를 끝내지 못하고 종료되면(claim_deadline, ack failed/finished, ownership_lost) heartbeat daemon(`claim_heartbeat.py`)이 그 wake 소유 lease를 해제하고, 미응답 DM이 남아 있으면 Slack listener와 같은 `nudge` 설정으로 webhook을 한 번 보낸다(wake당 1회, 최소 60초 간격, 실패는 trace에만 기록). `gate.py --ack-phase finished|failed`도 자기 lease를 해제하며, `gate --claim`은 활성 claim이 아닌 wake가 소유한 lease를 orphan으로 보고 해제한 뒤 바로 claim한다(`--check`는 읽기 전용으로 eligible만 보고).

## send-due의 말풍선 묶음 발송
한 그룹(reply·initiate)의 말풍선은 한 번의 send-due에서 이어서 보낸다. confirmed/submitted/unknown 뒤 다음 말풍선 전에는 다음 말풍선 길이에 따른 3~5초를 기다린다(2초 + 글자당 0.15초를 3~5초로 제한). 같은 호출은 최대 5파트이며, 마지막 파트 뒤에는 채널별 대화 모드 간격 또는 명시적 이어가기 간격을 둔다. 실제 failed/429에서는 묶음 발송을 멈춘다. 다른 그룹은 같은 호출에서 이어 보내지 않는다. 중간에 새 수신을 ingest하면 다음 claim의 문맥/중단 검사를 적용한다.
## 반환과 exit code

JSON에는 ok, reason, revision이 나온다. 0=명령 처리가 완료됨(발송 성공을 뜻하지 않음), 2=revision_conflict, 3=busy, 1=입력·저장·검증 오류다. send-due는 외부 실패도 결과를 정상 저장했다면 exit 0이며 results[].status와 reason을 반드시 확인한다. status --human만 일반 텍스트다.

## action_intents — 대화에서 정한 다음 행동

`apply.action_intents`는 순서대로 처리하는 작업 배열이다. 생략하면 새 의도를 생성하지 않는다. 문장 자동 추출은 없다. Muse가 자신의 다음 행동을 선택했고 이유를 설명할 수 있을 때만 쓴다. 시간은 기존 apply처럼 최신 tick.now_ms에 근거한다. tick 조회와 CLI 저장 시각이 밀리초 단위로 일치할 필요는 없다. 이미 advanced된 과거를 고치는 것은 기존 계약대로 금지된다.

```json
{
  "action_intents": [{
    "action": "create",
    "id": "watch-severance-s1-e5",
    "target_key": "series:severance:s1:e5",
    "summary": "세브란스 시즌1 5화 시청",
    "self_chosen": true,
    "earliest_ms": 1792209600000,
    "review_at_ms": 1792210200000,
    "expires_ms": 1792281600000,
    "condition_text": "점심 뒤 쉬고 나서, 다음 일정 전 시간이 있을 때",
    "sources": {"message_ids": ["실제로 적재된 수신 ID"], "job_ids": ["실제 그룹 ID:0"]},
    "followup_text": "본 뒤 스포일러 없이 감상 이야기하기",
    "reason": "유진이 다음 행동으로 선택하고 대화를 마무리함"
  }]
}
```

위 예시는 apply의 추가 필드만 보여 준다. 실제 결정에는 기존 decision_id, expected_revision, advance_to_ms, next_decision_ms 등이 필요하다. 예시 ID와 시간은 실제 tick/원장의 값으로 바꾼다. 조건 설명은 기계 실행식이 아니다.

### 작업 계약

모든 작업은 `action`, `id`, 비어 있지 않은 `reason`이 필수다. 알 수 없는 필드는 거절한다.

| action | 추가 입력 | 허용 상태와 결과 |
| --- | --- | --- |
| `create` | target_key, summary, self_chosen:true, review_at_ms; 선택 earliest_ms, expires_ms, condition_text, sources, supersedes_id, followup_text | 새 ID만 가능 → pending |
| `link_sources` | sources | 열린 의도에 출처 추가. 상태 유지 |
| `start` | activity_id | pending → in_progress. 같은 결정의 현재 시각에 시작하는 in_progress 활동과 상호 연결 필요 |
| `defer` | review_at_ms; 진행 중이면 result_fact_ids | pending 유지 또는 진행 중 구간 종료 후 pending으로 복귀 |
| `complete` | result_fact_ids | in_progress → completed. 연결 활동의 completed와 새 결과 사실 필요 |
| `cancel` | 진행 중이면 result_fact_ids | 열린 의도 → cancelled. 진행 중이면 구간 종료와 부분 결과 필요 |
| `expire` | 없음 | expires_ms를 지난 pending만 → expired |
| `share_result` | sources.job_ids | completed에서 감상·결과를 담은 텍스트 job 묶음을 연결. 상태 유지 |

create 직후 같은 배열에 start를 넣을 수 있다. 기존 열린 target_key가 있으면 intent_target_already_open이다. 종료된 행동의 재실행은 새 ID로 가능하다. 변경된 계획을 대체할 때 먼저 cancel/expire한 뒤 새 의도의 supersedes_id로 연결한다. terminal 의도를 다시 pending으로 열지 않는다.

sources는 선택이며, message_ids/job_ids 각각 최대 32개를 받는다. self_chosen은 반드시 true이고 reason이 있어야 한다. 내부 선택은 출처 없이 가능하지만, 대화에서 정했으면 관련 ID를 연결한다. ID 존재와 세션을 검사하며 텍스트 job만 출처로 허용한다. 의미상 해당 발언이 그 행동을 정했는지는 Muse가 검토한다. 같은 결정에서 만든 reply/initiate job도 참조 가능하다.

### 시간과 개수

- earliest_ms 생략: 해당 결정의 advance_to_ms.
- expires_ms 생략: 결정 시각 + 24시간. 명시 시 최대 7일 이내이며 earliest ≤ review < expires, review는 결정 시각보다 미래여야 한다. 즉시 start할 때도 create에 미래 review를 넣는다. 시작하면 pending review는 사용하지 않는다.
- defer는 미래 review를 잡고 기존 expires_ms를 바꾸지 않는다. “오늘” 같은 말은 Muse가 KST 기준 만료를 명시한다. 더 긴 계획은 기존 일정·관심 기록을 활용하거나 별도 설계를 한다.
- expires는 **새 실행을 시작할 수 있는 창**이다. 이미 실행 중인 행동은 기계적으로 중단하지 않으며 활동 결과로 닫는다. 부분 실행 후 창이 지나 재개할 수 없으면 결과와 cancel 사유를 남긴다.
- 세션의 동시 열린 의도 최대 32개, 한 apply 작업 최대 64개. 이 제한은 할 일·변경 입력 관리 한도이며 답장 말풍선 개수와 무관하다.
- pending의 기본 판단 시각은 min(review_at_ms, expires_ms). 현재 활동이 아직 unavailable이면 planned_end_ms 뒤로 미룬다. 이 유효 시각이 지나면 tick.needs_decision에 기존 `decision_due`가 나오며 gate와 live wait-dm이 기존 경로로 처리한다.
- 같은 apply가 끝났을 때 처리되지 않은 유효 due가 남으면 intent_review_required다. 시작/미래 보류/취소/만료로 명시적으로 처리한다. unavailable 종료 전이면 due 처리를 그때까지 미룰 수 있다. 짧은 확인 루프를 만들지는 않는다.

### 활동과 결과 사실

start의 activity_id는 같은 결정에 담긴 현재 시각의 새 실행 구간이어야 한다. `activities[].details.action_intent_id`와 의도 id가 같아야 한다. 하나의 활동을 여러 의도에 중복 연결하지 않는다. 재개는 새 활동 구간을 연결하며 이전 구간은 남긴다. 기존 활동·이동·고정 일정 검증은 그대로이고, 시작 창 전/만료 후 시작과 unavailable의 조기 중단은 거절한다.

연결된 진행 중 활동을 끝내면서 의도를 in_progress로 방치하면 intent_activity_resolution_required다. complete 또는 진행 중 defer/cancel에는 같은 결정의 **새 simulated facts**가 필요하다.

```json
{
  "id": "experience:severance-e5:completion",
  "source": "simulated",
  "occurred_ms": 1792213200000,
  "summary": "세브란스 시즌1 5화를 끝까지 보고, 인상 깊었던 지점을 기록함",
  "details": {
    "activity_id": "실제 연결된 시청 활동 ID",
    "action_intent_id": "watch-severance-s1-e5",
    "intent_outcome": "completed"
  }
}
```

result_fact_ids로 이 사실을 참조한다. occurred_ms는 연결 구간의 start~actual_end 안이어야 한다. complete는 활동도 completed, intent_outcome도 completed여야 한다. 중단 후 defer/cancel은 intent_outcome:partial을 쓴다. 일부 진행·아직 진전 없음 등 실제 결과를 적는다. 과거 사실 ID를 재사용하거나 summary만 같은 사실에 세부 근거를 덧씌우면 거절한다. 결과의 내용·충분한 경과·취미 진도 스냅샷은 Muse가 hobby-baselines에 따라 판단한다. 단순 시간 경과는 완료 근거가 아니다.

### 조회와 전달 상태

- `tick.action_intents`: 모든 pending/in_progress, 최대 32개. effective_review_ms, expired_by_clock, 출처, 연결 활동과 결과 사실 ID를 포함한다. 조회는 상태·revision을 바꾸지 않는다.
- `tick.action_intent_due_ids`: 현재 판단할 pending ID. unavailable이면 지연된 시각 기준이다.
- `tick.action_intent_followups`: followup_text가 있고 아직 결과 공유가 확정되지 않은 최근 completed 의도. 최근 7일 내 갱신된 최대 100건 중 20건을 맥락으로 반환한다. 별도 기상·발송·우선순위를 만들지 않는다. 전체 영구 이력은 inspect로 확인한다.
- `python3 ctl.py --db <DB> intent-inspect --session main --id <ID>`: terminal 포함 원장과 변경 이벤트·사유 전체 조회.
- 각 job 출처의 delivery_status/delivered는 실제 dm_jobs 원장에서 읽는다. confirmed와 provider_message_id가 함께 있어야 delivered다. unknown·부분 그룹·취소 초안을 전달 완료로 바꾸지 않는다.
- 결과 전달은 별도 share_result 작업의 job_ids **한 묶음 전부**가 confirmed일 때 result_shared:true다. 새 초안으로 다시 쓴 경우 새 작업으로 묶는다. 완료 이전에 작성된 계획 job은 재사용할 수 없다. 확정은 기존 발송기/reconcile만 수행한다.

feature는 의미 판단·자동 문자 추출·외부 앱 조작·새 메시지 발송을 수행하지 않는다. 모든 쓰기는 기존 apply의 revision 검사·멱등·transaction rollback에 묶인다. 정상 발송이나 reconcile이 출처를 확정하면 다음 조회가 그 상태를 반영한다.

## 일반 대화 소재·최근 반복 방지 (opt-in v1)

신규 모듈은 conversation_flow.py/topic_materials.py다. 첫 연결에서 추가 테이블을 원자적으로 생성한다. 기존 테이블을 재작성하거나 controller_meta 0.3을 바꾸지 않는다. 설치 전 기존 코드로 DB를 백업한다. `conversation_policy` 기본 mode는 off, 자동 탐색은 false다. 기존 queue/legacy 그룹은 계획 누락만으로 중단하지 않는다. 일반/취미 분류는 기존 review.memory.kind를 사용하며 범위 밖 묘사 지침은 수정하지 않는다.

CLI는 기존처럼 `python3 ctl.py --db <DB> <command> --session main` 형태다.

| command | 추가 인자와 동작 |
|---|---|
| conversation-configure | `--settings @file.json` 또는 JSON. 기존 값에 병합. mode off/shadow/enforce, 검색 플래그·예산·신선도·쿨다운 설정. 변경 시 revision 증가 |
| conversation-context | `--subject <subject_key>` 선택, `--offset 0 --limit 30`(최대 100). uses·답변/종료 근거·예약·정책과 기존 등록 units의 정규 키/point/fact를 반환. uses의 has_more/next_offset, units의 unit_has_more/next_unit_offset을 별도로 제공 |
| topic-materials | `--offset 0 --limit 30`. 출처 노트·소재·단위별 제외 사유·재확인 필요 여부·검색 예산 |
| conversation-preflight | `--decision @decision.json`. 전체 apply 계약을 같은 DB transaction에서 검증한 뒤 롤백. 등록 refs·정규 계획·경고 반환. **사전 검사 성공은 저장/예약이 아님** |
| topic-search-claim | 도구 검증/활성 플래그, 미처리 수신/발송/대화 lease, free/light 활동, 하루 예산·간격·동시 실행 확인 후 run_id/token/deadline 반환. 도구 실행·취소는 하지 않음 |
| topic-search-result | `--result @result.json`. 해당 token과 deadline을 검증해 결과 저장. 동일 결과 재실행은 멱등, 다른 결과 재사용·늦은 성공 거부 |

off는 새 계획을 강제하거나 이력 중복을 차단하지 않는다. 명시적으로 제출한 계획은 형식·근거 검증과 사용 이력 기록을 한다. shadow는 누락·이력 중복을 경고하고, enforce는 새 적용 대상 그룹의 누락·구체적 중복을 거부한다. 형식/근거 불일치는 제출한 계획이 있으면 모드와 무관하게 오류다. 기존의 conversation_review 검사는 모든 모드에서 그대로다.

### apply의 conversation_updates

최대 100개 연산을 facts 저장 후, DM 저장 전에 순서대로 실행한다. 공통 선택 `ref`에 이름을 주면 뒤 연산과 같은 결정의 계획에서 `@이름`으로 참조할 수 있다. ID는 코드가 정규 키로 발급하고 결과의 conversation_refs에 반환한다. 미등록 ID·다른 결정의 @ref·임의 ID 덮어쓰기는 허용하지 않는다.

- `register_material`: canonical_key, subject_key, kind(news/experience/general), fact_ids 필수. aliases, checked_ms, refresh_after_ms, details 선택. 출처 fact가 없으면 거부. 같은 fact/정규화 URL/등록 alias는 같은 소재로 해석한다. 같은 canonical key의 추가 facts는 합치며 과거 확인 시각으로 최신성을 후퇴시키지 않는다.
- `register_unit`: subject_key, kind(statement/question), facet_key, scope_key, summary, fact_ids 필수. material_id, point_keys 선택. 연결된 소재의 facts 범위에서 등록한다. 뉴스는 point_keys가 필요하다. point는 출처 details.point_keys에 있어야 한다. 같은 소재·같은 point 집합은 facet 표현을 바꿔도 기존 ID를 반환하고 부분 겹침은 오류다. 질문 ID는 material에 독립적인 대상·의도·범위로 결정한다.
- `link_alias`: material_id, alias_key, fact_ids 필수. 실제 근거를 대조한 뒤 사용한다. 서로 다른 기존 소재를 조용히 병합하지 않고 충돌을 반환한다.
- `record_evidence`: unit_id, use_id, kind(answer/close/shift/reopen), refs 필수. refs는 `{message_id,excerpt}` 또는 `{job_id,excerpt}` 목록. answer는 실제 incoming만 가능. job은 confirmed여야 하고, 모두 해당 use 전달 이후의 실제 구절이어야 한다. 애매한 호응의 의미 판정은 Muse 책임이다.

### reply/initiate/revalidate의 conversation_plan

형식은 `{version:1, units:[...], non_substantive:[...]}`다. 각 unit은 unit_id, mode, spans, basis를 갖는다. spans는 `{part_index,excerpt}` 목록이며 같은 구절이 반복되면 `char_start,char_end`(Unicode 문자, 끝 제외)를 함께 준다. 이미지 파트는 텍스트 매핑에서 제외한다. 모든 비공백 텍스트를 단위 또는 non_substantive로 설명해야 한다.

| mode | basis |
|---|---|
| answer | 실제 현재 reply target의 request_refs 필수. `{message_id,excerpt}` 목록. 과거 메시지를 가져온 blanket 면제 금지 |
| introduce | 등록된 단위의 첫 소개. fact_ids/point_keys 보충 가능, request_refs/prior_use_ids 면제 불가 |
| continue | prior_use_ids 필수. 현재 reply 수신이 있고, 최근 전달된 같은 대상의 열린 흐름이어야 함. 끝낸 흐름을 재시작하는 면제로 사용 금지 |
| reintroduce | prior_use_ids와 현재 request_refs 또는 unit의 fact_ids. 이전 공유와 시간 조건을 그대로 검사 |
| update | prior_use_ids, 새 fact_ids, 새 point_keys 필수. 이전 사실·point 재사용 불가. 실제 새 진전의 의미는 Muse가 대조 |

non_substantive는 `{mode:acknowledge 또는 close,spans:[...]}` 목록이다. 종료·짧은 호응·연결 구절용이다. 소개나 질문을 숨기지 않는다. 텍스트 매핑과 참조가 맞는다고 의미 분류까지 코드가 증명하는 것은 아니다. 지속되는 자기 주장은 기존 self_consistency도 별도로 충족해야 한다.

기본 정책: 끝낸 요점의 선제 재도입 2시간, 같은 뉴스/경험 요점 24시간 억제. introduce는 이미 전한 요점을 시간이 지났다고 다시 새 소개로 쓰지 못한다. reintroduce에는 현재 연결 근거가 필요하다. 답변된 질문은 시간만으로 해제하지 않는다. 진행 중 대화·재설명 요청·실제 새 진전은 좁은 단위로 검사한다. 같은 소재의 예약은 다른 메신저에서도 중복 선택을 보류한다.

최종 apply가 같은 함수를 transaction 안에서 재검사하고 작업 생성과 use 예약을 원자적으로 저장한다. 구체적 중복은 conversation_unit_reserved, conversation_material_already_shared, conversation_recent_reintroduction, conversation_question_answered 등으로 거부한다. stale/superseded 뉴스 point도 자동 소개를 거부한다. reason/detail에 기존 use/job/unit 등 식별 근거를 반환한다. 실패하면 facts/활동을 포함해 **그 apply 전체가 롤백**된다. 부분 성공으로 보고하지 않는다.

정상 전송·수동 reconcile·Slack receipt reconcile은 같은 use 확정 함수를 쓴다. confirmed 파트만 전달된 것으로 보고 partial·reserved·released를 구분한다. unknown/submitted와 미발송 불명 실패는 use의 확인 상태만 보류한다. 다음 DM 발송을 막는다는 뜻은 아니다. 취소·확실한 미발송만 남으면 해제하고, 일부 전달 이력은 유지한다. 확정되었거나 unknown/submitted인 구절 매핑은 revalidate에서 제거할 수 없다.

### 검색 결과

result는 `{run_id,token,status,facts:[],materials:[]}`이며 status는 success/no_candidates/failed/timeout이다. 실패·후보 없음에는 facts/materials를 넣지 않는다. 성공은 최대 search_max_materials(기본 3)개의 news 소재 등록 입력이다. facts는 observed이고 details에 evidence, url, publisher, published_ms(null 가능), event_ms(null 가능), checked_ms, body_verified:true, point_keys를 넣는다. updated_ms, developing, event_status 선택. 예정된 미래 사건은 event_status:scheduled로 구분한다.

자료를 확인한 checked_ms는 실행 선점 이후·결과 기록 이전이어야 한다. 각 material에는 fact_ids, canonical_key, subject_key, kind:news, checked_ms, refresh_after_ms를 준다. 일반 신선도는 최대 24시간, developing:true면 최대 1시간이다. 같은 기사의 추적 URL 차이는 제거하지만 article ID 등 의미 있는 query는 유지한다. 현재 확인 노트에 더 이상 없는 옛 point는 자동 소개에서 제외한다.

claim/result는 검색 예산·외부 작업 상태용 전용 진입점이며 revision을 증가시킨다. 생활·DM 결정을 대신하지 않는다. 이후 새 tick을 읽고 일반 apply는 기존 gate --apply-claimed만 사용한다. 자동 탐색은 실제 Muse 도구의 제한 시간/취소 지원을 확인한 뒤 켠다. 별도 모델/검색 제공자 호출이나 무지연을 이 CLI가 보장하지 않는다.

초기 정책·전체 예제 조각: examples/conversation-policy.example.json, examples/conversation-flow.example.json. 모드 적용 전후의 배포·롤백은 전달 폴더의 DEPLOY.md를 따른다.

## 답장 경량 조회·라우팅 (2026-10-18)

기존 `tick`은 `--view full`이 기본이며 지침 본문·주간 계획·피드를 포함하는 기존 조회를 유지한다. 새 경량 경로는 다음을 사용한다.

```sh
python3 ctl.py --db <DB> tick --session main --view dm --wake-id <wake_id>
python3 ctl.py --db <DB> tick --session main --view dm --known-core-rules-hash <이미_읽은_SHA256> --turn-id <turn_id> --wake-id <wake_id>
python3 ctl.py --db <DB> tick --session main --view life --turn-id <turn_id> --wake-id <wake_id>
```

- dm: core_rules 본문·weekly_plan을 생략한다. 현재 state/activity/appointments, 미처리 messages/jobs, due 의도·열린 의도·후속 공유, 초안 재검토 대상, 불명 발송·채널 간격·정책은 유지한다. 이벤트 payload는 messages와 중복 반환하지 않고 ID·cursor·kind 등 메타데이터를 남긴다.
- life: 지침 본문을 생략하고 생활 상세를 반환한다.
- gate: gate/wait 내부용이다. 본문과 프로필/의도 상세를 생략하고 분류·선점·수면 알림 판단에 필요한 메타데이터를 반환한다. 모델의 답장 문맥으로 쓰지 않는다.
- `core_rules_hash`, `core_rules_path`, `core_rules_changed`로 첫 읽기·변경 읽기를 판단한다. known hash를 보내지 않으면 changed=true다. compact view는 변경됐어도 본문을 자동 첨부하지 않으므로 해당 파일을 읽는다. hash는 소문자 64자리 SHA-256이다.
- `projection.omitted_fields`는 생략 항목이다. null/부재를 실제 기록 부재로 해석하지 않는다. `weekly_plan_present`가 요약을 제공한다. 기존 이벤트 has_more/next_cursor를 유지한다.
- `work_manifest`는 코드가 고른 primary/runbook/view, life_required, action_intent_due_ids, repair, unresolved_job_ids, background_allowed다. DM은 필요한 생활 경계만 함께 처리한다. 이 정보는 읽기/실행 선택이며 apply 권한·Store 가드를 대체하지 않는다.
- DM/life CLI 조회는 `turn_id`를 자동 생성한다. 같은 판단의 재조회·재시도·저장에는 그 ID를 재사용하고 새 판단에는 새 ID를 쓴다. gate `--apply-claimed`의 선택 인자 `--turn-id`가 CLI와 trace에 전달된다. turn/wake ID는 ASCII 문자·숫자·`_:-`, 최대 80자다.
- CLI 요약 로그의 tick 시작/종료·output_bytes/work_route와 gate/CLI apply 시작/종료를 turn_id/wake_id로 연결한다. 대화 본문·토큰은 기록하지 않는다. 이는 tick→적용 구간 계측이며 제공자 내부 추론·대기 시간을 분해하지 않는다.

wait-dm은 DM, due 의도·활동/일정·답장 due, draft_needs_review/draft_stale을 기존 흐름으로 처리한다. timeout에는 lease를 해제한다.

내부 앱용 Slack 기본 주기는 listener backfill 15초 / history_min_interval_ms 15000이다. 기존 자동화 JSON과 DB 값은 자동 덮어쓰지 않는다. `tools/configure_slack_polling.py`는 먼저 preview하고, `--internal-app-confirmed --apply`에서만 기존 신원/활성 플래그를 보존해 두 간격을 바꾸고 owner-only 백업을 남긴다. 기존 공용 budget과 Retry-After는 초기화하지 않는다. listener 재시작은 운영 절차로 별도 수행한다.
# 통합 판단 실행 API — decision_session.py

운영 경량화 `8410896` 위의 선택적 실행 절차다. Store의 기존 apply/의미 가드와 외부 발송 계약은 유지하며 DB schema 변경은 없다. `automation.json`의 기존 db/session/state_dir 및 채널별 transport_config를 사용한다. 상대 파일 경로는 설정 파일 디렉터리 기준이다.

```sh
python3 decision_session.py --config automation.json --wake-id WAKE prepare
python3 decision_session.py --config automation.json --wake-id WAKE prepare --recall @queries.json
python3 decision_session.py --config automation.json --wake-id WAKE commit --turn-id TURN --decision @decision.json
python3 decision_session.py --config automation.json --wake-id WAKE finish --turn-id TURN
python3 decision_session.py --config automation.json --wake-id WAKE close --turn-id TURN
```

- prepare: snapshot, 선택 runbook/변경 CORE_RULES, 최근 대화·파일 hash/신선도/미포함 target, 선택한 recall 여러 건을 반환한다. queries는 검색어 배열들의 배열(최대 8개)이다. `--context-limit`은 6~30, 기본 30개에 더 오래된 미처리 대상을 포함한다(다른 값을 전달할 수 있다). `read-context --limit`도 기본 30이다. DB/file은 원자적 snapshot이 아니며 revision 변화는 prepare_inputs_changed로 재준비한다. 필수 주제별 검색과 의미 판단은 모델의 책임이다.
- commit: 준비한 turn/revision·context coverage·현재 선점 확인 후 Store.apply를 호출한다. 무경고면 같은 DB transaction에서 저장, 경고면 전체 rollback과 `committed:false`, `reason:warning_review_required`, `commit.warning_review`를 반환한다. 검토 후 동일 초안/경고를 허용할 때만 `--warning-review TOKEN`을 전달한다. 토큰은 결정 digest/경고에 묶이며 revision은 기존대로 재검사한다. 새 초안이나 달라진 경고에 토큰을 재사용할 수 없다. enforce 오류는 계속 차단한다.
- commit의 `--conversation-ended`: 모델이 실제 대화를 마무리하기로 판단한 경우만 지정한다. 저장 후 채널별 발송과 조건부 wait까지 처리한다. 기본은 정책에 따른 live 여부이며 새벽 quiet를 변경하지 않는다.
- committed:true 이후 after.next는 prepare(다음 판단), done(종료), finish(후처리 재시도), retry_same_commit(동일 원본으로 저장 후 메타데이터 복구) 중 하나다. 저장 후 실패를 새 답장 생성으로 복구하지 않는다. 선점 상실은 done이며 다른 실행의 상태를 건드리지 않는다.
- finish는 저장된 현재 turn의 후처리만 반복한다. 채널은 저장한 그룹에서 가져오고 실제 전송 설정을 사용한다. mock fallback이 없다. 기존 send-due의 dedupe·unknown·pacing·상태 검사를 유지한다.
- close는 추가 저장할 일이 없을 때 사용한다. 새 DM/필수 생활 판단이면 prepare로 돌아가고, 해결 못 한 발송 unknown은 원장에 남겨 종료한다. background 한 단위 확인을 인계할 수 있다. 가짜 무변경 결정을 저장할 필요가 없다.

세션 상태는 `state_dir/session-<wake_id>.json`에 시작/turn ID·횟수·적용 ID·revision·문맥 ID 범위만 기록한다. 본문·검색어·결정 JSON은 저장하지 않는다. 모든 판단 변경은 gate 잠금 아래 현재 wake/ACK를 확인한다. wait 루프는 소유권 확인/갱신과 짧은 DB 작업에만 잠금을 쓰며 수면/네트워크 대기에는 잠금을 유지하지 않는다. 소유권이 바뀌면 이전 실행은 lease를 지우지 않는다.

정상 도구 반환은 exit 0(경고 검토 필요 포함), revision_conflict 2, stale_claim 10, 기타 입력/검증 오류 1이다. gate trace의 apply_finished는 exit_code 0이 성공 저장, 4가 경고 rollback이다. session_prepare에는 route/revision/recall 개수/elapsed, session_after에는 mode/대기·종료 이유가 남는다. 상세 원문과 도구 stdout은 trace에 넣지 않는다.

기존 CLI와 gate --apply-claimed는 호환용으로 남는다. 기본 Store.apply는 shadow 경고만으로 저장을 금지하지 않는다. 새 래퍼의 opt-in 경고 검토는 `guard_warnings=True`, 확인된 토큰은 `warning_review=TOKEN`으로 전달한다. Store.wait_dm의 선택적 owner_guard는 대기 시작·각 poll의 소유권 확인용 context manager이며 기본 동작은 기존과 같다.

hooks 활성화·생활 저장 계약 분리·VM 외부화는 이 API의 제공 범위가 아니다. 모델 밖 heartbeat는
아래 선택형 wake별 갱신 데몬이 제공한다.

### 판단 선점 수명·거절 초안 복구 (2026-10-20)

automation.json의 선택 항목 `decision_claim`은 `auto_heartbeat`(기본 false), `stale_after_seconds`(기본 900),
`heartbeat_interval_seconds`(30), `max_run_seconds`(1800)를 받는다. 기본 만료 15분은
긴 판단을 위한 임시 완화값이며 실제 최적값으로 측정한 값은 아니다. 유효한 active ACK는
fingerprint 변경보다 우선하며, 만료된 선점은 후속 gate 평가에서 정리하거나 다음 선점으로 교체한다.
ACK/heartbeat/교체/거절 시각과 사유를 trace로 연결한다. 기존 저장·발송 fencing은 유지한다.

- `commit` 오류 시 가능하면 private archive에 원본 초안을 보존하고 CLI 오류에
  `recovery.saved/draft_id`를 반환한다. `prepare.recovery_candidates`는 최대 5개 메타데이터다.
  현재 제출의 `recovery.next`는 `correct_and_retry_commit`(오류 필드·의미 검토 후 같은 준비에 제출),
  `complete_inputs_and_retry_commit`(빠진 읽기 확인 보충), `retry_same_commit`(일시 I/O/busy의 동일 결정 재시도),
  `prepare_and_review_changes`(입력 변경), `inspect_error`(turn/generation/ID·캐시 불일치 확인),
  `stop`(소유권 상실)을 구분한다. 재시도는 기존 검증을 그대로 통과해야 하며 자동 경고 승인이 아니다.
- `recovery-drafts`는 목록, `read-draft --draft-id SHA256`는 원본과 source wake/turn/revision을
  반환한다. 현재 claim 소유자만 읽는다. `can_apply_unchanged:false`,
  `next:prepare_and_review_changes`이며 현재 입력과 대조한 뒤 유효한 부분만 재사용한다.
  기존 expected_revision·경고 검토·receipt·Store 검사를 건너뛰지 않는다.
- archive는 state_dir/decision-drafts, 디렉터리 0700·파일 0600·최대 50개·조회 유효기간 7일이다.
  동일 원본 성공 재시도 시 해당 복사본을 지운다. archive 장애는 원래 저장 결과를 바꾸지 않는다.
  원문은 이 private 파일에만 보존하고 일반 trace에는 digest/식별자/오류 코드만 기록한다.
- auto_heartbeat=true이면 gate --claim이 setsid로 분리된 `claim_heartbeat.py`를 자동 시작한다.
  wake/gate/ACK 소유권과 phase를 감시하고 finished/failed/소유권 상실에 종료한다. 실행 상태를
  조회하는 API가 없어 모델의 생존은 확인하지 못한다. 실행이 조용히 죽으면 상한까지 보호될 수 있다.
- 자동 갱신 선점은 최초 발급 시 `claim_deadline_ms`를 wake와 gate_state에 고정한다.
  max_run_seconds는 최대 1800이며 decision_session.MAX_RUN_MS와 같은 상한이다. 재시작/수동 갱신으로
  연장하지 않는다. 기한에는 ACK failed를 기록한다. 데몬이 죽어도 gate 판단/저장은 이 상한을 검사해
  추가 stale 기간 없이 인계할 수 있다. 모델/플랫폼 작업을 강제 취소하는 기능은 없다.

실제 VM 분리 프로세스 생존·작업 종료 ACK·상한 복구를 검증한 뒤 auto_heartbeat를 켠다.
기존 크론/hook 진입과 모델 선택은 유지한다. 상세 적용·롤백은 [운영 안내](../operations/decision-claim.md)를 따른다.


## 수동 사건 주입 v1

`event-schema`는 DB 없이 기계 판독 JSON Schema를 반환한다. LLM 작성/수동 입력 방법은 [event-format.md](event-format.md), 판단 처리는 [runbooks/world-events.md](runbooks/world-events.md)를 따른다. 별도 해석기·외부 모델 호출은 없다.

`event-import --input @events.json --instruction-ref <설정지시참조> [--dry-run]`은 신뢰된 설정자 입력이다. occurred/scene/candidate를 받고 서버가 source_kind=author/producer_id=operator_cli와 수신 시각을 기록한다. payload의 출처·권한 자칭 필드는 거절한다. dry-run은 원본 DB의 migration도 피하는 복제 DB 검사다. UTF-8 64KiB/1~20건, 사건 ID 최대 120자, 요약 500자, 발췌 2,000자다. 한 배치는 원자적이며 오류 detail의 event_index/event_id/code로 문제 항목을 알린다.

occurred는 현재/과거 사건 원장에 확정하며 known_by에 self가 있으면 같은 트랜잭션에 `world-event:<event_id>` simulated fact를 쓴다. 모르는 사건은 세계 원장에만 두고, 나중의 인지는 별도 전언 사건으로 잇는다. 활동/advanced_to_ms/의도/관계/발송을 자동 변경하지 않는다. 동일 request_id+정규화 문서+instruction_ref는 원 수신 영수증을 반환하며 현재 상태는 inspect로 확인한다. 같은 event_id의 다른 내용은 거절한다. 새 요청 ID로 동일 사건만 재제출해도 기억·세션 revision을 중복 갱신하지 않는다.

`event-inspect --id <ID>`/`event-list [--offset N --limit N]`는 아는 확정 사건만 반환한다. --operator는 후보·미공개 사건·출처·변경 이력 조회용이다(목록 최대 50). `event-cancel --id <미발생ID> --reason ...`은 pending만 취소한다. 확정 사건을 삭제하지 않는다. 로컬 CLI는 신뢰된 운영 경로이며 외부 접근 권한 체계나 HTTP 입력 서버를 추가하지 않았다.

full/life tick의 world_events는 최근 알려진 사건 최대 8개와 현재 검토 가능한 후보/장면 최대 8개, has_more를 포함한다. dm view는 recent만, gate는 사건을 조회하지 않는다. prepare가 순수 DM으로 투영할 때도 pending을 제거한다. 후보는 기존 wake에 동반 노출하며 needs_decision/next_decision/wait/발송 조건을 새로 만들지 않는다. 만료는 조회 즉시 제외하고 maintenance에서 정리한다. 실제 원장 정리가 revision을 바꾸지는 않는다.

apply의 선택적 `world_event_ops`는 action/event_id/reason과 작업별 필드를 받는다. realize는 선택적 occurred_at(생략하면 now)으로 유효 시간/조건을 검사해 원장 소비와 기억을 원자적으로 반영한다. defer는 review_at, reject는 candidate에만 허용한다. scene은 임의 기각하지 않는다. share는 텍스트 job_ids를 연결하며 같은 apply의 새 파트도 가능하다. shares는 실제 dm_jobs 상태를 조회해 confirmed/unknown/부분 전달을 구분하고 20개 초과이면 shares_has_more를 준다. 발송기를 수정하거나 재전송하지 않는다. 명시적으로 잘못 쓴 사건 작업은 기존 apply 전체를 롤백하지만, 작업 자체를 생략한 DM/생활 apply에는 사건 처리 강제가 없다.

scene/candidate는 명시한 not_before/expires_at을 따른다. 정확한 시각의 실행 보장·추가 기상·자동 생성 빈도/비용 정책·생성/외부 공급자 어댑터는 이번 범위가 아니다. 소설을 LLM이 규격에 맞춰 작성한 JSON은 첫 수동 경로로 바로 사용할 수 있다.

### 입력 흐름과 회상 페이지 (2026-10-20)

recall --keywords ... --limit 1..10 --offset 0은 기존 검색/정렬/revision을 유지하며
 offset/limit/has_more/next_offset을 반환한다. Store/일반 CLI/prepare/read-recall의 기본은 모두 10건이다.
read-recall의 --limit/--offset은 일반 회상·--index·--memory에도 적용하며 원문/details를 줄이지 않는다.
현재 topic_state 1건이 필요하면 --limit 1을 명시한다. 마지막 next_offset은 null이다.

48 KiB 자동 모드 선택과 32 KiB 컴포넌트/64회 조회는 자체 권고값이다. 초과만으로 실패하지 않는다.
prepare.input_budget_exceeded와 read의 input_advisory.budget_exceeded/read_count_exceeded로 알린다.
큰 문서·단일 메시지·상태·다중 키 기억은 원형 그대로 반환하고, 명시적 개수/페이지 선택은 가능하다.
manifest의 max_reads는 null이며 recommended_max_reads와 budget_is_advisory를 함께 제공한다.

compact 필수 상태는 required_state_sections이고 state_sections 전체는 아니다.
같은 wake의 실제 문서 receipt는 동일 해시일 때 재준비 후 재제출할 수 있다.
reusable_receipt=true를 표시하며 원래 토큰이 없으면 재조회해야 한다. 상태/문맥 receipt와 새 wake에는
이 예외가 적용되지 않는다. 선택된 대화 변경·새 ID 도착·최종 revision/발송 정합성은 계속 검사한다.

### 독립 소재 생산자 (2026-10-20)

- `topic-producer-claim`: 정책·공유 예산·쿨다운·중복 실행만 검사한다. `claimed:false`는 정상적인 건너뜀이다. 수락 시 run_id/token/started_ms/deadline_ms/max_materials/task를 반환한다. DM·활동·lease/unknown 발송은 거절 조건이 아니며 gate/세션 revision을 변경하지 않는다.
- `topic-producer-result --result @result.json`: run_id/token/status(success|no_candidates|failed|timeout), 선택 facts/materials를 받는다. status=success는 임시 ready이며 공개된 사실이 아니다. deadline 후 결과도 종료 기록은 받되 expired로 제외한다. 토큰·결과 digest는 중복 제출/충돌을 구분한다.
- `topic-producer-inspect --limit 20`: 토큰·본문을 제외한 최근 실행/감사/게시 결과를 조회한다. 전체 이력은 별도 SQLite에 보존한다. 시작/선점/결과/게시의 run_id와 job_id로 연결하며, 미기동은 스케줄러 기록과 대조한다.
- `topic_search_worker.py --config automation.json`: `topic_search.worker_argv`에 연결한 검증된 검색/원문 도구 어댑터를 한 번 실행한다. JSON stdin에 task와 기한을 전달하고 stdout의 status/facts/materials를 받아 임시 적재한다. argv는 shell을 거치지 않는다. 설정 누락·도구 실패·시간 초과는 수집 작업만 종료한다. 토큰은 어댑터나 요약 로그에 전달하지 않는다.

유진의 search_limit_ms는 기본 180000, 허용 범위 1..180000ms다. 저장된 정책은 그대로 읽으므로 기존 값을 늘릴 때 `conversation-configure --settings @examples/topic-search-180s.example.json`으로 그 필드만 병합한다. mode/검색 플래그/횟수·간격을 덮어쓰지 않는다. claim의 deadline_ms가 worker와 검색 도구에 전달하는 총 수집 기한이며, 판단/답장 생성기의 시간 제한을 바꾸지는 않는다.

별도 원장은 `<db 경로>.topic-producer.db`이며 기존 topic_search_runs와 공유 파일 선점으로 예산/동시 실행을 합친다. search 정책의 새 강제 활성화나 별도 생산자 필수 플래그는 없다. 기존 두 search 플래그를 그대로 쓴다. 작업 스케줄은 별도 연결한다.

facts/materials 게시 API는 외부 CLI로 노출하지 않는다. decision_session의 기존 owner lock 아래 prepare 직전에만 선택 결과를 게시한다. 게시 영수증 `topic_producer_imports`와 사실·소재를 같은 주 DB 트랜잭션으로 저장하여, 별도 원장 ack 실패 재시도도 중복 반영하지 않는다. 게시에 성공했을 때만 세션 revision이 바뀐다. 한 후보 실패는 SAVEPOINT로 격리하고 DB 잠금은 기다리지 않고 다음 prepare로 넘긴다. 후보가 하나도 없으면 답장과 생활은 그대로 진행한다.

### DM 입력 묶음·준비 보존 (2026-10-21)

- `prepare --document-receipts @docs.json`: 같은 wake에서 실제 전달받은 문서 토큰 배열을 받는다. 동일 hash 문서는 `reused_documents`에 기록하고 bundled 본문에서는 생략한다. bundled 응답의 `document_receipts`는 문서 이름→토큰이다. 다른 wake 토큰은 거절하고 변경된 문서는 재전달한다.
- `read-bundle --turn-id ... --input-id ... [--document-receipts @docs.json] [--input-receipts @receipts.json]`: compact 필수 상태, 필수 문서, 준비된 신선한 문맥의 전체 선택 범위, prepare에서 요청한 recall을 한 응답의 `components`로 반환한다. 완전히 동일한 recall selector는 한 번 조회한다. `input_receipts`를 commit에 제출한다. 신선하지 않은 채널은 `unavailable_contexts`에 보고하고 문맥 receipt를 만들지 않는다. 개별 read-*도 유지한다.
- 선택적 `--input-receipts`는 같은 input_id에서 실제 읽고 보유한 확인값 배열이다. 이미 확인한 상태 절·문서·대화 ID·동일 recall selector의 본문을 생략하며 `reused_input_count`를 반환한다. 부분 조회된 상태 절은 전체 절을 보충하고, 좁은 recall 결과는 넓은 기본 조회를 대신하지 않는다. 반환 `input_receipts`는 기존 값과 새 값의 합집합이다. 다른 준비의 상태/문맥/검색이나 위조 토큰은 거절하며 신선도 검사는 유지한다.
- 묶음 크기를 이유로 내용을 자르거나 거절하지 않는다. 실제 도구 전달 한계는 별도다. 묶음 도중 revision/문맥 변경은 실패하며 일부 발급된 토큰을 로그에서 복원해서는 안 된다.
- prepare는 모든 준비가 성공한 후 새 세대를 발행한다. 실패하면 이전 상태 파일이 유지된다. 기존 준비의 실제 유효성은 원래 소유권/revision/문맥 검사로 판단한다.
- 저장된 현재 턴의 후처리가 남아 있으면 prepare는 `finish_required`와 `recovery={next:finish,turn_id,committed:true}`로 거절한다. 해당 턴의 finish를 재개하고 반환된 next를 따른다. 이미 저장된 답장을 재생성하거나 준비 상태를 덮어쓰지 않는다.
- 문서 토큰은 같은 wake의 턴 사이에서 재사용한다. 상태·문맥 토큰은 재준비/새 턴에서 재사용하지 않는다. bundled 준비에서 전달/재사용한 문서도 commit 직전에 hash를 확인한다.

### 선택형 답장 문장 생성 (2026-10-21)

prepare.constraints.reply_generator가 enabled/failure_policy/command를 반환한다.
`generate-reply --turn-id TURN --request @request.json [--input-receipts @receipts.json]`은
원문 문맥과 Muse가 선택한 상황/기억을 고정 프롬프트 뒤에 붙여 생성한다.
성공은 parts/photo/note/generator_id/usage/elapsed_ms, 재사용은 reused=true를 반환한다.
실패는 ok=false/reason/next=defer를 반환한다. failure_policy는 defer만 허용한다.
꺼진 상태는 reason=reply_generator_disabled/next=muse(기존 작성 경로)다.
전송 단계 실패는 같은 요청 재호출 시 한 번 더 생성한다. 그 밖의 실패는 재사용 결과를 반환한다.
소유권·새 수신·준비 변경은 기존 session 오류다. 실패 결과를 답장 성공으로 취급하지 않는다.
켜져 있으면 generator_id 없는 새 reply/initiate commit은 generator_required로 거절한다.

reply/initiate DM 객체의 generator_id는 현재 wake/turn/준비 세대/채널/대상과 연결한
저장 결과를 참조한다. 텍스트 parts를 생략하면 commit이 복사하고, 명시하면 같아야 한다.
Store 검증은 유지한다.
최상위 generator_id 필드는 추가하지 않는다. ID는 기존 결정 사실에 DM 필드로 보존된다.

기본 꺼짐이며 운영자가 작성한 추가 작성 규칙 파일을 rules_file로 지정한다.
켤 때는 OpenAI 호환 chat completions 주소를 `endpoint`에 반드시 지정한다(예: `https://api.example.com/v1/chat/completions`). 내장된 기본 제공자는 없으며, 주소가 없거나 잘못되면 `model_endpoint`·`invalid_model_endpoint`로 시작 전에 거절한다.
고정 prefix는 profile → 답장 규칙 → 추가 작성 규칙이고 동적 자료는 다음 메시지다.

## 대면 대화 상태

실제 전달 채널은 그대로다. tick.interaction은 mode(remote|in_person), active, available과
활성 약속 ID·channel·장소·이동 목적지·시작·검토 시각을 제공한다. 약속 시각만으로 활성화하지 않는다.
약속에 연결한 활동이 약속 장소에 있으면 remote 상태의 candidate_appointment_id로 시작 검토를 안내한다.
후보는 함께 있다는 증거가 아니다. with_user=false로 명시한 다른 사람의 약속은 후보로 안내하지 않는다.
새 필드는 선택형이며 일반 DM 결정에는 쓰지 않는다. full/compact-v1 모두 같은 in_person_ops를 쓴다.

```json
{"in_person_ops":[{"action":"start","appointment_id":"meetup-id","channel":"slack",
  "evidence_message_ids":["실제로 합류를 확인한 수신 ID"],"reason":"현재 대화에서 실제 합류를 확인함"}]}
```

start는 confirmed 약속, 그 장소에서 진행 중인 연결 활동(details.appointment_id), 읽은 실제 incoming
근거가 필요하다. 활동·대면 상태·답장을 같은 apply에 저장할 수 있다. 사용자 수락의 의미는 Muse가 판단한다.
연결 활동 없이 약속만 있으면 대면으로 전환되지 않는다. 시작은 수면/불가 상태를 자동으로 해제하지 않는다.

- 헤어짐: `{"in_person_ops":[{"action":"end","reason":"실제로 작별하고 떠남"}]}`.
  대면 상태를 ended로 하고 연결 약속을 completed로 정리한다. 관계 점수는 자동 변경하지 않는다.
- 계속 함께 있음: `{"in_person_ops":[{"action":"review_later","review_at_ms":1792841400000,"reason":"다음 실제 경계에서 확인"}]}`.
  미래 검토 시각을 지정하며 계획 종료 시각만으로 상태를 끊지 않는다. 확정된 연장은 기존 appointments 갱신도 가능하다.
- 이동: 기존 activities 전이와 이동 연속성을 사용한다. 실제 대면이 활성화된 뒤 그 약속에 연결한 활동은
  처음 만난 장소 밖으로 이동할 수 있다. 다른 고정 약속의 장소 검사와 이동 기록 요구는 그대로다.

tick.appointment_lifecycle에는 review_ids/preparations/next_boundary_ms가 있다. 끝난 confirmed 약속은
review_ids와 appointments에서 계속 보이고 appointment_review_due로 검토한다. 진행 중인 연결 활동이
있으면 appointment_due를 반복하지 않는다. 약속 details의 선택 prepare_at_ms/depart_at_ms로만 준비 신호를
만들며, 준비 활동의 appointment_phase=prepare와 실제 목적지로 향하는 연결 이동을 확인한다.
끝났지만 확인을 미룰 일반 약속은 details.review_at_ms를 미래로 지정할 수 있다.

대면 중 focused 활동도 해당 채널은 live가 된다. unavailable/수면은 유지한다. 원격↔대면 경계에서
이전 미발송 그룹은 draft_stale로 검토하며 Dispatcher도 검토 전 발송을 보류한다. 현재 맥락에 맞으면
revalidate_group으로 승인하고, 맞지 않으면 cancel_group으로 정리한다. initiate 전체를 금지하지 않는다.
세부 말투·행동·종료 지침은 design/in-person.md를 따른다.

## 대화 맥락 단서와 선택 기억 전달

prepare.memory_cues[채널]은 짧은 conversation_frame과 최신 topic_state의 정확한 key/fact_id를
제공한다. bundled/compact 모두 준비 응답에서 바로 보이며 별도 조회·필수 receipt는 추가하지 않는다.
목록은 최신순 최대 4개이고 topic_count/topics_has_more로 생략을 표시한다. 의미 검색 결과가 아니며
목록에 없다고 기억이 없다는 뜻은 아니다. topic_state와 원문은 기존 recall/read-recall로 필요할 때 읽는다.
recent_context_included/available은 이번 최근 대화 창과 캐시에 있는 범위다. available은 전체 대화 이력이 아니다.

현재 대화의 장난·전제는 선택형 사실로 남길 수 있다. 의미가 생기거나 바뀔 때만 갱신하고 같은 주제를
매 맞장구마다 다시 요약하지 않는다. 채널별 key는 아래처럼 쓰며 id는 새 버전마다 다르게 정한다.

```json
{"facts":[{"id":"conversation-slack-01","source":"simulated","occurred_ms":1792700000000,
 "summary":"topic_state:conversation:slack 현재 대화의 전제와 이어갈 맥락",
 "details":{"channel":"slack","frame":"실제로 읽은 대화의 전제·말투·서로 받아친 요지를 짧게 기록한다.",
   "callbacks":["나중에 다시 가리킬 수 있는 실제 표현과 그때의 의미"],"open_threads":[],
   "evidence_message_ids":["실제로 읽은 수신 ID"],"previous_id":null}}]}
```

이 문장은 모델이 실제 대화에 맞춰 쓴 요약이다. 사실·감정·영구 선호·관계 단계의 증거가 아니다.
사용자가 진지하게 전환하면 현재 발언이 우선이며, 약속은 기존 action_intents로 따로 잇는다.
아직 pending/unknown인 새 답장을 이미 주고받은 말로 기록하지 않는다. 현재 맥락의 근거는 읽은 수신과
확정된 지난 발언이다. 주제/놀이가 끝나면 새 버전에서 frame/callbacks를 실제 상태에 맞게 정리할 수 있다.

단서는 전체 두 채널 기준 약 3 KiB 예산이며, frame은 약 1 KiB 안에서 필드 단위로 전달한다.
필드 내용은 중간에서 자르지 않는다. omitted_fields 또는 frame_omitted_for_size면 원문을 조회한다.
큰 기존 기록이나 미작성 frame은 답장을 거절하거나 새 기억을 자동 생성하는 사유가 아니다.
memory_mode는 기존 DB 설정이다. off/shadow를 바꾸거나 shadow 기억을 자동으로 주입하지 않는다.

생성기 요청은 기존 memories 문자열 외에 선택형 memory_fact_ids 배열을 받을 수 있다.
prepare --recall의 bundled 원문, compact의 read-recall/read-memory-context/read-state memory_context에서
이번 준비에 실제 읽은 ID만 선택한다. compact에서는 해당 receipt도 generate-reply에 전달한다.
코드는 선택한 원문을 동적 memory_sources에 넣고 source·시각·구조화 기억의 상태/범위를 유지한다.
같은 채널의 제공된 conversation_frame도 동적 자료로 자동 연결한다. 고정 prefix는 변경하지 않는다.
metadata 목록만 본 ID, 이전 준비의 receipt, 다른 채널의 구조화 기억, 철회된 기억은 이 경로로 전달하지 않는다.
과거 철회 이력은 기존 history 조회 후 의미·시점을 구별해 memories 문자열로 설명한다.
선택 없이도 기존 generate/commit 흐름은 그대로이며 추가 조회를 의무로 만들지 않는다.
