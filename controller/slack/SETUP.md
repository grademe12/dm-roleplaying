# Slack 연결 순서

## 1. Slack 앱 설정 (api.slack.com/apps → yujin)

1. **Socket Mode**: Settings → Socket Mode에서 켠다. App-Level Token을 `connections:write` scope로 만들고(`xapp-...`), `~/.config/persona-slack/app-token`에 저장한 뒤 권한을 600으로 둔다.
2. **Event Subscriptions**: 켜고, Subscribe to bot events에 `message.im`을 추가한다.
3. **App Home**: Messages Tab에서 "Allow users to send ... messages from the messages tab"을 켠다.
4. **OAuth & Permissions → Bot Token Scopes**: `chat:write`, `im:history`, `files:write`. scope를 추가했으면 Reinstall to Workspace를 한다. 기존 bot token(`~/.config/persona-slack/token`)이 바뀌면 파일도 갱신한다.

## 2. 설치

```sh
cd ~/persona-controller/controller
python3 -m venv .venv-slack
.venv-slack/bin/pip install -r requirements-slack.txt   # slack_sdk==3.45.0
```

다음 파일을 controller에 둔다.
- `slack_listener.py`, `slack_send.py`
- `controller/`의 수정본 5개와 `tests/test_controller.py`
- 수정본 `poller.py`, `gate.py`

## 3. 설정

- `slack-send.json`, `transport-slack.json`: example 파일을 복사해서 만든다.
- `automation.json`에 `slack` 블록을 추가한다(`automation.example.json` 참고).
- `ingest_after_ts`: 지금까지 Hook이 직접 답장한 가장 최근 Slack 메시지의 ts로 정한다. 이보다 이전 메시지는 최근 대화 문맥으로만 쓰이고, 답장 대상이 되지 않는다.
- `backfill_seconds`: 이 프로젝트의 내부 앱 프로필은 `15`초다. DB의 `history_min_interval_ms`도 `15000`으로 맞춘다. 기존 운영 설정/DB의 60초 값은 새 기본값으로 자동 교체되지 않으므로 tools/configure_slack_polling.py와 2026-10-18/reply-latency/DEPLOY.md를 따른다. 외부 상용 배포 앱의 1회/분 제한이 적용되면 두 값을 60초 이상으로 유지한다.

## 4. 전환

1. `persona-slack-watch` Hook을 비활성화한다. 켜 두면 컨트롤러 밖에서 직접 답장이 나가서 중복되고, 기억에도 남지 않는다.
2. 테스트를 실행한다.
   ```sh
   python3 -m unittest discover -s tests
   CONTROLLER_CONTROLLER=$PWD python3 -m unittest discover -s slack/tests
   ```
   Slack 테스트는 가짜 Slack API를 쓰며 실제 Slack 토큰이나 외부 네트워크를 사용하지 않는다.
3. `systemctl --user enable --now persona-slack-listener.service`를 실행하고 `automation-state/slack_health.json`에서 `connected: true`, `last_backfill_success_ms`, `backfill_consecutive_failures: 0`을 확인한다. 이 수정본은 기존 `slack_seen.json`에 `backfill_cursor`가 없으면 `ingest_after_ts`부터 한 번 재조회한다. 이미 저장된 메시지는 ID로 중복 제거된다.
4. 폴러 timer를 재시작한다. 이번 수정본부터 폴러가 Slack 발송도 맡는다.
5. 별도의 `persona-send-only` 크론(send-due 전용)은 끈다. 폴러가 30초마다 두 채널을 순서대로 보내고, 대화 세션 중에는 Muse가 직접 보낸다. 크론을 남겨 두면 동시에 실행될 때 busy만 늘어난다.
6. Slack DM으로 메시지를 하나 보내서 두 가지를 확인한다.
   - tick의 `messages`에 `observation.channel: "slack"` 메시지가 생기는지
   - 답장이 Slack으로 나가는지

리스너 로그는 메시지 본문 없이 `source=socket|backfill`, Slack `ts`, 채널·발신자, 분류 결과와 제외 이유를 남긴다. `slack_health.json`의 `last_socket_message_ms`·`last_socket_outcome`·`last_ingest_success_ms`를 DB의 Slack 메시지 ID와 대조한다. 연결 하트비트만으로 ingest 성공을 판단하지 않는다. 실시간 ingest 실패나 이벤트 처리 예외가 발생하면 즉시 백필을 요청하며, 내부 앱 프로필의 정기 백필은 15초 간격이다. 실제 요청은 공용 history 예산·Retry-After·페이지 처리 시간에 따라 더 늦어질 수 있다.

2026-10-18 변경: 발송 확인용 자동 대범위 히스토리 조회는 제거했다. 일반 백필의 수신 복구·공유 예산·Retry-After는 유지한다. delivery worker는 기존/Socket 영수증만 처리하며, metadata-only 이벤트나 오래된 미확인 발송 때문에 history를 깨우거나 조회 범위를 되돌리지 않는다. [적용 안내](../../2026-10-18/remove-delivery-history-scan/DEPLOY.md)에 따라 listener와 worker를 함께 교체한다.

## 사용자 계정으로 바꾸려면

페르소나용 Slack 사용자 계정을 워크스페이스에 하나 만들고, 그 계정으로 앱을 설치해서 user token(`xoxp-`)을 받는다. User Token Scopes는 `chat:write`, `im:history`, `files:write`다. Event Subscriptions의 "Subscribe to events on behalf of users"에 `message.im`을 추가한다.

- **코드 변경**: 없다. 다음 설정만 바꾼다.
  - `slack-send.json`의 `token_path`와 리스너의 `bot_token_path`: user token 파일을 가리키게 한다.
  - `self_user_id`: 페르소나 계정의 user ID
  - `channel`: 페르소나 계정과 사용자 사이의 DM 채널 ID
- **확인할 것**: 사용자 대신 구독한 이벤트가 Socket Mode로 실제로 들어오는지는 설치 후 직접 확인해야 한다.

## Optional webhook nudge (default off)

After a new inbound DM is ingested, the listener can POST `{"session", "reason": "dm_ingested", "ts"}`
to a webhook so a decision runner wakes without polling. Add to `automation.json`:

```json
"nudge": {"enabled": true, "url_path": "/abs/nudge-url", "key_path": "/abs/nudge-key",
          "key_header": "Authorization", "min_interval_seconds": 20}
```

URL and key are read from files (never logged). `key_header` defaults to `Authorization`
(sent as `Bearer <key>`); any other header name gets the raw key. Timeout 5 s, at most one
nudge per `min_interval_seconds` (extra ones are dropped), runs in a background thread, and
failures are only logged — ingest is never blocked. Restart the listener to apply.
