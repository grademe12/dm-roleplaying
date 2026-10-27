# 판단 실행의 선점 수명과 복구

2026-10-21 후속: 선택형 [워커 시간 예산](worker-budget.md)은 로컬 훅의 기동 요청부터
제한 시간을 고정해 이 문서의 1800초 상한보다 먼저 claim/heartbeat/대기를 종료한다.
실행기 기본 진입 롤백은 유지한다. 런타임 종료 DB를 읽거나 모델 사망을 즉시 감지하는 변경은 아니다.

유진 기준 구현이다. 수정·다른 인물에는 별도 검토 후 필요한 부분만 적용한다.
설정 조각은 [decision-claim.example.json](decision-claim.example.json)이며 기존 automation.json에
`decision_claim` 항목만 병합한다. 실제 DB·경로·인증·채널·정책 설정을 예제로 덮어쓰지 않는다.

## 먼저 적용할 단계: 기존 크론 유지

- `gate.py`, `decision_session.py`, 새 `decision_drafts.py`와 `claim_heartbeat.py`를 같은 전달로 반영한다.
- `stale_after_seconds` 기본값은 기존 180초에서 임시 900초로 늘렸다. 900초는 실측 최적값이
  아니다. 실제 heartbeat 공백·판단 시간·장애 복구 지연을 보고 조정한다. 180초로 명시해
  이전 복구 지연을 유지할 수 있지만 긴 판단의 선점 상실도 다시 발생할 수 있다.
- 새 입력보다 유효한 started ACK를 먼저 보호한다. 새 DM은 평소처럼 DB에 적재하며,
  현재 실행의 다음 prepare/finish 또는 종료 후 다음 gate가 처리한다. 입력을 지우거나
  처리 완료로 표시하지 않는다. 판단할 일이 잠깐 없어져도 유효한 실행의 gate_state는 지우지 않는다.
- `claim_replaced`에는 교체 사유·이전 wake·기존 ACK 시각·heartbeat 경과시간·설정된 만료값을 남긴다.
  `heartbeat`는 실제 갱신 시각과 이전 시각·source(manual/session/daemon)를 남긴다.
  `claim_rejected`·`apply_rejected`·`session_action_failed`로 소유권 상실과 저장/입력 실패를 구분한다.
  거절된 legacy `gate --apply-claimed`도 기록하며, 실행된 legacy apply는 CLI JSON의 reason을 기록한다.
- auto_heartbeat가 꺼져 있다면 기존 긴 작업 전후 `gate --heartbeat`는 유지한다.

heartbeat의 생존 신호는 판단이 올바르게 진척되고 있다는 증명이 아니다. timeout을 늘리면
중단된 실행의 복구도 늦어진다. `max_run_seconds`는 아래 자동 갱신 선점의 상한이다. 모델 실행을 취소하는 기능은 없다.

선점·상한 검사는 gate/session 경유 저장에 적용한다. 구형 직접 ctl apply의 소유권
계약을 이번 작업에서 통일하지는 않았다. 운영의 직접 쓰기 진입점은 별도로 대조한다.

## 거절된 초안 재사용

정상 `decision_session commit`이 소유권·revision·검증·I/O 오류로 실패하면 가능한 경우 원래
결정을 `state_dir/decision-drafts/<sha256>.json`에 저장한다. 디렉터리 0700·파일 0600이며
대화 본문을 포함하므로 Git·공용 진단 로그·배포 자료에서 제외한다. 실제로 commit을 호출하기
전에 런타임이 죽은 경우의 모델 내부 추론이나 미작성 결정은 복원할 수 없다.

오류 CLI 응답의 `recovery.saved/draft_id`와 이후 prepare의 `recovery_candidates`에서 찾는다.
새 소유자는 다음 명령으로 목록·원본을 읽을 수 있다. 소유권을 잃은 실행은 자기 초안을
보존할 수 있지만 새 선점을 갱신하거나 archive 읽기/저장 권한을 얻지는 않는다.

```sh
python3 decision_session.py --config automation.json --wake-id WAKE recovery-drafts
python3 decision_session.py --config automation.json --wake-id WAKE read-draft --draft-id SHA256
```

초안은 검토 참고자료다. 현재 prepare와 새 수신·상태·활동·문서·문맥을 대조해 유효한 부분만
재사용하고 변경된 부분을 다시 판단한다. expected_revision만 바꿔 자동 재시도하거나 옛
warning-review/상태·문맥 receipt를 재사용하지 않는다. 기존 문서 receipt의 동일 턴 재사용
계약은 그대로다. 신규/변경 결정은 새 decision_id를 사용하고 기존 Store 검사를 모두 거친다.
저장 여부가 불확실하면 기존 decision_id의 멱등 재시도/원장을 확인하며 새 ID로 중복 실행하지 않는다.

최대 50개를 보존하며 7일 지난 초안은 조회에서 제외한다. 새 저장 때 오래된 파일·초과분을
정리한다. 동일 원본의 저장 성공 재시도는 해당 복사본을 제거한다. 다른 내용으로 수정해 저장한
초안과 앞선 복사본의 대응은 자동 추정하지 않으므로 목록에 남을 수 있다. archive 쓰기 실패는
원래 저장 오류를 바꾸지 않으며 성공 저장을 실패로 바꾸지도 않는다.

## 선택형 wake별 자동 heartbeat

Muse의 실행 시작·종료·취소를 외부 프로세스가 조회하는 인터페이스는 없다는 운영 회신에 맞춘 구성이다.
`decision_claim.auto_heartbeat=true`이면 기존 `gate --claim`이 `claim_heartbeat.py`를 자동으로 시작한다.
런북에서 모델이 데몬을 실행하거나 판단 중 heartbeat 호출을 기억할 필요는 없다.
기존 크론·hook과 prepare/commit 절차를 유지한다. 기본값 false이며 운영 설정은 별도로 켠다.

- Linux fork/setsid로 분리하고 stdin/stdout/stderr를 닫는다. 부모 gate 명령이 끝나도 갱신한다.
  시작 준비 확인 후에만 claim 성공을 반환한다. 시작 실패면 ACK failed와 진단을 기록하고 exit 1이다.
- wake별 OS 잠금으로 중복 데몬을 막는다. wake/gate/ACK ID가 일치하고 phase=started일 때만
  기본 30초마다 갱신한다. 평소 감시는 1초이며 gate/DB 잠금을 긴 대기 중 유지하지 않는다.
- ACK finished/failed, 소유권 상실, 최초 발급 시 고정한 claim_deadline_ms에서 종료한다.
  새 입력·수동 heartbeat·데몬 재시작·설정 변경은 원래 기한을 늘리지 않는다.
- max_run_seconds 기본 1800초는 decision_session.MAX_RUN_MS와 같은 최대 30분이다.
  더 짧게 설정할 수 있다. 기한에는 ACK failed(reason=claim_deadline)를 기록하고 종료한다.
  gate 선점/저장도 절대 기한을 확인하므로 데몬이 죽었어도 마지막 heartbeat 뒤 추가 15분을
  기다리지 않고 후속 선점이 인계할 수 있다. 데몬의 단조 시계 상한도 있어 실행 중 벽시계가
  멈추거나 뒤로 움직여도 해당 데몬이 무기한 유지되지는 않는다.
- heartbeat는 **저장소 선점 유지**다. 모델 생존·진척·실제 플랫폼 취소를 확인하는 신호가 아니다.
  모델이 흔적 없이 죽으면 데몬이 원래 기한까지 계속 갱신할 수 있다. 이 경우 장애 복구는
  최대 30분까지 지연될 수 있다. stale 기간만 줄여도 살아 있는 데몬의 갱신 때문에 조기 복구되지는 않는다.
- 기한 뒤 늦게 저장하려는 실행은 stale_claim으로 거절되고 가능한 경우 초안을 보존한다.
  모델 자체를 종료하거나 토큰 소비를 강제로 중단하지는 못한다. 플랫폼 실행 제한은 별도다.

예제 설정에서 auto_heartbeat만 true로 바꿔 기존 automation.json에 병합한다. stale은 최소 30초,
heartbeat는 최소 1초이고 stale의 1/3 이하여야 하며, 최대 실행시간은 heartbeat 간격보다 길고
1800초 이하여야 한다. 실제 상태 경로는 절대 경로를 권장한다.

`heartbeat-<wake>.json`은 준비 상태·PID·고정 기한을 보여 주는 보조 파일이며 소유권의 정본은
wake/gate/ACK다. PID가 있다는 사실을 모델 생존으로 해석하지 않는다. 종료 시 marker는 제거하고
잠금 파일은 남겨 inode 교체 경쟁을 피한다. 임의로 새 기한을 발급하는 재시작 명령은 제공하지 않는다.
구형 Hook처럼 claim 없이 wake 발급/ACK started만 하는 경로에는 자동 갱신을 붙이지 않는다.

## 도입 확인과 되돌리기

먼저 소스·상태·설정·DB를 보존하고 동일 전달의 gate/session/drafts/heartbeat를 함께 반영한다.
기존 auto=false에서 진단과 임시 15분 만료·초안 보존을 검증한 뒤, 격리 설정/가짜 수신으로
max_run_seconds를 짧게 잡아 자동 시작·부모 종료 후 생존·ACK 종료·소유권 상실·기한 인계를 확인한다.
실제 VM의 도구 실행기/프로세스 정리 정책 아래에서도 분리 프로세스가 생존하는지 확인한 뒤
운영 auto=true를 켠다. 변경한 설정은 다음 claim부터 적용한다. 서비스/크론을 새로 등록하지 않는다.

기존 실행이 있을 때 소스를 섞어 교체하지 않는다. 종료/인계를 먼저 확인한다. 긴 판단 동안 모델 호출
없이 heartbeat가 이어지고, 정상 종료 뒤 멈추는지 관측한다. 실제 모델 무응답을 조용히 복구하는 최적
상한값은 실측 대상으로 남긴다. 운영 회신의 setsid 생존 실험과 이 저장소의 로컬 시험은 별도 증거다.

롤백: auto=false로 다음 자동 시작을 끄고 현재 wake를 기존 ACK finished/failed로 정상 인계한다.
현재 데몬이 멈춘 것을 확인한 뒤 이전 소스를 반영한다. 필요하면 stale=180으로 복귀한다.
이미 발급된 claim_deadline_ms를 파일 편집으로 연장하지 않는다. DB·수신·발송 이력은 유지하며
archive를 DB 작업 큐로 import하지 않는다. 설정만 끈다고 이미 실행 중인 데몬을 강제 취소하지 않는다.

다른 인물에는 active ACK 우선 검사 유무, 같은 gate/ACK 계약, 직접 apply 경로, Linux 분리 프로세스
생존, DB 정합성·발송 계약을 확인한 뒤 필요한 부분만 적용한다. 인물별 지침·정책은 이식하지 않는다.
