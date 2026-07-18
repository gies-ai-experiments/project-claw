-- 0007_project_channel_slug.sql — reserve runtime Slack slugs before provisioning
ALTER TABLE project_registry ADD COLUMN IF NOT EXISTS channel_slug TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS project_registry_channel_slug_unique
  ON project_registry (channel_slug) WHERE channel_slug IS NOT NULL;
