-- v0.1 신규 생활 저장소 초안. 기존 DB에 대한 마이그레이션이 아님.
-- 모든 *_ms는 UTC Unix milliseconds. 날짜 키는 KST.
-- 모든 연결에서 foreign_keys를 켜고 트랜잭션 종료 후 연결을 닫는다.
PRAGMA foreign_keys = ON;

CREATE TABLE sessions (
  id TEXT PRIMARY KEY,
  schema_version INTEGER NOT NULL DEFAULT 1 CHECK(schema_version = 1),
  revision INTEGER NOT NULL DEFAULT 0 CHECK(revision >= 0),
  advanced_to_ms INTEGER NOT NULL,
  next_decision_ms INTEGER,
  wake_mode TEXT NOT NULL CHECK(wake_mode IN ('on_input','scheduled')),
  location TEXT NOT NULL DEFAULT 'unknown',
  fatigue REAL NOT NULL CHECK(fatigue BETWEEN 0 AND 100),
  mood REAL NOT NULL CHECK(mood BETWEEN 0 AND 100),
  hunger REAL NOT NULL CHECK(hunger BETWEEN 0 AND 100),
  social_fatigue REAL NOT NULL CHECK(social_fatigue BETWEEN 0 AND 100),
  sender TEXT NOT NULL CHECK(length(sender) > 0),
  recipient TEXT NOT NULL CHECK(length(recipient) > 0)
);

CREATE TABLE input_events (
  session_id TEXT NOT NULL REFERENCES sessions(id),
  id TEXT NOT NULL,
  kind TEXT NOT NULL CHECK(kind IN ('dm_received','time_due','tool_result','resume')),
  received_ms INTEGER NOT NULL,
  processed_ms INTEGER,
  payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),
  PRIMARY KEY(session_id,id)
);

CREATE TABLE weekly_plans (
  session_id TEXT NOT NULL REFERENCES sessions(id),
  week_start TEXT NOT NULL,
  remote_date TEXT,
  seed TEXT NOT NULL,
  reason TEXT NOT NULL,
  PRIMARY KEY(session_id,week_start)
);

CREATE TABLE appointments (
  session_id TEXT NOT NULL REFERENCES sessions(id),
  id TEXT NOT NULL,
  start_ms INTEGER NOT NULL,
  end_ms INTEGER NOT NULL CHECK(end_ms > start_ms),
  status TEXT NOT NULL CHECK(status IN ('proposed','confirmed','cancelled','completed','missed')),
  fixed INTEGER NOT NULL CHECK(fixed IN (0,1)),
  location TEXT NOT NULL,
  details_json TEXT NOT NULL CHECK(json_valid(details_json)),
  PRIMARY KEY(session_id,id)
);

CREATE TABLE activities (
  session_id TEXT NOT NULL REFERENCES sessions(id),
  id TEXT NOT NULL,
  category TEXT NOT NULL,
  label TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('planned','in_progress','completed','interrupted','cancelled')),
  start_ms INTEGER NOT NULL,
  planned_end_ms INTEGER NOT NULL CHECK(planned_end_ms > start_ms),
  actual_end_ms INTEGER CHECK(actual_end_ms >= start_ms),
  location TEXT NOT NULL,
  destination TEXT,
  attention TEXT NOT NULL CHECK(attention IN ('free','light','focused','unavailable')),
  interruptibility TEXT NOT NULL CHECK(interruptibility IN ('anytime','at_break','at_end')),
  visibility TEXT NOT NULL CHECK(visibility IN ('ordinary','personal','private')),
  source TEXT NOT NULL DEFAULT 'simulated' CHECK(source = 'simulated'),
  details_json TEXT NOT NULL CHECK(json_valid(details_json)),
  PRIMARY KEY(session_id,id),
  CHECK((status IN ('completed','interrupted') AND actual_end_ms IS NOT NULL)
     OR (status IN ('planned','in_progress','cancelled') AND actual_end_ms IS NULL))
);
CREATE UNIQUE INDEX one_active_activity ON activities(session_id)
  WHERE status = 'in_progress';

CREATE TABLE facts (
  session_id TEXT NOT NULL REFERENCES sessions(id),
  id TEXT NOT NULL,
  occurred_ms INTEGER,
  recorded_ms INTEGER NOT NULL,
  source TEXT NOT NULL CHECK(source IN ('canon','observed','simulated','proposed')),
  summary TEXT NOT NULL,
  details_json TEXT NOT NULL CHECK(json_valid(details_json)),
  PRIMARY KEY(session_id,id)
);

CREATE TABLE relationships (
  session_id TEXT NOT NULL REFERENCES sessions(id),
  subject_id TEXT NOT NULL,
  other_id TEXT NOT NULL,
  details_json TEXT NOT NULL CHECK(json_valid(details_json)),
  PRIMARY KEY(session_id,subject_id,other_id),
  CHECK(subject_id <> other_id)
);

CREATE TABLE dm_messages (
  session_id TEXT NOT NULL REFERENCES sessions(id),
  id TEXT NOT NULL,
  observed_ms INTEGER NOT NULL,
  text TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('received','deferred','queued','handled')),
  reply_due_ms INTEGER,
  observation_json TEXT NOT NULL CHECK(json_valid(observation_json)),
  PRIMARY KEY(session_id,id)
);

CREATE TABLE dm_jobs (
  session_id TEXT NOT NULL REFERENCES sessions(id),
  id TEXT NOT NULL,
  dedupe_key TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('pending','attempting','submitted','confirmed','failed','unknown','cancelled')),
  recipient TEXT NOT NULL CHECK(length(recipient) > 0),
  text TEXT NOT NULL,
  created_ms INTEGER NOT NULL,
  updated_ms INTEGER NOT NULL,
  result_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(result_json)),
  PRIMARY KEY(session_id,id),
  UNIQUE(session_id,dedupe_key)
);
CREATE UNIQUE INDEX one_attempt_per_session ON dm_jobs(session_id)
  WHERE status = 'attempting';

CREATE TABLE dm_job_messages (
  session_id TEXT NOT NULL,
  job_id TEXT NOT NULL,
  message_id TEXT NOT NULL,
  PRIMARY KEY(session_id,job_id,message_id),
  FOREIGN KEY(session_id,job_id) REFERENCES dm_jobs(session_id,id),
  FOREIGN KEY(session_id,message_id) REFERENCES dm_messages(session_id,id)
);

-- 저장 어댑터의 추가 검증:
-- 1. UPDATE sessions ... WHERE revision = expected_revision 성공 행 수 = 1.
-- 2. 주간 날짜 유효성, 활동 기간 겹침, 장소 이동, JSON 필수 필드·타입.
-- 3. 상태 전이의 합법성, 하나의 DM에 활성 답변 작업이 중복되지 않는지.
-- 4. 사건의 참여자·이를 아는 인물·근거 ID, 원문 관찰 정보의 신뢰도.
-- 5. 예약 실패 처리. 이 스키마만으로 예약·판단·DM 발송은 실행되지 않음.
