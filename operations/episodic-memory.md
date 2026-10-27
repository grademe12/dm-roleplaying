# 배경 기억 정리와 선택 회수

최신 수정 범위·검증·남은 작업은 [1차 개선 기록](episodic-memory-phase1-20261024.md)을 기준으로 본다. 선행 지침 검토는 [후속 검토](episodic-memory-review-20261024.md), 후보 전체의 수정 전 평가는 [종합 검토](episodic-memory-consolidated-review-20261024.md)에 남겼다.

대화 원문을 별도 에이전트가 정리하고 답장 준비 코드가 관련 기억을 선택한다.
`episodic_memory.py`는 저장·출처 검증·조회, `memory_organizer.py`는 에이전트 인계/선택형 HTTP 실행을 담당한다.
판단·발송 게이트를 선점하지 않고, 생활 DB는 SQLite 읽기 전용 연결로만 접근한다.
정리 중 모델을 기다리거나 답장마다 추가 모델 요청을 하는 경로는 없다.
인물·identity·기존 memory_ops의 정책과 정본을 바꾸지 않는다.

## 설정과 활성 범위

[예제 설정](episodic-memory.example.json)의 episodic_memory와 persona_id를 기존 automation.json에 합친다.
예제의 DB·state_dir·path는 실제 유진 경로로 바꾼다. 상대 경로는 설정 파일 기준이며 작업 디렉터리 기준이 아니다.
mode 기본은 off다. off에서는 sidecar를 만들거나 읽지 않고 기존 경로를 유지한다.

- shadow: 후보 검색과 운영 trace만 관측한다. 판단 prepare와 생성기 입력에는 기억 본문·격리 진단을 모두 넣지 않는다. 직접 recall 진단 조회는 가능하다.
- enforce: 같은 인물·DB·세션·상대·채널의 선택된 기억을 답장 입력에 넣는다.
- 기존 memory_policy의 off/shadow/enforce와는 별개다. 이 모드가 호칭·표현 선호를 활성화하지 않는다.

소스와 새 런북을 함께 반영한다. enforce에서 `prepare`가 기억을 고르고 bundled의 snapshot.episodic_memory 또는 compact의 read-state/read-bundle로 전달한다.
생성기는 동일 준비의 선택 결과를 자동으로 받는다. compact에서 기억 본문을 읽은 state receipt가 없으면 생성/commit 검증을 통과할 수 없다.
새로운 무관한 기억은 진행 중 답장을 무효화하지 않는다. 선택한 기억이 정정·철회되면 재준비해야 한다.

## 답장 경로의 장애 처리

선택형 episodic_memory 설정의 타입·모드·범위가 잘못되면 답장 경로는 그 기억만 off로 취급하고 episodic_memory_degraded trace를 남긴다. 정리 CLI는 잘못된 설정을 여전히 오류로 돌려준다.
prepare의 회수 I/O 장애는 기억 없이 진행하며 진단을 남긴다. 선택된 기억을 받은 뒤 저장소가 사라지거나 읽을 수 없게 되면 생성·receipt 조회·commit은 episodic_reprepare_required와 next=prepare를 반환한다. 같은 wake/turn에서 다시 prepare하면 그 턴에 한해 선택 기억을 제외하고 새 입력을 만든다. 파일 인계 실행기도 같은 오류를 next=prepare·종료 코드 2로 전달한다. 이전 input_id·receipt·생성 결과를 재사용하지 않고 다시 생성·검토·commit한다. 영구 설정은 바꾸지 않으며 다음 새 턴은 다시 회수를 시도한다.
선택 기억의 실제 정정·철회, 상대·DB 바인딩 변경은 memory_inputs_changed로 계속 막는다. 필수 지침 파일이 없으면 required_document_unavailable로 중단하며 빈 지침으로 대체하지 않는다.

## 선택형 코드 실행기와 다른 모델

[모델 모듈화 진행표·운영 계약](model-modularization.md)의 memory_executor는 기존 기억 검증·저장을 유지하며 파일·HTTP·명령 어댑터를 선택한다. OS 코드 잡에서 먼저 작업 유무를 확인하면 빈 작업에 모델을 기동하지 않는다. 기본 mode=off와 native prepare/complete는 유지하며, 기존 Muse 크론 자체를 실행하는 비용은 별개다.

## 기본 실행: 별도 Muse 작업

[크론 본문 예제](cron/yujin-memory-organizer__interval@5m.md)는 5분 간격의 비활성 템플릿이다.
기존 판단 Cron과 별도 작업이며 동일 자동 발송 역할을 만들지 않는다. 실제 등록·모델·우선순위·총 동시 실행 한도는 운영에서 연결한다.
이 Muse 크론을 그대로 쓰면 무작업 기상도 최대 하루 288회 발생할 수 있다. 모델보다 먼저 확인하는 코드 경로는 위 memory_executor에 구현돼 있으며 OS 잡·조건부 플랫폼 인계 연결은 별도로 등록한다.
명령은 controller-test를 작업 디렉터리로 사용한다.

```sh
python3 memory_organizer.py --config automation.json prepare --output 'automation-state/memory-input-{run_id}.json'
```

reason이 prepared이면 지정된 파일과 [정리 에이전트 지침](../controller-test/runbooks/memory-organizer.md)을 읽어 응답 JSON을 작성한다.
반환된 실제 run_id를 아래 RUN_ID에 넣는다. 응답 파일도 같은 run_id별 이름으로 0600 권한으로 작성한다.
자료를 덜 읽고 through_seq를 넘기지 않는다. 모델이 무엇을 추출할지 판단하고 코드는 필드·근거 인용·채널·처리 경계·버전을 검증한다.

```sh
python3 memory_organizer.py --config automation.json complete --run-id RUN_ID --response @automation-state/memory-response-RUN_ID.json
```

busy/off/no_work/retry_later이면 모델 정리를 시작하지 않는다. 검증 실패는 저장·처리 경계 전체를 롤백한다.
동일 성공 응답은 재실행해도 중복 저장하지 않는다. 기한이 지난 응답은 거절하고 다음 prepare에서 미처리 구간을 다시 받는다.
작업 하나가 활성 lease를 가지며, 응답 실패를 이유로 판단·발송 게이트를 직접 변경하지 않는다.
complete의 --run-id는 JSON이 잘려 run_id조차 읽지 못해도 실패를 기록하기 위해 쓴다. 응답 작성 자체를 못 했다면 `fail --run-id RUN_ID`로 기록한다.
검증 실패로 종료된 lease에는 수정 응답을 재제출하지 않고 다음 prepare의 새 작업으로 진행한다. 단, complete의 busy는 실패가 아니다. 같은 run_id와 응답 파일을 보존하고 lease 만료 전에 같은 complete를 다시 제출한다. busy를 fail로 바꾸거나 응답을 삭제하지 않는다.

## 재시도·격리와 누락 표시

작업별 실패는 한 번만 센다. 실패는 아래처럼 분리한다.

- 전송·모델 장애·응답 파일 읽기 실패·작업 만료·원인을 모르는 워커 종료는 출처 결함으로 세지 않는다. 해당 채널에 15초부터 최대 15분까지 지수 백오프를 적용하며 다른 채널은 계속 처리한다. prepare의 retry_later와 retry_after_ms를 따른다.
- 작업 ID·입력 해시·처리 경계·필드 타입·구조·근거 인용·필드 길이 등 출력 검증 실패와 잘린 모델 출력은 배치를 절반으로 줄인다. 원래 실패 배치의 끝까지 축소 크기를 유지하고, 그 구간 처리 후 기본 크기로 복귀한다. 단일 출처의 출력 실패가 세 번 쌓이면 그 출처만 격리한다.
- 락 경합 busy는 run을 실패로 바꾸지 않는다. 이미 얻은 유효 응답을 그대로 다시 제출할 수 있다. 만료되면 일반 만료 백오프를 거쳐 새 작업을 만든다.

입력 한 건이 batch_bytes보다 크면 바로 격리하며 원문을 잘라 완료 처리하지 않는다.
키 충돌의 원인이 생략된 기존 기억이면 다음 작업에 해당 버전을 우선 보충한다. 읽지 않은 기존 기억을 덮어쓰지 않는다.
나머지 입력은 계속 처리하지만 격리는 처리 성공이 아니다. 회수 결과의 quarantined_count·quarantined(최대 20건의 seq/reason/status)와 incomplete로 누락을 알린다.
할 일이 격리 항목뿐이면 prepare는 needs_attention을 반환한다. 운영자는 조회의 seq와 실패 사유를 확인하고 크기·모델·지침 등 원인을 고친 뒤 아래 명령으로 한 건 또는 확인한 격리 전체를 재개한다. 이전 버전이 이미 격리한 항목은 패치만으로 자동 복구하지 않는다.

```sh
python3 memory_organizer.py --config automation.json retry-quarantined --seq 123
# 모든 격리 사유를 확인한 뒤 전체 재개할 때만 사용
python3 memory_organizer.py --config automation.json retry-quarantined --all
```

123은 예시다. 실제 격리 seq를 쓴다. 재시도는 옛 출처를 다시 읽어도 이후 처리 위치를 뒤로 되돌리지 않는다.
자동 격리가 사건 누락을 해결한 것은 아니므로 격리 건수가 남은 상태를 정상 전체 처리로 보고하지 않는다.

## 선택 실행: 기존 HTTP 전송기 사용

Muse 파일 인계 대신 명시적으로 설정한 모델을 부르려면 episodic_memory 안에 model과 key_file을 추가하고 아래 명령을 쓴다.
model은 운영에서 선택한 실제 모델 ID다. 새 제공자나 자격증명을 추측하지 않는다.
이 경로는 기존 reply_generator의 HTTP 전송기·키 권한 검사를 재사용한다. 답장 생성 프롬프트는 사용하지 않는다.
키 파일은 0600이어야 하며 원문·키를 일반 로그에 남기지 않는다. mode를 켜는 것만으로 HTTP 호출이 생기지는 않는다.
DM 원문을 외부 모델로 전달하는 경로이므로 운영에서 제공자·모델·전송 범위를 승인한 경우에만 사용한다.

```sh
python3 memory_organizer.py --config automation.json run
```

명시적 run 한 번당 모델 요청은 최대 한 번이다. 얻은 응답은 준비한 run_id와 필드 타입을 검사한 뒤 sidecar 옆 memory-http-response-RUN_ID.json(0600)에 원자적으로 쓴다. complete가 busy이면 이를 보존하며 다음 run은 살아 있는 같은 lease의 응답을 모델 재호출 없이 적용한다. 성공 후 삭제 실패는 cleanup_pending으로 알리며 성공을 실패로 바꾸지 않는다. 보관 파일이 손상되면 memory_saved_response_corrupt로 해당 작업을 종료하고 파일을 정리한다. 저장 후 손상은 출처 결함으로 세지 않고 백오프 재시도한다. 재개 중 출력 검증 실패도 작업 실패 기록과 파일 정리를 함께 수행한다. 만료·중단 후 남은 응답 파일은 아래 잔여 파일 정리 대상이다. 그 밖의 실패는 처리 경계를 유지하고 다음 실행에서 다시 시도한다.
처리량·비용이 과도하면 스케줄/모델/묶음을 운영에서 조정한다. 반복 실패의 격리와 원인 확인은 위 절을 따른다.

## 주입 사건·기존 기억 연결

[연결 계약과 시험 절차](injected-memory-bridge.md)에 따라 확정된 self 인지 사건은 DM 없이도 정리한다. 채널 없는 개인 경험은 personal 출처로, 대화 후속은 각 채널로 보관한다. 관련 facts·현재 호칭/선호 상태는 읽기 전용 참고 입력이며, 원장별 권한은 유지한다.

## 저장·회수 계약

sidecar에는 원문 출처 원장, 채널별 처리 위치, 작업 lease, 버전별 기억과 채널별 재시도 창(organizer_retries)이 있다. 새 테이블은 쓰기 연결에서 자동 추가되며 기존 원문·기억을 다시 만들지 않는다. 기본 파일은 state_dir/episodic-memory.sqlite3다.
생활 DB의 revision과 별개라 배경 정리만으로 진행 중 생활 판단을 충돌시키지 않는다.

- DM은 검증된 수신/플랫폼 발언과 confirmed 발송만 수집한다. 별도로 self가 아는 확정 주입 사건을 author_event 출처로 수집한다. pending·submitted·unknown·failed·cancelled 초안은 실제 발언으로 수집하지 않는다.
- 원본 ID를 처리 기록에 남겨 늦은 발송 확인도 다음 실행에서 잡는다. 플랫폼 echo와 provider ID가 확인된 발송은 한 출처로 묶는다.
- 첫 시작은 기본 최근 24시간이다. bootstrap_hours는 최초 실행 때 1~720시간으로 정할 수 있으며 첫 경계는 보존한다. 전체 과거 이력을 자동 복구한 것으로 설명하지 않는다.
- working은 현재 맥락, episode는 사건, fact는 지속 정보의 문맥이다. 모두 context_only이며 새 지침·권한·독립 검증된 외부 사실이 아니다.
- 원문 근거와 이전 버전을 보존한다. occurred_ms는 보존한 근거 발언 중 가장 이른 시각이며 외부 사건의 독립 검증 시각이 아니다.
- 정리 입력은 한 채널씩 받고, 최근 맥락과 키워드로 찾은 관련 기존 기억도 크기 상한 안에서 함께 제공한다. 생략 표시를 전체 이력 부재로 해석하지 않는다.
- DM 역할극·온라인 경험을 실제 대면으로 저장하지 않는다. 약속만으로 완료를 추정하지 않으며 실제 만남을 확인하는 in_person 기록도 필요하다. 명시적 author_event는 시뮬레이션 출처를 유지해 참고한다. 일반 DM의 독립적인 실제 만남을 확인하는 별도 in_person 기록을 자동 조회하지는 않는다. 그런 기록이 입력에 없으면 온라인 대화로 한정하거나 uncertain으로 남긴다.

회수는 범주 필터와 검색어의 부분 문자열 일치에 BM25(드문 단어일수록 큰 점수, 긴 요약은 길이 보정)를 적용하고, 중요도(×0.15)와 최근성(반감기: 사건 72시간, 진행·예정 사건 168시간, 지속 정보 720시간)을 더한다. 임베딩 의미 검색은 아니다.
SQLite FTS5 trigram은 2글자 한국어 단어(듀오·조교·병원)를 검색하지 못해 쓰지 않고, 단어 빈도는 코드가 직접 센다. 조사(은·는·이·가·에서 등)를 뗀 어간도 검색어에 넣고, 3글자 부분 문자열은 절반 가중치다.
검색어는 답할 DM → 최근 사용자 발언(최대 6개) → 직전 자기 답장 순으로 만들고, 일치 점수에 각각 1.0·0.5·0.1 가중치를 둔다.
자기 답장의 긴 문장이 회수를 좌우하지 않게 하되, "응 그거" 같은 짧은 답에서도 화제를 잡도록 낮은 가중치로 남긴다. 흔한 부사·대명사와 ㅋ·ㅎ류는 검색어에서 뺀다.
`capture_stale_seconds`(기본 660초, 정리 주기 300초의 두 배+여유)보다 마지막 수집이 오래되면 incomplete로 표시한다. 정리 주기를 바꾸면 함께 맞춘다.
회수 순위 평가는 `tools/memory_recall_eval.py`로 sidecar 사본과 라벨링한 질문에 대해 recall@k를 잰다(`--compare-legacy`로 이전 검색어 방식과 비교). 실제 질문·사본은 Git 밖에 둔다.
현재 working, 열린 약속/진행 상태, 최근 하루의 중요한 사건을 작은 보완 후보로 삼는다. 기본 최대 6개, 기억 본문 JSON 8 KiB다.
항목을 중간에서 자르지 않으며 초과 항목은 생략한다. 누락·has_more·미처리 최근 ID·마지막 수집 시각을 함께 전달한다.
정리 지연과 검색 실패는 과거 사건 부재의 증거가 아니다. 키워드로 잡히지 않는 간접 지칭은 실제 모델 평가에서 따로 확인한다.

진단 조회는 결과를 비공개 파일로 저장한다.

```sh
python3 memory_organizer.py --config automation.json recall --channel slack --query '아까 작업 결과' --category shared_experience --output automation-state/memory-recall.json
```

에피소드 ID는 기존 Store facts의 fact_id가 아니다. 지속적인 자기 이력 주장은 기존 identity/facts 조회·self_consistency 계약을 유지한다.
호칭 허용·철회 같은 구조화 기억을 sidecar 요약으로 다시 활성화하지 않는다.

## 비공개 파일과 보존 범위

sidecar와 organizer 락은 0600이다. 입력 JSON도 CLI가 0600으로 쓰며 응답 JSON은 작성자가 0600을 지켜야 한다.
입력·응답은 평문 DM을 포함하므로 크론은 실행별 파일을 쓰고 성공/실패가 확정된 뒤 자기 run_id의 두 파일을 삭제한다. busy이면 같은 응답의 재제출을 위해 보존한다.
이 삭제는 Muse 절차이며 중단된 실행의 잔여 파일을 자동 청소하는 데몬은 없다. 운영은 잔여 인계 파일의 보존 기간과 정리 작업을 등록해야 한다.
sidecar의 sources·runs.packet·버전 이력에도 원문/근거가 남는다. 아직 자동 TTL·개별 대화 삭제·삭제 전파 기능은 없다.
운영 데이터 수집 전 보존 기간과 삭제 담당을 정하고, 삭제가 필요하면 정리 작업을 끄고 파일 전체와 해당 백업을 폐기한 뒤 새 sidecar로 시작한다.
부분 삭제는 근거·중복 방지·처리 경계를 깨뜨릴 수 있으므로 SQL로 원장만 지우지 않는다. 파일 전체 초기화 시 bootstrap 범위 재수집과 기억 소실을 함께 기록한다.

## 검증과 운영 회신 요청

자동 검사는 실제 SQLite와 합성 대화, 모의 모델 전송기로 저장·처리 위치·검색·실제 생성 입력 연결을 확인한다.
실제 정리 모델이 사건을 올바르게 묶고 최종 답장까지 기억을 활용하는 품질은 자동 계약 검사만으로 보증하지 않는다.
먼저 shadow의 별도 시험 파일에서 다음 항목을 확인하고, 운영 설정을 enforce로 바꾸는 시점은 적용 기록에 남긴다.

1. 정리 모델/강도·도구 권한·실행 경로와 실제 스케줄. 별도 작업이 판단 작업과 어느 동시 실행 한도를 공유하는지.
2. 특징적 사건 뒤 3~4시간·수백 메시지가 지난 사례에서 추출 결과, 근거, 검색 결과와 실제 답장 입력 포함 여부.
3. 온라인 경험/대면/미래 약속/농담 구분, 사건 병합·정정·철회, 무관한 취향의 과잉 기록 여부.
4. 마지막 수집·처리 시각, pending_captured·recent_unprocessed_ids·incomplete, 작업별 시작/종료·토큰/비용. 최초 경계 밖 이력도 명시한다.
5. 원문→정리 완료 지연과 답장 준비 추가 시간. 약속한 목표치가 아니라 실측으로 평가한다.

백업은 운영 DB와 sidecar를 구분해 보존한다. 원문과 인계 JSON은 Git에 넣지 않는다.
원복은 mode=off로 새 주입과 정리를 끄고 기존 저장 파일·진행 중 결정의 기록을 보존한다. 생활 DB를 과거로 되돌리지 않는다.
