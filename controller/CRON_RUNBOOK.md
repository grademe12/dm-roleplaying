# 판단 진입·저장·대화 세션

기본 경로는 decision_session이다. 파일 인계 실행기는 선택형이다. 같은 커밋의 decision_session.py·Store/gate를 함께 배포한 뒤 사용한다.
이 문서와 코드가 고른 실행 문서 하나부터 읽고, 주제별 필수 지침·의미 판단·API 검증을 유지한다. 과거 전체 런북 `runbooks/legacy-reference-20261017.md`는 자동으로 읽지 않는다.

| 진입·선점 조건 | 행동·제한 |
|---|---|
| 시간 기반 / doorbell 훅 기동 | 먼저 `gate --claim`. 훅의 `--check`/wake payload는 선점·입력 스냅샷이 아님. 종료 코드 10: 조용히 종료, 0: 아래 절차 진행 |
| `worker_execution.enabled=true` | 훅의 `controller_invocation_id`를 `gate.py --config automation.json --claim --invocation-id ID`로 전달. 로컬 훅 기동 요청부터 claim·heartbeat·발송·대기가 고정 기한을 공유 |
| 기동 시각 없는 크론 | 기본 `worker_entry_fallback`: claim 진입부터 제한. 런타임 생성 시각을 아는 것은 아님. [워커 시간 예산](../operations/worker-budget.md) 참조 |
| `decision_claim.auto_heartbeat=true` | claim이 wake별 갱신 데몬 시작. 모델의 별도 시작·heartbeat 호출 불필요. ACK 종료/소유권 상실/원 선점 최대 30분 중 먼저 도달할 때 종료. worker_execution 사용 시 더 짧은 고정 기한 적용 |
| auto_heartbeat 꺼짐 | 긴 모델/도구 작업 전후 `gate --heartbeat` 유지. 기본 선점 만료는 임시 15분, `automation.json`의 `decision_claim.stale_after_seconds`로 조정 |
| 자동 갱신 기한 | 원 선점의 `claim_deadline_ms` 고정. 수동 갱신·데몬 재시작·새 입력으로 연장 불가. 기한 뒤 저장은 `stale_claim` |

데몬은 모델 생존 확인·강제 취소를 하지 않는다. [선점 운영 안내](../operations/decision-claim.md) 참조. 새 경로도 같은 gate 잠금·소유권을 검사한다. 기존 `conversation-preflight`/`gate --apply-claimed`는 호환·복구용이며 정상 새 경로와 섞지 않는다.

## 준비 한 호출

아래 session 명령은 모두 `python3 decision_session.py --config automation.json --wake-id <wake_id>` 뒤에 붙인다.

1. `automation-state/wake.json`의 `wake_id` 확인. `controller_error`/`poller_stale`/`poller_alert`이면 `runbooks/repair.md`의 운영 확인부터 한다. claim이 이미 started를 기록했으므로 재선점하지 않는다.
2. `prepare --decision-format compact-v1` 실행. **기존 decision_session 진입이며 파일 인계 실행기를 사용하지 않는다.** `decision_generation`, `decision_contract`, `snapshot`, `contexts`, `documents`, `constraints`를 읽는다.
3. 이번 판단·재조회·재시도·prepare 재호출에는 같은 `turn_id`를 유지한다. 실제 읽은 CORE_RULES hash만 `--known-core-rules-hash <hash>`로 전달한다.
4. `snapshot.work_manifest`의 경로를 따른다. DM의 `life_required`는 같은 판단의 필수 경계만 처리한다. 생략 필드는 같은 turn_id의 기존 life/full tick으로 조회 가능. `has_more`는 같은 revision으로 페이지를 잇고 실제 처리한 event만 결정에 넣는다.

| 준비 항목 | 규칙 |
|---|---|
| 입력 모드 | 기본 `--mode auto`: 중복 제거 후 UTF-8 48 KiB 이하이면 `input_mode=bundled`. 초과/명시적 `--mode compact`는 아래 분리 조회. 크기는 모드 선택 권고이며 조회 거절 기준이 아님 |
| 원문·지침 | `snapshot.messages[].body_ref`는 contexts의 동일 ID·본문을 참조. documents에는 처음/변경된 CORE_RULES·선택 runbook·필요한 생활 경계 지침 포함. 기타 주제별 필수 문서도 읽음 |
| 미리 아는 검색어 | `prepare --recall @queries.json`. 예: `[["topic_state:book"],["identity:education"]]`. 최대 8개 조회, recall당 기존 10개 제한. 필요한 검색만 선택; 무매칭을 전체 기억 부재로 단정하지 않음 |
| compact 준비 뒤 추가 검색 | 같은 `input_id`의 `read-recall --keywords ...`. 기억 조회만을 위한 재prepare 불필요 |
| 최근 대화 | 화자·순서를 보존한 기본 30개와 더 오래된 미처리 대상. 약속 시각·장소·계산 주체 등 기존 합의를 뒤집지 않도록 끝까지 읽음. 다른 범위는 `--context-limit <6~30>`으로 재준비 |
| 대화 확인 | `contexts.<channel>.ready`, `missing_target_ids`, 파일 hash/fetched_ms 확인. DB ingest·대화 파일은 비원자적. 불확실하면 재준비 또는 기존 defer/운영 확인 |
| 외부 변경으로 revision 변경 | prepare부터 재판단. 준비 JSON의 revision만 교체 금지 |
| 구버전/full 출력 | `--decision-format`을 빼고 아래 full 계약 사용 |

prepare는 스냅샷 전에 독립 수집 inbox의 작은 결과 묶음을 선택적으로 게시한다. 게시 실패·DB 잠금·캐시 손상에도 답장 준비를 계속하며 검색 도구·수집 완료를 기다리지 않는다.

대면 중이거나 약속 장소에 도착해 candidate_appointment_id가 있으면 interaction과 함께 전달된
design/in-person.md를 따른다. candidate는 대면 확정이 아니며 실제 합류 근거를 읽고 start한다.
appointment_prepare_due/appointment_review_due도 기존 생활 경계다. 예정 종료 시각만으로 작별하지 않는다.

## 큰 입력의 분리 조회

`input_mode=compact`의 첫 출력은 목록·ID·메타데이터다. `documents={}`, `recalls=[]`는 자료 부재가 아니다.
같은 turn_id와 이번 prepare의 input_id로 `read-bundle --turn-id <turn_id> --input-id <input_id>`를 호출한다. 필수 상태·지침·준비된 최근 대화·요청 기억의 `components` 본문을 읽고 `input_receipts` 배열을 그대로 commit에 제출한다.
추가 기억·선택 자료는 개별 조회한다. 실제 전달 한계에 맞춰 처음부터 개별 조회를 선택해도 된다. 권고 크기 초과는 표시하되 본문은 자르지 않는다. [분리 조회 지침](runbooks/prepared-inputs.md) 참조.

아래 명령에는 공통으로 `--turn-id <turn_id> --input-id <input_id>`를 붙인다.

| 명령 | 필수 범위·선택 방법 |
|---|---|
| `read-state` | `input_manifest.required_state_sections` 필수. `state_sections`는 선택 자료 포함 전체 목록이므로 무관한 배경 전량 조회는 불필요. 큰 상태는 `--section <항목>` 반복, 목록은 한 항목씩 `--offset 0 --limit 10` 등으로 끝까지 읽음. 미확인 페이지를 처리 완료로 표시 금지 |
| `read-document --name <경로>` | `input_manifest.documents` 전부와 조건에 맞는 주제별 필수 문서. CORE_RULES는 known hash만으로 대체 불가 |
| `read-context --channel <채널> --offset 0 --limit 30` | `available_message_ids` 순서로 페이지 조회 또는 `--message-id <ID>` 반복. 최근 `context_limit`개(기본 30, 조회당 최대 30)·실제 대상·검토 인용 ID 모두 읽어야 답장/재승인 가능. `ctl conversation-context`의 사용/공유 이력은 대화 원문을 대신하지 못함 |
| `read-recall --index 0` 또는 `read-recall --keywords <검색어...>` | prepare의 선택적 recall과 같은 revision 검사. 기존 topic_state 조회·의미 검토 유지. 일반/다중 키/topic_state 기본 10건; 현재 상태 1건만 필요하면 `--limit 1`, 이력은 `--offset` |

**읽기 확인·재사용**

- 성공 응답의 `content`를 실제 읽고 받은 `receipt`만 JSON 문자열 배열로 모아 commit에 `--input-receipts @receipts.json` 제출. 읽기 확인용 별도 ACK 호출 없음. 미전달 출력·trace finished만으로 읽기나 receipt를 복구하지 않음.
- bundled prepare/read-bundle의 `document_receipts`는 문서 이름→토큰이다. 실제 읽고 계속 보유한 동일 hash 문서의 토큰 값 배열을 `--document-receipts @docs.json`으로 다음 prepare/read-bundle에 제출하면 본문 재전달을 생략한다.
- 재사용은 같은 wake만 허용. 실제 문서 receipt가 `reusable_receipt=true`이면 새 턴에도 제출 가능. 읽지 않음·컨텍스트 유실·다른 wake이면 재사용 금지.
- prepare 성공: 새 input_id·상태/문맥 확인값 발급, 동일 문서 receipt 유지. 실패: 이전 준비 보존하되 원래 소유권·revision·문맥 검사도 유지; 자동 유효 판정 아님. `prepare_inputs_changed`는 재준비·재판단.
- 같은 input_id에서 일부만 읽었거나 commit에 receipt가 빠졌으면 `read-bundle --turn-id TURN --input-id INPUT --input-receipts @receipts.json`으로 보충한다. 실제 읽고 계속 보유한 receipt만 제출하며, 반환된 합친 `input_receipts`를 다음 commit에 사용한다. 이미 확인한 상태 절·문서·대화 메시지·동일 검색은 재전달하지 않는다. 준비가 바뀌면 상태·대화·검색 receipt는 재사용할 수 없다.

**입력 범위·오류**

- 모드 선택은 크기 기준이다. `wake.attempt`는 검사 차단의 증거가 아니며 실패 횟수로 필수 지침·원문 생략 금지.
- 각 조회도 플랫폼 검사 대상. 거절 자료를 변형·더 잘게 분할해 재요청하지 않고 기존 보고·인계 사용.
- 출력 크기·조회 횟수는 권고/관측값이며 초과만으로 실패하지 않음. 원문·기억 details는 그대로 반환.
- 선택 범위 밖 옛 캐시 내용 변경은 재준비를 강제하지 않음. 새 수신·선택 메시지 변경은 재평가.

## 검증과 저장 한 호출

`constraints.reply_generator.enabled`에 따라 `generate-reply --turn-id TURN --request @gen-request.json`을 사용한다. compact면 실제 문맥 receipt도 전달한다. 생성 문장을 검토하고 해당 DM의 `generator_id`로 연결한다. 기존 full/compact-v1 commit을 유지하며 파일 인계로 전환하지 않는다.

초안·근거·반복/자기 일관성 검토·conversation_plan은 기존 계약을 따른다.

| compact-v1 작성 주체 | 필드·규칙 |
|---|---|
| 모델 | 새 reply/initiate의 conversation_review에는 `previous_self/user_uptake/topic/memory/checks`만 작성. `message_ids`·`channel` 명시. 검토 통과·근거·계획·생활 판단도 모델 담당 |
| 코드 | 중복 `context/draft_parts`, `decision_id/expected_revision`은 모델이 생략. 코드가 전달 문맥·선택 대상·초안 사본·준비 revision을 연결 |
| 현재 준비 | `decision_generation`은 입력 준비 식별자이며 읽기 receipt를 대신하지 못함. 새 prepare 성공 후 옛 토큰 사용·재준비 전 초안을 새 revision에 자동 연결 금지 |

```sh
python3 decision_session.py --config automation.json --wake-id <wake_id> commit \
  --turn-id <turn_id> --decision-format compact-v1 --decision-generation <decision_generation> \
  --decision @decision.json
```

분리 입력이면 `--input-receipts @receipts.json`도 추가한다. 필요한 API 절은 `read-decision-schema --turn-id <turn_id> --decision-generation <decision_generation> --topic activities` 등으로 원문 조회한다. [출력·복구 계약](runbooks/session-compact.md) 참조.

full은 snapshot.revision·고유 decision_id를 포함한 전체 결정으로 `commit --turn-id <turn_id> --decision @decision.json` 실행(위 session 공통 접두사 사용).

| 저장 결과 | 다음 행동·금지사항 |
|---|---|
| 무경고 정상 | 같은 DB transaction에서 기존 검증 후 저장. 별도 conversation-preflight·gate --apply-claimed 반복 불필요 |
| `reason=warning_review_required`, `committed=false` | 미저장. `commit.conversation_groups`의 정규화 계획·경고와 기존 수정 정책 검토. 초안을 고치거나, shadow 정책상 동일 초안을 허용할 때만 `--warning-review <commit.warning_review>`로 같은 commit. 자동 토큰 전달·다른 초안/경고에 재사용 금지 |
| 하드 오류 | 저장 차단. `activity_unavailable`은 실제 활동·답장 순서 재판단. 가드 통과용 attention 변경 금지. next_decision은 자동 상태 전환이 아님 |
| 오류의 `recovery.next=correct_and_retry_commit` | 같은 준비에서 오류 필드·판단을 검토하고 수정해 commit. 형식 오류만으로 prepare·전체 문서 읽기·성공한 생성기 호출을 반복하지 않음. 의미 오류는 근거와 초안도 재검토하며 기존 검증 유지 |
| `complete_inputs_and_retry_commit` | 실제 보유한 receipt를 제출하거나 위 read-bundle로 빠진 입력만 읽고 같은 준비에 commit. 읽지 않은 자료의 receipt 추측 금지 |
| `retry_same_commit` | 일시 I/O·busy이면 같은 결정 ID·내용으로 재시도. 성공 여부가 불명인 결정을 수정하거나 새 답장 생성 금지 |
| `inspect_error` | 잘못된 turn/generation·ID 충돌·캐시 불일치 확인. 토큰이나 revision을 추측해 교체하지 않음 |
| `prepare_and_review_changes` (`revision_conflict`, `episodic_reprepare_required`, `character_reprepare_required` 등) | 같은 turn으로 prepare부터 확인. 오류 `recovery`/prepare `recovery_candidates`의 초안은 `read-draft --draft-id <id>`로 읽고 새 입력·상태에 유효한 부분만 재사용. revision 교체·옛 warning-review/상태/문맥 receipt 재사용 금지 |
| `10/stale_claim` | 소유권 상실: 종료. 다음 소유자가 보존 초안 검토 가능 |
| `committed=true` | 저장 완료. 발송/대기 오류로 답장 재생성 금지. `after.next=finish`는 같은 turn의 `finish --turn-id <turn_id>`로 후처리만 재시도. `retry_same_commit`은 같은 ID·내용의 원본 결정으로 commit 재시도해 메타데이터 복구; DB가 중복 저장 방지 |
| prepare의 `finish_required` | 이전 턴 저장이 끝났고 후처리가 남음. `recovery.turn_id`의 finish를 실행하고 반환 next를 따름. 새 입력 준비로 기존 턴을 덮어쓰지 않음 |

복구 안내는 다음 행동을 구분하며 재제출의 성공을 보증하지 않는다. 입력이 실제로 바뀌면 다음 검증에서 재준비를 요구한다. 코드가 기억·정책의 의미 판단을 대신하거나 검사를 생략하지 않는다. 다른 턴·wake의 보관 초안을 꺼내는 read-draft는 기존대로 새 입력과 대조한다.

## `proactive_check_due`만(또는 포함) 깨어난 경우

`needs_decision`에 `proactive_check_due`가 있으면 최근 Slack 스레드와 관계 스냅샷을 읽고 `design/autonomy.md`의 선제 DM 기준으로 initiate 여부를 판단한다. 보내지 않더라도 apply(또는 필요한 no-op apply)로 저장해 컨트롤러가 다음 검사 시각을 밀도록 한다. 이 wake만으로 생활 활동을 바꾸거나 지어내지 않는다. 주제·횟수 목표를 새로 만들지 않는다.

## 저장 후 처리와 다음 단계

commit이 현재 상태를 재확인하고 저장 그룹의 실제 채널 transport로 send-due를 호출한다. Slack 리스너·최근 대화 신선도 확인, missing config의 mock 대체 금지. busy/send_interval·뒤 파트는 기존 폴러에 맡기며 불명 발송은 재전송하지 않는다.

live이고 명시적인 대화 종료·30분/20턴 제한에 해당하지 않으면 코드가 최대 600초 wait-dm으로 기다린다. 실제 대화를 마무리하기로 판단한 경우에만 commit에 `--conversation-ended`를 붙인다. 유진은 persona.py에 따라 시각만으로 야간 quiet를 강제하지 않는다. focused/unavailable은 기본 quiet지만, 실제 대면의 available 상태이면 해당 채널은 focused여도 live다. 활동 attention을 가드 통과용으로 바꾸지 않는다. 수면·unavailable은 유지하며 긴 wait 동안 gate/DB 잠금을 유지하지 않는다.

| 다음 상태 | 행동 |
|---|---|
| `after.next=prepare` | 새 DM/필수 판단/복구/조용한 background 한 단위 필요. prepare로 새 turn_id·경로 수신. 저장한 결정 재작성 금지 |
| `after.next=done` | ACK finished 완료: 종료. 늦은 입력·남은 backlog는 보존되어 다음 gate가 처리 |
| prepare가 idle 또는 background/복구 확인 뒤 추가 저장 불필요 | `close --turn-id <turn_id>` 실행 후 반환 next 따름. 새 DM/필수 생활 판단 발견 시 prepare로 돌아가며, 해결 못 한 발송 unknown은 원장에 남기고 종료 가능 |

background는 조용할 때 한 단위만 확인한다. 누락 프로필·원래 due를 완료로 위장하지 않는다. 해결 불가 장애는 기존 운영 보고 후 `gate --ack-phase failed`로 인계; 단순 비긴급 잔여 작업은 failed 사유가 아니다.

**실행 기록**

- wake/turn별 `session_prepare`, `input_read_started/finished/failed`, `input_acknowledged`, `apply_started/finished`, `session_send`, `session_after`, `session_wait_finished`, ACK 기록. 분리 조회 read_id로 시작·종료 연결.
- finished는 서버 출력 생성 완료이며 플랫폼 전달 성공이 아님. `apply_finished exit_code 0`만 성공 저장, 경고 rollback은 4(도구 응답은 정상). session_after는 실제 mode·종료/대기 이유 기록.
- trace에 원문·검색어·확인값·결정 JSON 금지. claim→성공 apply, apply→첫 confirmed, 다음 DM→판단 시작을 각각 측정.
