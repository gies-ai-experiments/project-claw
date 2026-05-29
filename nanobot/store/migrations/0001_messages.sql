-- 0001_messages.sql — raw conversation log (L1)
CREATE TABLE IF NOT EXISTS messages (
    id              BIGSERIAL PRIMARY KEY,
    channel_type    TEXT        NOT NULL,
    channel_id      TEXT        NOT NULL,
    thread_ts       TEXT        NOT NULL,
    project_id      TEXT,
    user_id         TEXT,
    role            TEXT        NOT NULL CHECK (role IN ('user','assistant','tool')),
    body            TEXT        NOT NULL,
    tool_calls      JSONB,
    slack_ts        TEXT        NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    distilled_at    TIMESTAMPTZ,
    UNIQUE (channel_type, channel_id, slack_ts)
);

CREATE INDEX IF NOT EXISTS messages_thread_idx
    ON messages (channel_id, thread_ts, created_at DESC);
CREATE INDEX IF NOT EXISTS messages_body_fts_idx
    ON messages USING GIN (to_tsvector('english', body));
CREATE INDEX IF NOT EXISTS messages_undistilled_idx
    ON messages (project_id, created_at) WHERE distilled_at IS NULL;
