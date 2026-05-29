-- 0003_project_facts.sql — distilled per-project facts (L2)
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS project_facts (
    id                  BIGSERIAL PRIMARY KEY,
    project_id          TEXT NOT NULL,
    kind                TEXT NOT NULL CHECK (kind IN
                          ('decision','action','fact','open_question','role')),
    subject             TEXT NOT NULL,
    body                TEXT NOT NULL,
    source_message_ids  BIGINT[] NOT NULL DEFAULT '{}',
    confidence          REAL NOT NULL DEFAULT 1.0,
    distiller_version   TEXT NOT NULL,
    embedding           vector(1536),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    superseded_by       BIGINT REFERENCES project_facts(id)
);

CREATE INDEX IF NOT EXISTS project_facts_kind_idx
    ON project_facts (project_id, kind, created_at DESC);
CREATE INDEX IF NOT EXISTS project_facts_fts_idx
    ON project_facts USING GIN (to_tsvector('english', subject || ' ' || body));
CREATE INDEX IF NOT EXISTS project_facts_embed_idx
    ON project_facts USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS project_facts_current_idx
    ON project_facts (project_id) WHERE superseded_by IS NULL;
