# Persona DM Controller

LLM이 연기하는 가상 인물이 실제 메신저 DM으로 대화하면서 자기 생활(활동, 일정, 기억)을 이어 가게 하는 프로그램의 제어 계층입니다. 무엇을 할지는 모델이 정하고, 그 결정을 상태에 반영하고 메시지를 보내는 일은 이 코드가 맡습니다.

LLM에게 답장을 쓰게 하는 것 자체는 쉽습니다. 문제는 그 뒤에 생깁니다. 답장을 쓰는 사이 새 메시지가 오거나, 같은 메시지에 두 번 답하거나, 전송이 타임아웃으로 끝났는데 실제로는 나갔거나, 모델이 "보냈다"고 했는데 실패한 경우입니다. 이 프로젝트는 이런 경우를 모델에게 맡기지 않고 코드와 데이터베이스 제약으로 막는 데 집중했습니다.

예시 인물 `유진`은 가상의 대학원생입니다. 공개용으로 컨트롤러 코어만 분리한 저장소라 운영 설정, 인증 정보, 실제 대화 기록은 들어 있지 않습니다. 프로젝트 소개: <https://woosupar.dev/projects/dm-roleplaying/>

## 구조

```text
메신저 수신 ─▶ ingest ─▶ SQLite 원장 (store.py) ◀── apply: 결정 검증 후 한 번에 반영
                              │                         ▲
                              ▼                         │
                        gate: 지금 모델을 깨울지      decision_session:
                        (모델 호출 없이 판단)         입력 준비 → 모델 판단 → 검증
                              │                         │
                              ▼                         │
                         모델 (외부 LLM) ───────────────┘
                              │
                     dispatch: 발송 순서, 잠금, 재시도 ─▶ 메신저 전송
```

- **모델 출력은 제안입니다.** 모델은 JSON 결정을 내고, `store.apply`가 활동 시간 충돌, 고정 일정, 대상 메시지, 초안 일치 여부를 검사합니다. 하나라도 어긋나면 결정 전체를 되돌리고 이유를 돌려줍니다.
- **"보냈다"는 증거가 있을 때만 사실입니다.** 발송 결과를 `confirmed`, `submitted`, `unknown`, `failed`로 나눕니다. `unknown`은 자동으로 다시 보내지 않고, 확인된 발송만 인물이 "한 말"로 기억합니다.
- **판단 중에 온 메시지를 잃지 않습니다.** 입력을 준비한 뒤 새 메시지가 오면 상태 revision이 바뀝니다. 이미 쓴 답장은 그대로 보내고, 늦게 온 메시지는 다음 판단에서 답합니다.
- **기억 검색은 잽니다.** 배경 사건 기억은 BM25에 중요도와 최근성 가중치를 더해 고르고, `tools/`의 오프라인 평가로 변경 전후 recall을 비교합니다.

| 디렉터리 | 내용 |
|---|---|
| `controller/` | 컨트롤러 코드와 테스트. 진입점은 `ctl.py` |
| `controller/design/` | 인물 설정과 대화 규칙. 이 문서만 바꾸면 다른 인물이 됩니다 |
| `controller/runbooks/`, `CORE_RULES.md` | 상황별로 모델에게 주는 지침 |
| `controller/API.md` | 모델이 내야 하는 결정 JSON의 형식 |
| `operations/` | 운영 기록. [토큰 사용량 실측 분석](operations/token-usage-analysis.md) 포함 |

## 써 보려면

Python 3.11 이상, 표준 라이브러리만 씁니다. Slack 연동만 `slack_sdk`가 필요합니다.

```sh
cd controller
python3 -m unittest discover -s tests                        # 전체 테스트

python3 ctl.py --db demo.db seed --import @examples/seed.json  # 인물과 초기 상태
python3 ctl.py --db demo.db ingest --session main --batch @examples/ingest.json   # 예시 DM 수신
python3 ctl.py --db demo.db status --session main --human    # unhandled=1 이 보이면 정상
python3 ctl.py --db demo.db tick --session main              # 모델에게 넘길 현재 상태(JSON)
```

`tick`이 출력한 상태를 보고 모델이 결정 JSON을 만들고, 그 결정을 `ctl.py apply`로 넣는 것이 한 번의 판단입니다. 결정 형식은 [`API.md`](controller/API.md)에 있습니다. `ctl.py --help`로 전체 명령을 볼 수 있습니다.

### 알아 둘 것

- **실제 메시지는 기본으로 나가지 않습니다.** 발송기(`transport`)는 꺼진 상태로 시작합니다. 실제 메신저로 보내려면 발송 설정을 켜야 하고, Slack은 [`slack/SETUP.md`](controller/slack/SETUP.md)를 따릅니다.
- **답장을 쓰는 모델은 직접 연결합니다.** 내장된 기본 제공자는 없습니다. OpenAI 호환 chat completions 주소와 모델, 키 파일 경로를 설정에 넣어야 켜집니다.
  ```json
  {"reply_generator": {"enabled": true, "endpoint": "https://api.example.com/v1/chat/completions",
                       "model": "<model-id>", "key_file": "~/.config/persona/key", "rules_file": "<rules.md>"}}
  ```
- **확장 기능은 모두 꺼진 채 시작합니다.** 역할 분리, 배경 기억, 반복 방지, 인물 문서 갱신은 설정으로 켭니다. 대부분 결과를 기록만 하는 shadow 모드로 먼저 켠 뒤 실제 반영(enforce)으로 바꿀 수 있습니다.
- **상태는 SQLite 파일 하나입니다.** `--db`로 준 파일을 지우면 인물의 기억과 일정이 모두 사라집니다. `ctl.py backup`으로 백업할 수 있습니다.
