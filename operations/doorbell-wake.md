# 유진 doorbell 운영 연결

이 문서는 검토용 설치 계약이다. 실제 계정·토큰 이동·서비스 등록·훅 등록·크론 변경을 실행하지 않았다. 기존 수정·나현 설정에는 적용하지 않는다.

## 구성

1. **미니 PC:** `slack/doorbell_listener.py`가 지정 Slack DM의 수신을 감지하고 `doorbell.py`의 별도 SQLite에 seq·Slack ts·시각만 남긴다. 본문·첨부·대화 DB는 보관하지 않는다. 감지 기록을 저장한 뒤 Socket envelope를 ACK한다.
2. **VM 일반 서비스:** `wake_receiver.py`가 15초마다 제한된 SSH JSON RPC로 알림을 읽는다. 기존 Slack backfill로 실제 메시지·최근 문맥을 VM의 기존 DB/상태 파일에 적재한 뒤 캡처한 seq까지만 ACK한다. 이 서비스만 SSH/Slack 자격증명을 사용한다. Muse를 깨우거나 gate claim·답장·발송을 하지 않는다.
3. **자격증명 없는 런타임 훅:** `operations/hooks/wake.sh`를 15초 간격으로 등록한다. 로컬 `gate --check`만 실행해 `wake_eligible`을 확인하고 제공된 `wake`/`silent` 헬퍼 중 하나로 끝낸다. 새 외부 호출 API·무인 모델 클라이언트는 없다.
4. **등록 워커:** 기존 크론과 동일하게 `gate --claim` → 성공한 wake의 `CRON_RUNBOOK.md` 실행 → `finished/failed`. 훅 payload는 진입 힌트이며 권한이 아니다. 훅에서 claim한 것으로 취급하지 않는다.

hook 런타임에는 토큰·SSH 키가 없다는 실제 계약을 반영했다. 따라서 훅이 미니 PC의 비인증 공개 HTTP를 직접 읽도록 만들지 않았다. HTTP 서비스 신설 대신 일반 서비스의 제한 SSH와 로컬 읽기로 역할을 나눈다. 기존 운영자가 이미 검증한 호출 헬퍼를 그대로 쓴다.

## 준비와 비밀값

- 소스·주 DB·기존 automation-state·운영 설정·슬랙 앱 설정을 백업한다.
- 미니 PC의 전용 사용자·설정·DB 폴더를 준비한다. 토큰 파일과 SSH 키는 0600, 폴더는 0700 등 제한된 권한으로 별도 제공한다. 예시에는 실제 토큰·키·사용자 ID가 없다.
- 미니 PC의 `operations/doorbell.example.json`을 실제 경로·workspace·DM channel·peer·기존 ingest 경계로 채운다. 자동으로 새 경계를 잡지 않는다.
- `python3 doorbell.py --db /var/lib/yujin/doorbell.db --init`는 **최초 한 번**만 실행한다. 누락/손상 원장을 자동 재생성하지 않으며 재초기화로 seq·예산 같은 운영 상태를 초기화하지 않는다.
- broker용 전용 SSH 키는 강제 명령 `python3 /srv/yujin/controller-test/doorbell.py --db /var/lib/yujin/doorbell.db`만 허용한다. 포워딩·PTY·일반 셸을 허용하지 않는다. 인자는 고정하고 요청 JSON은 stdin으로만 전달한다.
- VM의 기존 automation.json에 `operations/wake-bridge.example.json`의 wake 항목만 병합한다. 기존 db/session/slack/transport/policy를 보존한다. `enabled`는 검증 뒤 명시적으로 켠다.
- mini/VM 양쪽의 persona·workspace·DM channel·peer·ingest 경계를 대조한다. VM은 auth.test와 실제 적재된 메시지 ID까지 확인한다.
- Slack SDK는 기존 `requirements-slack.txt`를 사용한다. 실제 Slack history 호출 제한/429를 확인하고 `sync_interval_ms`를 운영 앱에 맞춘다. 훅의 15초 간격이 매번 history 호출을 강제하지는 않는다.

## 전환 순서

1. 기존 1분 크론을 유지한 상태에서 격리 DB·모의 런타임으로 테스트한다. 기존 state_dir·DB를 복사해 외부 발송 없이 후보 환경에서 확인한다.
2. VM 수신 실행기를 한 번 실행해 backfill·recent_thread·heartbeat·실제 발송 영수증 경로를 검증한다. 기존 VM Socket listener와 새 receive_once는 같은 상태 파일을 쓰므로 동시에 운영하지 않는다.
3. 전환 창에서 VM Socket listener를 중지하고, 기존 상태 파일을 보존한 채 VM 수신 실행기를 시작한다. 미니 PC Socket listener를 시작한다. 같은 Slack 앱의 소켓 여러 개는 이벤트가 분배되므로 양쪽이 전체 이벤트를 받을 것이라고 가정하지 않는다.
4. VM receive_once의 일반 수신 backfill에서만 선택적으로 정확한 발송 영수증을 대조한다. 미확인 발송 시점까지 되감는 delivery 전용 history 스캔은 재도입하지 않는다. 기존 delivery worker·send guards·실제 confirmed 계약은 유지한다.
5. 15초 훅과 아래 워커 프롬프트를 등록하고 dry-run에서 wake/silent가 정확히 하나 나오는지 확인한다. payload로 직접 apply하지 않는다.
6. DM·예약답장·활동 종료·일정·아침 연락·의도·이어가기 보충이 제시간에 기동되는지 관측한다. 기존 시간 판단은 15초마다 가벼운 gate 점검으로 유지한다. 별도 30분 그리드로 늦추지 않는다.
7. 위 경로 확인 뒤 기존 1분 Muse 크론만 30분 백스톱으로 완화한다. 백스톱은 기존 워커의 gate --claim 계약을 그대로 쓰므로 빈 실행 비용이 하루 최대 48회분 남는다. HATCH_HOOK_RUNTIME 헬퍼는 등록 훅 실행에서만 제공되므로 일반 systemd/cron에서 이 셸 훅을 임의로 호출하지 않는다. 기존 maintenance·의도 monitor·폴러·발송·피드는 자동 변경하지 않는다.

`systemd/yujin-doorbell-listener.service`와 `yujin-wake-receiver.service`는 비활성 템플릿이다. 사용자·경로와 SDK 환경을 채우고 검토해 설치한다. VM 교체 뒤에는 수신 실행기 **및 등록 훅**을 복구하는 bootstrap/watchdog 경로도 함께 적용한다. 기존 watchdog의 옛 Socket listener 자동 재설치를 그대로 두면 새 서비스와 충돌하므로 운영 전환 때 감시 대상을 명시적으로 바꾼다.

## 등록 워커 프롬프트

유진 DM/시간 기반 판단 기상이다. 지정된 유진 작업 디렉터리에서 `python3 gate.py --config automation.json --claim`을 먼저 실행한다. 종료 코드 10이면 조용히 끝낸다. 0이면 현재 wake.json의 wake_id를 읽고 CRON_RUNBOOK.md를 실행한다. 다른 코드는 운영 장애로 보고한다. 성공/실패는 해당 wake_id로 gate --ack-phase finished/failed를 기록한다. 훅 이벤트의 seq·revision·reason을 소유권이나 최신 DB 스냅샷으로 사용하지 않는다. 수정·나현에는 적용하지 않는다.

등록 대상은 유진 side_chat으로 실제 ID를 운영 환경에서 지정한다. poll_interval_secs=15. `HATCH_HOOK_RUNTIME`은 런타임이 제공하며 `source` 뒤 wake/silent를 사용한다. `CONTROLLER_AUTOMATION_CONFIG`, `CONTROLLER_SOURCE`, 필요 시 `CONTROLLER_PYTHON`에는 비밀값 없는 경로만 준다. Bash와 Python만 사용하고 jq는 요구하지 않는다. HATCH_HOOK_DRY_RUN=1은 debounce 상태를 전진시키지 않는다.

## ACK·재시도·시간

- offer는 epoch/binding/consumer/상한 seq에 묶여 있다. 처리 중 도착한 새 알림을 ACK에 포함하지 않는다. 100건을 넘으면 여러 poll에 걸쳐 인수한다.
- 실제 수신 backfill이 전체 페이지를 완료하고 모든 힌트의 메시지가 주 DB에 존재해야 handoff를 내구성 파일에 쓴다. 그 뒤 ACK한다. 부분 backfill·429·적재 실패는 ACK하지 않는다.
- ACK 응답을 잃으면 같은 offer로 멱등 재시도한다. 알림 소모는 답장 완료와 별개이며 실제 미처리 수신은 주 DB에 남는다.
- 훅의 마지막 wake 시각은 30초 debounce일 뿐 영구 워터마크가 아니다. 런타임 기동 실패 뒤에도 같은 작업이 다시 기상할 수 있다. 활동 중 워커·conversation lease·기존 retry backoff는 gate의 읽기 전용 `wake_eligible`로 따른다.
- broker 장애는 빈 알림과 구분해 기록한다. VM은 알림 없이도 기본 60초 간격으로 일반 수신 backfill을 수행해 소켓이 살아 있으나 이벤트가 누락되는 경우도 복구한다.
- DB의 next_decision·activity/appointment·reply_due·의도/이어가기 등의 기존 needs_decision을 계속 gate가 검사한다. Muse 호출 빈도는 실제 업무와 재시도 정책으로 결정하며 고정 토큰 절감 수치를 보장하지 않는다.

## 복구·롤백

- 미니 PC DB와 VM 주 DB·automation-state를 각각 SQLite/운영 백업 절차로 보존한다. offer·epoch·consumer 인수 상태도 운영 자료이며 Git에 올리지 않는다.
- 원장 epoch가 바뀌면 빈 큐로 간주하지 않고 오류를 남긴다. 원장 복원·주 DB의 백필 범위 확인 후 인수 상태를 명시적으로 재설정한다. 주 DB만 과거로 복원하고 최신 handoff 파일을 재사용하지 않는다.
- 롤백은 훅과 새 두 서비스를 중지하고 기존 VM Socket listener·1분 크론·기존 watchdog 구성을 복구한다. 주 DB·발송 이력·미확인 작업을 삭제하거나 재발송하지 않는다. 수신 경계는 보존하고 일반 backfill로 공백을 메운다.
- 실제 수신→ingest→gate claim→첫 confirmed 시간, 빈 모델 기상 횟수, ACK 대기·backfill 실패·429·detector heartbeat·VM 재생성 후 복구를 관측한 뒤 유지 여부를 판단한다.

공식 확인: [Slack Socket Mode](https://docs.slack.dev/apis/events-api/using-socket-mode/), [conversations.history](https://docs.slack.dev/reference/methods/conversations.history/). Socket ACK·다중 연결 분배·수신 이력 API의 현재 제한을 운영 앱에 맞춰 확인한다.
