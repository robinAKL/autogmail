CREATE TABLE IF NOT EXISTS workspace_migrations (version text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS workspace_records (
  scope text NOT NULL,
  kind text NOT NULL,
  id text NOT NULL,
  version integer NOT NULL DEFAULT 1,
  data jsonb NOT NULL,
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY(scope, kind, id)
);
CREATE INDEX IF NOT EXISTS workspace_records_kind ON workspace_records(scope,kind,updated_at DESC);
INSERT INTO workspace_migrations(version) VALUES ('001_workspace') ON CONFLICT DO NOTHING;
