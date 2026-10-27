# Persona DM Controller

LLM이 연기하는 가상 인물이 실제 메신저 DM으로 대화하고, 자기 생활(활동·일정·기억)을 이어가도록 만드는 **장기 실행 에이전트의 제어 계층**입니다.
모델은 "무엇을 할지"를 판단하고, 이 컨트롤러는 그 판단이 **정확히 한 번, 올바른 순서로, 검증된 상태에서만** 반영되도록 보장합니다.

> 예시 인물 `유진`은 가상의 대학원생 게임 친구입니다. 인물 설정은 `controller/design/`의 문서만 바꾸면 교체할 수 있습니다.

**English summary.** A control layer for a long-running LLM agent that plays a fictional character over real messenger DMs and keeps its own simulated daily life (activities, schedules, memories). The model decides *what* to do; this controller makes sure each decision is applied exactly once, in order, and only against validated state — with SQLite-backed revision checks, delivery leases, evidence-based handling of uncertain sends, and measured memory retrieval. Python 3.11+, standard library only.

## 왜 만들었나

LLM에 DM 답장을 맡기는 것 자체는 쉽습니다. 어려운 건 그 뒤입니다.

- 모델이 답장을 쓰는 동안 새 메시지가 오면? 같은 메시지에 두 번 답하면?
- 전송이 타임아웃으로 끝났는데 실제로는 나갔다면? 다시 보내면 중복입니다.
- 어제 한 말과 오늘 한 말이 다르면? "지난번에 말한 그거"를 모델이 기억하지 못하면?
- 모델이 "보냈다", "저장했다"고 말했지만 실제로는 실패했다면?

이 프로젝트는 이런 문제를 **모델의 성실함에 맡기지 않고 코드와 데이터베이스 제약으로** 해결하는 데 집중했습니다.

## 구조

```
메신저 수신 ─▶ ingest ─▶ SQLite 원장(store.py) ◀─ apply(결정 검증·원자적 반영)
                              │                          ▲
                              ▼                          │
                         gate.py (깨울지 판단)          decision_session.py
                              │                     (입력 준비 → 모델 판단 → 검증 → 커밋)
                              ▼                          │
                        모델 워커(외부 LLM) ─────────────┘
                              │
                    dispatch.py (발송 lease·순서·재시도) ─▶ 메신저 전송
```

| 영역 | 주요 모듈 | 하는 일 |
|---|---|---|
| 상태 원장 | `store.py`, `schema/` | 활동·일정·사실·DM 작업을 SQLite에 저장. revision 충돌 시 결정 전체 롤백 |
| 발송 | `dispatch.py`, `delivery_flow.py`, `slack_delivery.py` | OS 파일 잠금 + 작업 lease, 429 차단, 결과 불명(`unknown`) 보류와 증거 기반 정리 |
| 깨우기 | `gate.py`, `doorbell.py`, `wake_*.py`, `worker_budget.py` | 모델 호출 없이 "지금 판단이 필요한가"를 결정. 하나의 고정 시간 예산 안에서 claim·발송·대기 |
| 판단 세션 | `decision_session.py`, `prepared_inputs.py`, `muse_executor.py` | 모델에게 줄 입력을 준비하고, 읽었다는 receipt가 있는 입력만 판단 근거로 인정 |
| 역할 분리 | `role_executor.py`, `role_contract.py`, `role_preflight.py` | 생활 판단 → 대화 방향 → 문장 작성 → 검토를 서로 다른 모델 역할로 분리하고 계약으로 연결 |
| 답장 생성 | `reply_generator.py`, `model_adapter.py` | 문장만 쓰는 모델. 출력 형식 검증, 재시도 정책, 프롬프트 캐시 고정 prefix |
| 기억 | `episodic_memory.py`, `memory_continuity.py`, `memory_organizer.py`, `memory_origins.py` | 배경 사건 기억, 출처 추적, 버전이 있는 구조화 기억 |
| 대화 품질 | `conversation_flow.py`, `repetition_cues.py`, `continuations.py` | 화제 반복 방지, 자기 발언 일관성, 사용자가 요청한 "이어서 보내기" |
| 인물 문서 | `character_documents.py`, `character_brief.py`, `character_editor.py` | 사건이 쌓이면 인물 설명을 버전 관리하며 갱신 |

## 설계에서 신경 쓴 점

**1. 모델 출력은 제안일 뿐, 반영은 코드가 한다.**
모델은 JSON 결정을 제출하고, `store.apply`가 활동 시간 충돌·고정 일정·대상 메시지 ID·초안 일치 여부를 검사합니다. 하나라도 어긋나면 결정 전체가 롤백되고, 모델에게 구조화된 오류가 돌아갑니다.

**2. "보냈다"는 증거가 있을 때만 사실이 된다.**
발송 결과는 `confirmed / submitted / unknown / failed`로 구분합니다. `unknown`은 자동 재전송하지 않고, 확인된 발송만 "내가 한 말" 기억(`self-said:`)이 됩니다. 실패가 확실히 증명된 경우(`not_sent: true`)에만 다시 시도합니다.

**3. 판단 도중 들어온 메시지를 잃지 않는다.**
입력을 준비한 뒤 새 메시지가 오면 revision이 바뀝니다. 새 수신만으로 생긴 revision 변화는 `revision_sources`로 구분해 기존 답장은 그대로 보내고, 늦게 온 메시지는 다음 판단에서 답합니다(`late_incoming_ids`).

**4. 기억 검색은 측정한다.**
배경 사건 기억은 BM25 + 중요도 감쇠 + 최근성 감쇠로 순위를 매기며, `tools/`의 오프라인 recall 평가로 변경 전후를 비교합니다.

**5. 모든 확장은 기본 꺼짐.**
역할 분리, 배경 기억, 반복 방지, 인물 문서 갱신은 모두 설정으로 켜는 기능입니다. 기존 경로를 바꾸지 않고 shadow 모드로 먼저 관측한 뒤 enforce로 전환할 수 있습니다.

## 실행

Python 3.11+ 표준 라이브러리만 사용합니다(Slack 연동만 별도 의존성).

```sh
cd controller
python3 -m unittest discover -s tests      # 전체 테스트
python3 demo.py                             # 임시 DB에서 생활·답장 흐름 시연 (실제 전송 없음)
python3 ctl.py --db demo.db seed --import @examples/seed.json
python3 ctl.py --db demo.db tick --session main
python3 ctl.py --db demo.db status --session main --human
```

답장 생성기와 외부 모델 역할은 기본 꺼짐입니다. 켤 때는 OpenAI 호환 chat completions 주소를 설정의 `endpoint`에 지정해야 하며, 내장된 기본 제공자는 없습니다. 주소가 없거나 잘못되면 모델 호출 전에 `model_endpoint`·`invalid_model_endpoint`로 거절합니다.

```json
{"reply_generator": {"enabled": true, "endpoint": "https://api.example.com/v1/chat/completions",
                     "model": "<model-id>", "key_file": "~/.config/persona/key", "rules_file": "<rules.md>"}}
```

기억 평가 도구는 `tools/`에 있습니다.

```sh
python3 -m unittest discover -s tools -p 'test_*.py'
```

## 문서

- [API.md](controller/API.md) — 모델이 제출하는 결정 JSON 계약
- [CORE_RULES.md](controller/CORE_RULES.md) — 인물 공통 판단 원칙
- [runbooks/](controller/runbooks) — 상황별(DM·생활·복구) 모델 지침
- [design/](controller/design) — 인물 설정과 대화 규칙

## 범위

이 저장소는 개인 프로젝트에서 컨트롤러 코어만 분리한 공개용 버전입니다. 운영 설정, 인증 정보, 실제 대화 기록은 포함하지 않습니다.
