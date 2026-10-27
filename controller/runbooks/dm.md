# DM 우선 실행

현재 interaction이 in_person이거나 약속 준비·도착·종료를 판단할 때는 [대면 대화](../design/in-person.md)를 함께 적용한다. 실제 합류 전에는 원격이며, 시간만으로 만남·작별을 확정하지 않는다.

연락은 모두 개인 DM(Slack)이다. 최근 대화의 장면과 현재 활동을 이어 받는다. 대면은 확정된 약속과 이동 근거가 있는 공동 장면에서만 사용하며 만남이나 사용자 반응을 대신 만들지 않는다. 답장 저장 결과에 `late_incoming_ids`가 있으면 저장한 답장은 그대로 두고 그 메시지는 다음 판단에서 답한다. 실제 채널·검토·발송 계약은 유지한다.

대화에서 새 주요 활동을 시작/변경해야 한다면 life view의 activity_history와 activity-selection.md를 먼저 대조한다. 평범한 답장마다 활동을 새로 뽑거나 생활 전체를 다시 계획하지 않는다.

unknown/submitted 발송은 영수증 확인을 뒤에서 계속하며 새 답장과 후속 파트를 막지 않는다. 이를 확인하려고 대화를 중단하거나 원문을 다시 보내지 않는다. completed 그룹도 실제 게시 확인은 jobs의 confirmed/provider_message_id로 구분한다.

사용자가 시간을 두고 계속 보내 달라고 요청했거나 snapshot에 active/paused continuations가 있으면 [이어가기 계약](continuation.md)을 함께 읽고 같은 판단에 처리한다. 새 메시지로 paused가 된 실행은 중단/화제 전환이면 stop하고, 계속할 상황에서만 새 generation으로 resume한다.

현재 attention과 수면 알림의 wake_allowed를 먼저 확인한다. unavailable이면 기존 수면/활동 기준으로 defer하며 초안을 쓰지 않는다. 활동 종료 등 life_required가 있으면 life-boundaries.md의 최소 정리를 같은 판단에 포함한다.

1. [문맥 검토](../design/reply-context.md)의 최근 대화·신선도·현재 상태 확인을 한다. prepare가 전달한 대화를 끝까지 읽고, 불확실하면 기존 defer/운영 확인을 따른다.
2. [기억 공통 계약](memory-continuity.md)에 따라 필요한 기억과 주제별 필수 topic_state·identity를 읽는다. 대화 맥락 기록·단서 해석·원문 조회·생성기 전달의 공통 규칙은 그 문서에서 관리한다. 이미 읽은 동일 해시의 지침은 기존 receipt 계약으로 재사용한다.
3. constraints.reply_generator.enabled=true이면 생성기 절차로 상황·기억·대상을 전달하고 generate-reply 결과의 parts와 generator_id를 사용한다. 실패(next=defer)는 기존 인계 정책을 따르며 직접 문장을 대체하지 않는다. 꺼져 있으면 Muse가 [공통 대화 규칙](../design/conversation-rules.md)에 따라 쓴다. [문맥 검토](../design/reply-context.md)의 기존 네 점검과 conversation_review를 완료한다. 일반 소재의 plan·evidence·preflight는 [conversation-initiative](../design/conversation-initiative.md)를 따른다.
4. 이번 대화의 핵심 사실·화제 변화·스스로 선택한 미래 행동은 같은 apply에 반영한다. 미래 약속은 필요한 action_intents로 연결한다. 실제 실행 전 진도를 완료로 기록하지 않는다. 활동/의도 결과와 직접 연관된 기억 갱신은 생략하지 않는다.
5. draft_stale/draft_needs_review가 있으면 이미 보낸 부분과 미발송 부분을 구분해 기존 revalidate_group/close_partial_group/cancel_group 계약을 따른다. 불명 발송은 repair.md의 증거 확인만 한다.
6. CRON_RUNBOOK.md의 decision_session commit으로 저장·재확인·발송·조건부 wait-dm을 한 번에 실행하고 반환된 next를 따른다. 경고이면 저장 전에 의미를 검토한다. 이 턴에서 새 프로필 조사·긴 요약 확장·뉴스 검색을 붙이지 않는다. 필요한 후속 작업은 원장에 남기고 완료로 위장하지 않는다.

답장 길이·말풍선 분할은 [공통 대화 규칙](../design/conversation-rules.md)을 따른다. 발송은 기본/최대 5개이며 같은 답장 안에서는 3~5초 간격이다. 다른 답장 사이 간격·새 수신 시 재검토·미확정 발송 처리는 기존 계약을 따른다.
