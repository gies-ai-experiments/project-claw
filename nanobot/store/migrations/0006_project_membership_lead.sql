-- 0006_project_membership_lead.sql — enforce one lead membership per project
CREATE UNIQUE INDEX IF NOT EXISTS project_membership_one_lead
  ON project_membership (project_id) WHERE role = 'lead';
