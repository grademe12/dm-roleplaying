# 기존 판단 세션의 compact-v1 출력

이 경로는 현재 CRON_RUNBOOK의 decision_session.py prepare/조회/commit을 그대로 사용한다.
muse_executor.py의 파일 입력·응답 봉투·prepare/respond/recover를 사용하지 않는다.
입력의 bundled/compact 선택과 **출력 형식** compact-v1은 독립된 옵션이다.

## 준비와 읽기

```sh
python3 decision_session.py --config automation.json --wake-id WAKE prepare --decision-format compact-v1
```

반환된 turn_id, decision_generation, decision_contract와 원래 상태·문서·문맥을 읽는다.
decision_generation은 성공한 prepare마다 바뀐다. 실패한 재준비는 이전 준비와 토큰을 보존한다.
bundled는 직접 반환한 문맥 ID만 복사 대상으로 삼는다. input_mode=compact는 기존 read-bundle/개별 조회를
그대로 사용하고, 실제 전달받아 commit에 제출한 receipt의 문맥 ID만 복사한다. 서버 생성 로그로 읽기를 대신하지 않는다.

## 모델이 작성하는 것

- advance_to_ms/next_decision_ms, 처리한 이벤트, 활동·사실·기억·화제·계획·의도는 기존처럼 판단한다.
- reply/initiate의 action/channel/대상 message_ids/group_id/due_ms/parts/reason과 필요한 부가 필드를 작성한다.
  initiate에는 message_ids를 넣지 않는다. 새 parts는 텍스트·이미지 합계 1..5개다.
- 새 reply/initiate의 conversation_review는 previous_self/user_uptake/topic/memory/checks 다섯 필드다.
  previous_self는 모델이 선택한 {message_id, point} 또는 null이다. 검토 통과·주장 근거·대화 계획을 코드가 채우지 않는다.
- 중복 context/draft_parts와 최상위 decision_id/expected_revision은 생략한다. 임의로 제출하면 거절한다.
- revalidate_group의 review는 기존 전체 형식이다. 보관된 초안 내용을 바꾸지 않는다.

코드는 선택 대상·최종 parts의 사본과 전달된 해당 채널 문맥의 최대 30개 ID를 연결한다.
이전 자기 발언이 오래되었으면 실제 읽은 범위 안에서만 포함한다. 답장·원문·검토 의미를 요약하거나 잘라내지 않는다.

## 저장·경고·복구

```sh
python3 decision_session.py --config automation.json --wake-id WAKE commit \
  --turn-id TURN --decision-format compact-v1 --decision-generation GENERATION --decision @decision.json
```

분리 입력이면 기존 --input-receipts @receipts.json을 추가한다. 이 둘은 서로 대체하지 않는다.
코드는 같은 wake/turn/prepare 토큰인지 확인하고 조립 결과를 비공개 compact-decisions/WAKE 파일에 먼저 보관한다.
최종 gate 잠금 안에서 토큰과 입력을 다시 검사한 뒤 기존 원자적 Store apply를 실행한다.
새 수신·원문 변경·DB revision 변경은 다시 준비한다. 토큰·revision만 새 값으로 바꿔 이전 초안을 적용하지 않는다.

warning_review_required는 여전히 미저장이다. 경고를 모델이 검토한 뒤 동일한 원본 compact JSON과
토큰으로 --warning-review를 제출한다. 추가 receipt 때문에 미저장 조립 메타데이터가 달라지면
기존 승인 토큰은 새 결정/경고에 통하지 않는다. 하드 검사·확정 발송 멱등성은 그대로다.

committed=true 이후 finish 오류는 기존 finish 명령을 쓴다. retry_same_commit은 **같은 원본 compact JSON과
같은 decision_generation**으로 commit을 재시도한다. DB에 이미 저장된 정확한 조립 결과를 복구하며
옛 receipt·현재 입력을 끼워 새 답장을 생성하지 않는다. 복구가 끝난 턴에 새 결정을 제출하는 방식은 아니다.
미저장 후보는 같은 현재 준비에서 새로 제출한 receipt에 맞춰 기계적 메타데이터를 다시 구성할 수 있다.
소유권·기한·신선도·전체 receipt 검증은 그대로 적용된다.

원본 모델 파일은 덮어쓰지 않는다. compact-decisions에는 답장·근거가 들어가므로 운영 DB와 같은 비공개 자료로
취급하고 Git에 넣지 않는다. 진행 중인 wake의 복구 파일을 지우지 말고 종료·감사 후 기존 운영 보존 정책으로 정리한다.

## 필요한 API 절만 읽기

```sh
python3 decision_session.py --config automation.json --wake-id WAKE read-decision-schema \
  --turn-id TURN --decision-generation GENERATION --topic memory --topic intents
```

topics는 activities/facts-plans/dm-actions/continuations/media/feed/intents/memory다.
준비 때 생활·수리·피드·기한 의도/이어 보내기에 필요한 절은 자동 포함한다. 추가 계약만 이 명령으로 읽는다.
원본의 해당 절을 그대로 반환하며 전체 API 문서 소비 receipt를 만들지 않는다.
원본 API hash를 신선도 의존성으로 연결해 변경 시 재준비를 요구한다.
구조를 넘어 실제 주제 지침·증거가 필요하면 기존 문서·기억 조회도 수행한다.

## full 호환과 롤백

코드 기본값은 full이다. compact 토큰·JSON을 기존 full commit에 섞지 않는다.
기존 full 진입과 저장·발송·대기 정책은 유지한다. 새 wake에서 prepare 옵션을 빼고 전체 JSON 계약으로 돌아갈 수 있다.
진행 중 compact 결정은 먼저 새 코드로 저장 여부와 후처리를 정리한다. 이후 CRON_RUNBOOK의 prepare/commit에서
compact-v1 관련 옵션을 제거하고 full 계약을 따른다. 운영 DB 복원·발송 상태 변경·실행기 활성화는 필요하지 않다.
