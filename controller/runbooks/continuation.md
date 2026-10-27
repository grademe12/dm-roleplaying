# 요청받은 이어가기

사용자가 시간을 두고 계속 보내 달라고 명시한 경우만 시작한다. 일반 선톡·한 답장의 여러 말풍선과 구분한다. Instagram/Slack 공통이며 기존 주제·사진·기억·검토 규칙을 매 그룹에 적용한다. 새 사진 정책이나 생성 작업을 만들지 않는다.

`continuation_ops`로 시작·보류·재개·종료하고, 각 `reply`/`initiate`에 `continuation:{"id":"run-id","generation":1}`을 연결한다. API.md의 혼합형 이어가기 계약을 따른다.

- 시작: 요청 메시지를 `source_message_id`로 남기고, 같은 apply에서 첫 `reply`와 후속 `initiate`를 합쳐 2~3그룹을 쓴다. 고유 그룹 ID와 그룹별 conversation_review/plan이 필요하다. 첫 reply를 먼저 연결해야 뒤 initiate가 그 reply의 queued 대상과 공존한다. 다른 미응답 수신은 먼저 처리한다.
- 간격: 기본 60초, `interval_ms`로 5초~1시간을 지정한다. 앞 그룹의 마지막 발송 시도 종료(confirmed/submitted/unknown)부터 잰다. 늦은 영수증으로 간격을 다시 시작하지 않는다. 파트 간 간격은 기존 계약이다. `due_ms`는 가장 이른 시작일 뿐이며 밀린 예약을 몰아서 따라잡지 않는다. quiet는 기존 채널 간격 이상으로 늦춘다.
- 보충: `continuation_due_ids`에 나오면 채널 문맥을 다시 확인하고, 활성 그룹이 2~3개가 되도록 새 그룹만 보충한다. 읽기 실패·할 말 없음·상황 변화이면 `defer`/`pause`/`stop`한다. 만기를 처리하지 않은 apply는 거절된다. 새 action_intent나 짧은 next_decision_ms 루프는 필요 없다.
- 새 수신: 어느 메신저에서든 같은 세션의 실행이 즉시 paused가 되고 미발송 파트가 취소된다. 중단·화제 전환이면 `stop`한다. 계속할 상황이면 `resume`에 새 메시지와 snapshot의 expected_generation을 넣고, 증가한 generation으로 새 그룹을 쓴다. 취소한 초안을 되살리지 않는다. 전혀 보내지 못한 reply 대상은 received로 돌아오므로 새 메시지와 함께 reply/no_reply로 처리한다.
- 실패·활동: 만료·실제 발송 실패·unavailable은 추가 발송과 보충을 멈춘다. unknown/submitted는 그대로 기록하고 다음 파트·그룹·보충을 계속한다. 이를 확인하려고 필수 판단이나 수동 해제를 기다리지 않는다. 실행을 다른 이유로 pause했더라도 unknown 영수증 없이 resume할 수 있고 실제 attempting 동안만 기다린다. 원문은 자동 재전송하지 않는다. 이어가기 소유 그룹에는 일반 revalidate/cancel/retry 대신 실행 단위 pause/stop을 쓴다.
- 상한: 기본은 사용자 중단까지다(expires_ms/max_groups 없음). 사용자가 시간·횟수를 정하면 해당 상한을 명시한다. max_groups는 취소한 초안도 포함한 총 생성 수다. 종료한 실행 ID는 재사용하지 않는다.

전송 중인 요청은 회수할 수 없다. 중단 직전 이미 claim된 한 파트의 결과가 뒤늦게 올 수 있지만 이후 파트는 발송하지 않는다.
