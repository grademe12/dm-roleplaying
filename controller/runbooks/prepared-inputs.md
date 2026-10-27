# 판단 입력 분리 조회

출력 compact-v1은 입력의 bundled/compact와 독립적이다. 기존 decision_session 경로에서
prepare --decision-format compact-v1을 선택하면 commit에 동일 decision_generation도 제출한다.
입력 receipt 검증은 그대로이며 [출력·복구 계약](session-compact.md)을 따른다. 파일 실행기는 필요하지 않다.

입력 분리는 출력량과 출처를 관리한다. 자체 예산을 넘었다는 이유로 정상 입력 조회나 답장을 차단하지 않는다.
플랫폼 검사 거절을 자료 변형·분할 재시도로 우회하는 기능은 아니다.

## 준비·조회

기본 prepare는 auto다. UTF-8 JSON 약 48 KiB를 기준으로 작은 입력은 bundled, 큰 입력은 compact
목록으로 나눠 읽는다. 이 값은 모드 선택/관측 기준이다. 명시적 --mode bundled나 compact 목록이
권고 크기를 넘어도 크기만으로 실패하지 않으며 input_budget_exceeded로 알린다. 본문은 잘라내지 않는다.

compact 명령 앞에는 기존 `python3 decision_session.py --config automation.json --wake-id <wake_id>`를 붙인다.

정상 compact 입력은 다음 한 호출로 묶는다. 요청했던 recall의 완전히 같은 검색어 배열은
한 번만 조회한다. 상태는 required_state_sections만 포함하며 준비된 문맥은 필요한 페이지를
연결한다. 신선하지 않은 채널은 unavailable_contexts에 표시하고 읽었다고 인정하지 않는다.

```sh
read-bundle --turn-id <turn_id> --input-id <input_id>
```

components를 실제로 읽고 input_receipts 배열을 commit에 제출한다. document_receipts의 토큰
값 배열을 다음 read-bundle 또는 prepare의 --document-receipts @docs.json에 전달하면
같은 wake에서 읽은 동일 문서 본문을 생략한다. 변경된 문서는 다시 반환한다.
토큰이 없거나 실행 컨텍스트를 잃었으면 재사용하지 않는다. 전체 응답 크기는 권고 초과로
표시하며 잘라내지 않는다. 기존 개별 조회는 선택 자료/추가 기억/사전에 선택한 페이지 조회에 쓴다.

같은 준비에서 실제 읽고 계속 보유한 입력은 다음과 같이 재사용하며 빠진 자료만 보충한다.

```sh
read-bundle --turn-id <turn_id> --input-id <input_id> --input-receipts @receipts.json
```

`components`에는 미확인 상태 절·문서·대화 메시지·검색만 포함한다. 상태 목록의 일부 페이지만
읽었다면 그 절은 다시 반환한다. `input_receipts`는 기존 확인값과 새 확인값을 합친 배열이며 이를
commit에 제출한다. 전부 보유한 경우 components가 비어 있어도 정상이다. 이 경로도 소유권·revision·
문서 해시·선택 대화 변경을 검사한다. 서버가 발급했다는 이유만으로 receipt를 자동 회수하지 않는다.
입력 유실·미전달·컨텍스트 유실이면 재사용하지 않으며, 새 prepare 뒤에는 상태·대화·검색 확인값을
다시 받아야 한다. 문서만 기존 --document-receipts 계약으로 같은 wake에서 재사용할 수 있다.

```sh
read-state --turn-id <turn_id> --input-id <input_id>
read-document --turn-id <turn_id> --input-id <input_id> --name CORE_RULES.md
read-document --turn-id <turn_id> --input-id <input_id> --name runbooks/dm.md
read-context --turn-id <turn_id> --input-id <input_id> --channel slack --offset 0 --limit 30
read-recall --turn-id <turn_id> --input-id <input_id> --keywords topic_state:overwatch hobby_profile:overwatch
```

문서·채널은 manifest에 맞춘다. 조회는 DB를 변경하지 않으며 소유권과 준비 revision을 확인한다.
기본 최근 문맥은 30개(`--context-limit`로 6~30 지정 가능)지만 그보다 앞선 미처리 대상도 포함한다. context offset은 available_message_ids의
앞에서부터 세며 특정 ID는 --message-id를 반복한다. 결과는 원본 대화 순서다.

read-state는 준비의 DM/life view를 유지한다. 전체를 한 번에 읽거나 필요한 --section을 고른다.
필수 범위는 **input_manifest.required_state_sections**다. state_sections는 읽을 수 있는 전체 목록이고,
그 안의 피드 backlog 같은 선택 자료를 전부 읽어야 하는 것은 아니다. life_required에 필요한
일정·의도·생활 상태와 활성 기억/정책 등은 필수 목록에 포함된다. 일정(appointments)은 7일 안의 확정 일정과 24시간 안의 제안 일정이다(최대 20개). 읽지 않은 배경 작업을 완료로 표시하지 않는다.
목록은 한 항목씩 --offset/--limit로 이어 읽을 수 있으며 필수 목록의 실제 범위는 모두 확인한다.

```sh
read-state --turn-id <turn_id> --input-id <input_id> --section state --section activity --section messages
read-state --turn-id <turn_id> --input-id <input_id> --section jobs --offset 0 --limit 10
```

문서 경로는 CORE_RULES.md·API.md·runbooks/·design/ Markdown만 허용한다. 상위 경로·외부 symlink는 거절한다.
문서는 길어도 원문 전체와 receipt를 반환한다. 관련 문서만 읽고 주제별 기존 정책을 적용한다.

## 확인값과 재준비

각 성공 응답의 content를 읽고 receipt를 JSON 배열로 모아 commit에 --input-receipts로 제출한다.
서버는 확인값 해시와 범위만 저장하며, 조회 완료 로그로 실제 읽기를 위장하지 않는다.
필수 상태·문서·대화의 확인값이 없거나 조작된 경우에는 계속 저장을 거절한다.

같은 wake에서 prepare를 다시 하면 **동일 해시의 문서 확인값만** 재사용할 수 있다.
manifest 문서의 reusable_receipt=true는 재사용 가능한 확인값이 서버에 있다는 뜻이다.
실제로 받은 기존 문서 receipt를 다시 제출한다. 값을 잃었으면 문서를 다시 읽고, 해시만으로 대신하지 않는다.
상태·문맥·기억 조회의 옛 확인값은 재사용하지 않는다. 같은 wake의 새 턴에는 문서 확인값을 유지하며, 새 wake에서는 재사용할 수 없다.
문서가 바뀌면 기존 확인값은 통하지 않는다. optional 문서도 제출했다면 내용 변경을 검사한다.

reply/initiate/revalidate_group은 해당 채널의 최근 context_limit개, 실제 대상,
검토에 인용한 ID를 모두 확인한다. 캐시가 원래 비어 있는 정상 initiate에 가짜 문맥은 필요 없다.
DB 메시지 조회가 양방향 최근 대화 조회를 대신하지 않는다.

실제 선택된 메시지 내용·순서·삭제와 새 ID의 도착은 재준비를 요구한다. 선택 범위 밖의 기존 옛 캐시
내용 변경이나 fetched_ms/헬스 갱신만으로 준비를 버리지 않는다. 캐시 신선도와 Store 최종 revision·
발송 직전 stale 검사는 유지한다. DB revision이 바뀌면 상태를 새로 준비하고 같은 wake의 동일 해시 문서만 재사용한다.

## 크기·횟수와 기억

32 KiB 컴포넌트와 64회 조회는 **권고/관측값**이다. input_advisory.budget_exceeded 또는
read_count_exceeded를 알리지만 넘었다고 읽기·receipt 발급·commit을 중단하지 않는다.
API 문서 한 개·긴 메시지 한 개도 원형대로 읽을 수 있다. 실제 도구/플랫폼 오류는 별도로 보고한다.

일반/다중 키/topic_state 회상은 모두 기존 기본 10건을 유지한다. --limit 1..10과 --offset으로
필요한 개수와 이력을 고를 수 있다. 현재 상태만 필요하면 --limit 1을 명시하고, 과거 변화나
옛 약속이 중요하면 이력/사건도 조회한다. --index·--memory도 같은 명시적 limit를 받는다.
응답의 has_more/next_offset은 남은 이력이고 전부 읽을 의무는 아니다. 내용과 details는 줄이지 않는다.

```sh
read-recall --turn-id <turn_id> --input-id <input_id> --keywords topic_state:overwatch --limit 1
read-recall --turn-id <turn_id> --input-id <input_id> --keywords topic_state:overwatch --limit 2 --offset 2
```

기억 채널/scope·철회 상태, warning_review, 최종 하드 검사, 저장 후 finish/wait/발송과
저장 완료 결정의 동일 ID/내용 replay 계약은 그대로다. 이미 저장된 결정을 복구할 때 새 확인값은 요구하지 않는다.

prepare 실패는 이전 준비를 지우지 않는다. 성공한 새 준비가 발행된 뒤에만 이전 상태·문맥 확인값이 무효화된다. 보존된 준비에도 원래 revision/문맥/소유권 검사는 적용된다.
