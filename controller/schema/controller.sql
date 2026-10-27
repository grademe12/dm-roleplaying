-- 신규 빈 DB에서 schema.sql 다음에 적용. 기존 DB 마이그레이션 아님.
CREATE TABLE controller_meta (
  singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
  version TEXT NOT NULL CHECK(version = '0.3')
);
INSERT INTO controller_meta VALUES (1, '0.3');

CREATE TABLE decisions (
  session_id TEXT NOT NULL REFERENCES sessions(id),
  id TEXT NOT NULL,
  digest TEXT NOT NULL,
  applied_ms INTEGER NOT NULL,
  PRIMARY KEY(session_id,id)
);

CREATE TABLE runtime_settings (
  singleton INTEGER PRIMARY KEY CHECK(singleton=1),
  transport_mode TEXT NOT NULL CHECK(transport_mode IN ('mock','external'))
);

ALTER TABLE sessions ADD COLUMN send_blocked_until_ms INTEGER;
ALTER TABLE sessions ADD COLUMN timing_profile TEXT NOT NULL DEFAULT 'normal'
  CHECK(timing_profile IN ('live','normal','quiet'));
ALTER TABLE sessions ADD COLUMN next_send_after_ms INTEGER;
ALTER TABLE sessions ADD COLUMN dm_observation_revision INTEGER NOT NULL DEFAULT 0
  CHECK(dm_observation_revision >= 0);

CREATE TABLE reply_groups (
  session_id TEXT NOT NULL REFERENCES sessions(id),
  id TEXT NOT NULL,
  observation_revision INTEGER NOT NULL CHECK(observation_revision >= 0),
  status TEXT NOT NULL CHECK(status IN ('active','completed','cancelled')),
  created_ms INTEGER NOT NULL,
  PRIMARY KEY(session_id,id)
);

CREATE TABLE reply_targets (
  session_id TEXT NOT NULL,
  group_id TEXT NOT NULL,
  message_id TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
  PRIMARY KEY(session_id,group_id,message_id),
  FOREIGN KEY(session_id,group_id) REFERENCES reply_groups(session_id,id),
  FOREIGN KEY(session_id,message_id) REFERENCES dm_messages(session_id,id)
);
CREATE UNIQUE INDEX one_active_reply_group_per_message
  ON reply_targets(session_id,message_id) WHERE active = 1;

-- ALTER로 추가한 nullable 필드는 삽입 트리거에서 필수값을 검사한다.
ALTER TABLE dm_jobs ADD COLUMN due_ms INTEGER;
ALTER TABLE dm_jobs ADD COLUMN group_id TEXT;
ALTER TABLE dm_jobs ADD COLUMN part_index INTEGER CHECK(part_index >= 0);
ALTER TABLE dm_jobs ADD COLUMN attempt_token TEXT;
ALTER TABLE dm_jobs ADD COLUMN attempt_started_ms INTEGER;
ALTER TABLE dm_jobs ADD COLUMN lease_expires_ms INTEGER;
ALTER TABLE dm_jobs ADD COLUMN provider_message_id TEXT;
CREATE UNIQUE INDEX one_part_per_group ON dm_jobs(session_id,group_id,part_index);
CREATE INDEX due_job_lookup ON dm_jobs(session_id,status,due_ms);

CREATE TRIGGER validate_new_job BEFORE INSERT ON dm_jobs BEGIN
  SELECT CASE WHEN NEW.due_ms IS NULL OR NEW.group_id IS NULL OR NEW.part_index IS NULL
    THEN RAISE(ABORT, 'missing_job_schedule') END;
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1 FROM reply_groups WHERE session_id=NEW.session_id AND id=NEW.group_id)
    THEN RAISE(ABORT, 'missing_reply_group') END;
  SELECT CASE WHEN NEW.status <> 'pending'
    THEN RAISE(ABORT, 'new_job_must_be_pending') END;
END;

CREATE TRIGGER validate_updated_job BEFORE UPDATE ON dm_jobs BEGIN
  SELECT CASE WHEN NEW.group_id IS NOT OLD.group_id OR NEW.part_index IS NOT OLD.part_index
    OR NEW.session_id IS NOT OLD.session_id OR NEW.id IS NOT OLD.id
    OR NEW.dedupe_key IS NOT OLD.dedupe_key
    THEN RAISE(ABORT, 'immutable_job_identity') END;
  SELECT CASE WHEN NEW.due_ms IS NULL
    THEN RAISE(ABORT, 'missing_due_ms') END;
  SELECT CASE WHEN NEW.status='attempting' AND (
    NEW.attempt_token IS NULL OR NEW.attempt_started_ms IS NULL
    OR NEW.lease_expires_ms IS NULL OR NEW.lease_expires_ms <= NEW.attempt_started_ms)
    THEN RAISE(ABORT, 'missing_attempt_lease') END;
END;

-- Python 어댑터에서 추가 검증:
-- group 삭제 금지, 그룹 0부터 연속 순번 및 dedupe_key 일치, 상태 전이,
-- reply_targets.active와 그룹 상태의 원자적 갱신, handled 수신 재답변 금지,
-- attempt_token 비교, 시간/정수 타입, due/차단/관찰 revision/이전 부분 확인.
