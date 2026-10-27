# 훅 워커의 고정 시간 예산

## 확인된 제약

운영 회신에서 event_hook 워커의 created_at→worker_timeout이 약 600초로 확인됐다.
이 종료 기록은 Muse 내부 DB `runtime.execute_resolve_runs`에 있고 로컬 파일/API로 제공되지 않는다.
따라서 코드가 해당 DB를 읽어 모델 생존이나 종료를 즉시 감지한다고 가정하지 않는다.

이번 선택 기능은 **모델 기동 전에 로컬 훅이 요청한 시각**을 기록해 보수적인 기한을 만든다.
실제 런타임 생성은 그 뒤이므로 기동 대기·콜드 스타트도 예산에 포함된다. 해당 시각은
런타임 created_at의 실측값이 아니다. 600초 자체와 여유 시간은 운영 정책에 맞춰 설정한다.

## 적용 경계

- `worker_execution.enabled=false`가 기본이다. 기존 decision_session 진입과 실행기 롤백을 유지한다.
- 예시는 [worker-execution.example.json](worker-execution.example.json)이다. 기존 설정의 해당 항목만 병합한다.
- 실제 운영 DB·토큰·transport·수신/발송 이력을 예제 설정으로 교체하지 않는다.
- gate/wake_hook/worker_budget/decision_session/dispatch/Store를 같은 소스로 맞춘다.
- 이미 진행 중인 wake는 끝내거나 **종료가 확인된 해당 wake**만 기존 ACK failed로 인계한 뒤 바꾼다.
  살아 있는 실행의 기한을 파일 편집으로 줄이거나 늘리지 않는다.

## 훅의 기동·선점

1. 기존 wake_hook이 gate --check로 작업/재시도 자격과 debounce를 확인한다.
2. enabled=true이면 wake 직전에 state_dir/worker-invocations/ID.json을 만든다.
   기동 요청 시각과 요청 시각+timeout_seconds를 저장한다. 훅 dry-run은 ticket을 만들지 않는다.
3. 기존 wake payload에 `controller_invocation_id`, `worker_requested_ms`, `worker_deadline_ms`가 추가된다.
   기존 셸은 JSON payload를 그대로 넘기므로 별도 비밀값이나 새 runtime API가 필요 없다.
4. 등록 워커 프롬프트에서 **첫 도구 명령**으로 다음을 실행한다.

```sh
python3 gate.py --config automation.json --claim --invocation-id CONTROLLER_INVOCATION_ID
```

ID는 해당 런타임 기동 이벤트 payload의 값이다. DM 원문이나 모델이 만든 시각/ID를 사용하지 않는다.
코드는 로컬 ticket을 대조하고 한 claim에만 소비한다. 기한은 claim 시점에 다시 시작하지 않는다.
이미 선점한 워커에서 실행기를 쓸 때는 같은 wake_id를 넘기며 다시 claim하지 않는다.

## 크론과 인계

크론에는 로컬 훅이 만든 ID가 없을 수 있다. 기본 `require_invocation=false`에서는
claim 시점+timeout_seconds를 `worker_entry_fallback`으로 **구분해서** 제한한다.
이는 크론의 백업 기능을 유지하지만 생성→claim 지연을 소급할 수 없으므로, 실제 워커 종료 후
그 지연만큼 선점이 남을 수 있다. 정확한 startup 포함 기한은 훅 ticket 경로만 제공한다.
훅에서 이 fallback이 보이면 프롬프트 배선을 고쳐야 한다. 고정 기한을 런타임 created_at이라고 보고하지 않는다.

`require_invocation=true`는 ID 없는 claim을 거절한다. 이 설정을 크론과 공유하면 크론도 거절되므로
훅 전용 설정/진입을 운영자가 명시적으로 구성한 경우에만 사용한다.

## 공통 기한

- claim_deadline은 기존 max_run_seconds와 ticket 기한 중 빠른 값이다. 자동 heartbeat가 꺼져 있어도 고정된다.
- 자동 heartbeat는 그 기한을 읽으며 끝난다. 재시작·새 수신·수동 갱신은 기한을 연장하지 않는다.
- 남은 작업 시간은 세션 상한·claim 기한·ticket 기한의 최솟값에서 finish_reserve_seconds를 뺀 값이다.
- prepare와 추가 입력 패킷에는 남은 시간이 표시된다. 준비할 시간이 없으면 빈 결정을 저장하지 않고 인계한다.
- send_due는 다음 파트의 transport timeout과 파트 사이 간격이 예산에 들어갈 때만 시작한다.
  시간 부족은 pending 큐를 보존하며 원래 폴러가 처리한다. submitted/unknown의 재발송 정책은 그대로다.
- wait_dm은 남은 시간만 기다린다. 대기 lease와 새 수신 뒤 composing 유예도 예산을 넘지 않는다.
- 인계는 wait lease만 해제하고 ACK를 기록한다. 대화 종료 발언·생활 완료·처리 완료 사건을 만들지 않는다.
- 필수 판단을 저장하지 못한 예산 소진은 failed, 저장 후 인계/비긴급 종료는 finished다.
  기존 제한된 재시도·retry_exhausted/복구 대기를 유지해 무제한 즉시 재기동을 하지 않는다.

## 운영 검증

테스트 명령: `python3 tools/run_live_tests.py --suite core`.
작업별 합성 재현: `python3 2026-10-21/runtime-budget-recovery/probe.py`.

실제 도입은 기존 decision_session 경로에 시간 예산만 먼저 적용한다. 실행기를 기본으로 다시 켜지 않는다.
현재 운영 설정의 worker_execution을 켜고 훅 프롬프트의 ID 전달을 함께 반영한 뒤 **새 wake**로 확인한다.
claim의 `worker_budget_basis=local_hook_request`와 요청→선점→기한의 관계를 확인한다.
169초 늦게 claim했다면 600초를 다시 주는 것이 아니라, 요청 시각부터 남은 431초만 남아야 한다.
종료 여유가 20초면 실제 남은 작업 시간은 그보다 20초 짧다.

실제 runtime worker_timeout/취소 기록과 wake의 deadline을 운영 측 DB에서 대조한다.
원래 기한 뒤 keeper 갱신·옛 wake 저장이 차단되고 후속 claim이 가능한지 본다.
재시도 상한·훅 debounce·기존 폴러 주기는 그대로여서 항상 다음 15초에 기동한다고 보장하지 않는다.

시간 예산은 큰 모델 작업이 600초 안에 끝나도록 보장하지 않는다. 입력 경량화는 별도 비교하고,
여전히 반복해서 잘리면 판단 단위/생활 결합을 후속으로 분리한다. timeout을 늘려 해결했다고 보고하지 않는다.
명시적 런타임 종료 콜백/조회 인터페이스가 생기면, run_id↔로컬 invocation↔wake를 대조한 뒤
해당 wake만 failed로 넘기는 후속 연동을 만들 수 있다. 현재는 그 연결을 구현·배포했다고 주장하지 않는다.

## 롤백·보관

새 기동이 없는 경계에서 worker_execution.enabled=false와 워커의 ID 전달을 함께 원복한다.
기발급 ticket/claim의 절대 기한은 연장하지 않는다. 운영 DB·confirmed/unknown 이력은 복원하지 않는다.
ticket은 state_dir의 비공개 파일이며 invocation ID·시각·claimed wake만 포함한다.
완료/기한 경과가 확인된 오래된 ticket은 운영 보관 정책에 따라 정리하며 Git에 넣지 않는다.
