-- 0005_asana_project_sync.sql — durable Asana approval and provisioning state
ALTER TABLE project_registry ADD COLUMN IF NOT EXISTS display_name TEXT NOT NULL DEFAULT '';
ALTER TABLE project_registry ADD COLUMN IF NOT EXISTS description TEXT NOT NULL DEFAULT '';
ALTER TABLE project_registry ADD COLUMN IF NOT EXISTS lead_email TEXT;
ALTER TABLE project_registry ADD COLUMN IF NOT EXISTS slack_channel_id TEXT UNIQUE;
ALTER TABLE project_registry ADD COLUMN IF NOT EXISTS asana_project_gid TEXT UNIQUE;
ALTER TABLE project_registry ADD COLUMN IF NOT EXISTS lifecycle_status TEXT NOT NULL DEFAULT 'active';
ALTER TABLE project_registry ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'static_config';
ALTER TABLE project_registry ADD COLUMN IF NOT EXISTS created_by_slack_id TEXT;
ALTER TABLE project_registry ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT now();
ALTER TABLE project_registry ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now();

CREATE TABLE identity_directory (
  email_normalized TEXT PRIMARY KEY,
  display_name TEXT NOT NULL,
  slack_user_id TEXT,
  asana_user_gid TEXT,
  verified_at TIMESTAMPTZ
);

CREATE TABLE project_membership (
  project_id TEXT NOT NULL REFERENCES project_registry(project_id),
  email_normalized TEXT NOT NULL REFERENCES identity_directory(email_normalized),
  role TEXT NOT NULL CHECK (role IN ('lead', 'participant')),
  PRIMARY KEY (project_id, email_normalized)
);

CREATE TABLE meeting_approval (
  id UUID PRIMARY KEY,
  note_id TEXT NOT NULL,
  meeting_title TEXT NOT NULL,
  meeting_date DATE NOT NULL,
  project_key TEXT NOT NULL,
  revision INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL CHECK (status IN
    ('draft','pending','skipped','approved','provisioning','complete','needs_attention')),
  draft JSONB NOT NULL,
  approved_snapshot JSONB,
  approver_slack_id TEXT,
  approved_at TIMESTAMPTZ,
  review_channel_id TEXT,
  review_message_ts TEXT[] NOT NULL DEFAULT '{}',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (note_id, project_key)
);

CREATE TABLE provisioning_job (
  id UUID PRIMARY KEY,
  approval_id UUID NOT NULL UNIQUE REFERENCES meeting_approval(id),
  kind TEXT NOT NULL CHECK (kind IN ('existing_project','new_project')),
  status TEXT NOT NULL CHECK (status IN ('pending','running','complete','needs_attention')),
  retry_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE provisioning_step (
  job_id UUID NOT NULL REFERENCES provisioning_job(id),
  step_name TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('pending','running','complete','needs_attention')),
  idempotency_key TEXT NOT NULL,
  external_id TEXT,
  attempt_count INTEGER NOT NULL DEFAULT 0,
  last_error TEXT,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (job_id, step_name),
  UNIQUE (idempotency_key)
);
