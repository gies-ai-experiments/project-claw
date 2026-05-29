-- 0002_project_registry.sql — project registry + per-thread sticky project lock
CREATE TABLE IF NOT EXISTS project_registry (
    project_id          TEXT PRIMARY KEY,
    github_repos        TEXT[] NOT NULL DEFAULT '{}',
    granola_folder_id   TEXT,
    allowed_channels    TEXT[] NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS thread_project_lock (
    channel_id  TEXT NOT NULL,
    thread_ts   TEXT NOT NULL,
    project_id  TEXT NOT NULL,
    locked_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (channel_id, thread_ts)
);
