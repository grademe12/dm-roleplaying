# 배경 사건 기억 입력

설정이 enforce이면 prepare에 `episodic_memory[채널]`이 추가될 수 있다. 기본은 off다.
working은 현재 맥락, episode는 사건, fact는 지속 정보의 문맥이며 모두 context_only다.
선택된 요약·근거·시각·interpretation을 읽고 현재 대화와 함께 사용한다. 별칭·범주 기반 선택 결과이지 전체 기억이나 의미 검색 결과가 아니다.
`incomplete`, `pending_captured`, `recent_unprocessed_ids`, `last_capture_ms`, `coverage_since_ms`, `has_more`는 처리 범위와 지연을 보여준다.
본문이 없거나 일부만 있다는 것을 과거 사건이 없었다는 뜻으로 바꾸지 않는다. shadow는 운영 trace·별도 진단 조회에만 남고 판단과 생성기에는 기억·격리 진단을 주입하지 않는다.

compact에서는 read-state/read-bundle로 해당 본문을 읽고 receipt를 제출한다. 생성기는 같은 준비의 선택 결과를 자동으로 받는다.
주제별 topic_state·identity 필수 조회와 기존 memory_policy·scope·호칭 철회 규칙은 유지한다.
에피소드 ID는 Store의 fact_id가 아니므로 self_consistency.claims에 대신 넣지 않는다.
기존 conversation_frame과 배경 요약이 다르면 기록 시각만이 아니라 실제 근거와 현재 발언을 대조한다.
선택 기억을 받은 뒤 저장소 장애로 `episodic_reprepare_required`가 나오면 같은 wake에서 prepare를 다시 한다. 그 턴의 새 준비는 선택 기억을 빼고 만든다. 새 input_id·receipt로 다시 읽고 생성·검토하며 이전 초안을 보내지 않는다. 실제 정정·철회인 `memory_inputs_changed`는 기존 신선도 확인을 따른다.
운영 연결은 [배경 기억 안내](../../operations/episodic-memory.md), 정리 역할은 [에이전트 지침](memory-organizer.md)을 따른다.
