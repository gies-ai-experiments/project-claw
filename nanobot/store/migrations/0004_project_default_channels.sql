-- 0004_project_default_channels.sql — per-channel default project.
-- Channels listed here resolve to this project when a turn names no project
-- (e.g. a granola-only "context default" for a multi-project channel).
ALTER TABLE project_registry
    ADD COLUMN IF NOT EXISTS default_channels TEXT[] NOT NULL DEFAULT '{}';
