# 코드가 이어가는 판단 실행 루프

2026-10-26. `799427d`의 역할별 실행기 위에 추가한 선택형 진입이다. 기본 비활성이며,
운영 설정·프롬프트·서비스·크론은 자동 변경하지 않는다. 단일 모델과 역할별 모델을 모두 지원한다.

## 해결하는 문제

모델 프롬프트의 "commit까지 40회 반복"은 실행 루프가 아니다. 도구의 busy나 장시간 실행을
모델이 완료로 받아들이거나 세션을 종료하면 다음 기동까지 작업이 끊어진다. 기존 실행기가
반환한 after.next=prepare/finish를 운영 호출자가 이어야 했던 부분도 남아 있었다.

`controller-test/decision_driver.py`는 모델과 분리된 코드 프로세스에서 다음을 계속 수행한다.

- busy는 완료가 아닌 대기다. 원래 wake의 기한 안에서 같은 저장 결과·실행 기록으로 재개한다.
- 모델 결과 파일을 비공개 저장한 뒤 적용한다. 적용·발송·후처리 오류는 기존 recover로 재개하며
  이미 저장된 답장을 새로 작성하지 않는다. 실제 입력 변경에는 기존 재준비·검증을 유지한다.
- committed=true 이후 after.next=prepare는 같은 wake의 다음 턴, finish/retry_same_commit은
  후처리 재개다. ok=true만 보고 종료하지 않는다. 예상하지 못한 next는 미완료 오류로 남긴다.
- 파일 결과 대기 횟수와 잠금 충돌은 모델 호출·진행 단계 상한을 소비하지 않는다. 기존 claim,
  worker_execution·세션 기한과 모델 전송 시도 상한은 유지하며 기한을 연장하지 않는다.
- 모델은 지정된 결과 하나를 작성한다. 셸 실행·폴링·다음 역할 호출을 모델에게 맡기지 않는다.

플랫폼의 같은 모델 컨텍스트를 강제로 유지하는 기능은 아니다. 로컬 코드가 대기를 담당하므로
플랫폼의 명령 도구가 background 상태로 넘어가거나 호출 모델이 종료해도 진행·결과가 남는다.
플랫폼이 작업 종료 시 자식 프로세스의 cgroup까지 정리하는 환경에서는 아래 start만으로
생존을 보장할 수 없다. 그 환경은 systemd 등 플랫폼 세션 밖의 감독자가 run을 실행해야 한다.
해당 플랫폼의 비공개 실행 ID를 조회하거나 도구의 background 결과를 폴링하는 API는 추가하지 않았다.

## 두 실행 방식

설정은 [예시](decision-driver.example.json)의 항목을 **기존 automation.json에 병합**한다.
DB·identity·전송·기억 설정을 예제 전체로 교체하지 않는다.

| decision_driver.mode | 동작 |
|---|---|
| muse | 기존 MuseExecutor의 compact-v1 입력·추가 조회·의미 판단·검토·저장을 모델 하나가 처리. adapter는 필수 |
| roles | 기존 role_executor를 코드 루프로 진행. 역할별 설정은 role_execution.adapters 그대로. role_execution.enabled=true 필요 |

이 스위치는 다른 기억·인물 처리기의 활성화를 자동 변경하지 않는다. 단일 의미 판단 모델의
muse 모드는 role_execution을 켤 필요가 없다. 기존 reply_generator가 enabled면 그 계약도
그대로라서 별도 작성기 사용 여부는 운영에서 독립적으로 맞춘다.

## 진입과 관측

기존 작업이 실제 끝난 경계에서 진입을 교체한다. 같은 작업에 기존 판단 루프와 새 driver를
동시에 붙이지 않는다. 이미 선점한 wake는 그대로 전달한다. 훅 진입은 기존 invocation ID를 전달한다.
아래 명령은 controller-test를 작업 경로로 사용한다.

```sh
# 코드가 직접 선점하고 실행. 호출 모델의 셸 대기가 필요 없는 별도 프로세스다.
python3 decision_driver.py --config /path/to/automation.json start

# 이미 선점한 경우 (ID는 실제 진입에서 받은 값 사용)
python3 decision_driver.py --config /path/to/automation.json --wake-id WAKE start

# 훅이 기동한 경우. ticket을 임의로 만들거나 다른 wake에서 재사용하지 않는다.
python3 decision_driver.py --config /path/to/automation.json --invocation-id ID start

# 상태만 조회. 선점·heartbeat·모델 호출·재시도를 하지 않는다.
python3 decision_driver.py --config /path/to/automation.json status

# systemd 등 독립 프로세스 감독자가 사용하는 포그라운드 진입
python3 decision_driver.py --config /path/to/automation.json run
```

start의 driver_started/driver_running과 next=status는 **처리 완료가 아니다**. 실행 식별자와
상태는 state_dir/decision-driver/current.json, 프로세스 실행 정보는 launch.json에 남는다.
중복 start는 실행 중인 로컬 코드 프로세스에 연결한다. 실제 진행 락도 별도로 잡아 두 코드
프로세스가 같은 작업을 실행하지 못하게 한다. 죽은 코드 프로세스의 미완료 작업은 같은 설정으로
start/run하면 저장된 입력·결과·commit 기록에서 이어간다. 다른 wake로 권한을 이식하지 않는다.

status는 job_id/wake_id/phase/reason/terminal/completed/worker_alive와 대기 요청 ID를 반환한다.
worker_alive는 PID 존재 여부이며 모델의 생존·진행 증명이 아니다. 프로세스가 죽어도 미완료 상태를
완료로 바꾸지 않는다. 새 run에 --wake-id를 명시하면 그 wake의 완료 결과를 다시 조회할 수 있다.

| 상태 | 의미 |
|---|---|
| waiting / next=status | 잠금·모델 파일·I/O 대기. 같은 코드 작업을 유지 |
| completed / completed=true | 해당 판단 세션이 정상 종료됐고 이 driver가 기록한 그룹에 미확정 발송이 없음 |
| handed_off / completed=false | 판단은 종료됐지만 발송 pending/attempting/submitted/unknown/failed가 남음. delivery에 건수 표시 |
| stopped / completed=false | 기한·단계·시도 상한, 소유권 상실 또는 오류로 중단 |
| skipped / completed=false | gate가 이번 선점을 허용하지 않음. 전역 작업 완료를 뜻하지 않음 |

terminal=true는 코드 작업이 끝났다는 뜻이다. 전송 결과 불명은 기존 폴러/대조 경로에
명시적으로 인계한다. 이미 finished인 wake를 억지로 연장하거나 새 모델을
계속 깨우지 않는다. completed=true도 미래 DM이나 원장 전체의 모든 예약이 끝났다는 뜻은 아니다.

current.json에는 원문 입력·모델 결과가 있으므로 비공개 운영 상태로 보관한다. 폴더 0700, 파일
0600이며 Git/일반 로그에 넣지 않는다. worker.log와 stdout은 메타데이터만 기록한다.
장기 보관·정리는 운영 정책으로 정한다.

## 파일 처리기와 Dropbox 연결

muse 모드의 file 어댑터는 decision_driver.path(기본 state_dir/decision-driver)에
decision-model-input-UUID.json / decision-model-response-UUID.json을 사용한다.
작업 종류는 **persona_decision**이다. 기존 persona_writer만 처리하는 외부 워커를 이 작업에
그대로 배정하지 않는다. 이 작업은 문장만이 아니라 원래 Muse의 전체 의미 판단을 담당한다.

입력은 공통 persona-model-request-v1이다. input.original_input에 기존 Muse 입력 원문이
그대로 들어 있고, input.response_envelope에 실제 Muse request_id/input_sha256이 있다.
결과는 persona-model-response-v1의 output 안에 **기존 Muse 응답 봉투 전체**를 넣는다.

```json
{
  "schema": "persona-model-response-v1",
  "request_id": "입력 request_id",
  "input_sha256": "입력 input_sha256",
  "output": {
    "request_id": "input.response_envelope.request_id",
    "input_sha256": "input.response_envelope.input_sha256",
    "input_read": true,
    "action": "commit",
    "conversation_ended": false,
    "decision": {}
  }
}
```

decision의 빈 객체는 위치만 보여준다. 실제 생활·답장·검토·기억 결과는 모델이 작성한다.
추가 조회는 action=read, 경고 검토는 approve_warning, 의미상 할 일이 없을 때만 close를 쓴다.
원문 전체를 실제로 읽어야 input_read=true로 확인한다. 요청별 모델 컨텍스트 보존을 가정하지
않으므로 retain_context=true는 받지 않는다. 기존 같은 턴의 receipt·해시 검증은 유지한다.
HTTP/command 어댑터는 같은 공통 입력·output을 직접 호출하며 결과를 적용 전에 저장한다.

decision_driver.dropbox=true일 때만 실제 Dropbox를 쓴다. 기본 remote root는 muse 모드
/yujin-project/decision-exchange, roles 모드는 기존 /yujin-project/role-exchange다.
필요하면 remote_root, dropbox_config_dir를 설정한다. 인증 파일은 기존 교환기와 같은 계약이다.
코드 루프가 공용 bridge의 업로드·다운로드를 직접 호출하고 결과를 소비한다.
별도 bridge --complete 루프를 추가하지 않는다. 기억/인물 bridge의 원장·폴더·락은 그대로 둔다.

roles 모드는 기존 role-model-input/response 파일을 사용하며 모델을 새 방식으로 재배정하지 않는다.
역할별 외부 모델 호출·수정 상한, 생성 결과 재사용·의미 검토·최종 원자 적용도 유지한다.

## 운영 전환과 원복

1. 새 코드·설정·worker의 task/출력 봉투 연결을 시험 DB에서 함께 맞춘다.
2. 기존 반복 프롬프트·role bridge --complete와 중복 기동하지 않도록 진입을 교체한다.
3. 실제 훅 ID/선점 경로, 로컬 코드 프로세스가 플랫폼 모델 종료 뒤 생존하는지 확인한다.
4. busy 뒤 같은 요청 ID 재개, paid 결과 재사용, after.next=prepare 같은 wake 유지,
   완료/인계/중단 상태를 운영 기록에서 확인한다. 실제 모델 품질·지연·비용은 별도 측정한다.

원복은 새 driver 기동을 중지하고 실제 진행 작업을 정상 종료/명시적 인계한 뒤 enabled=false로
바꾸고 기존 진입으로 되돌린다. 실행 중인 driver가 설정 파일 변경을 즉시 읽어 종료한다고 가정하지 않는다.
원본 DB·발송 원장·저장된 응답을 삭제하거나 이미 보낸 파트를 다시 만들지 않는다.

이번 구현은 운영 프로세스·설정·크론을 바꾸거나 실제 모델/Dropbox를 호출하지 않는다.


## 검증 기록

- 신규 driver 시험 25건: 실제 SQLite/선점·가짜 모델/Dropbox, busy 뒤 동일 paid 결과 재사용,
  저장 후 finish 복구·같은 wake의 다음 턴·대기 횟수와 단계 예산 분리·기한/소유권 상실·
  경고 승인 뒤 발송 그룹 추적·최종 ACK 뒤 중단 복구·전송 시도 상한·중복 start·관측만 하는 status.
- 실제 별도 코드 프로세스가 파일 응답을 받아 끝내는 시험을 포함한다. 외부 모델은 가짜 응답이며,
  Muse 플랫폼이 프로세스를 어떻게 정리하는지까지 검증한 것은 아니다.
