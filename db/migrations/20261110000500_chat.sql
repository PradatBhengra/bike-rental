-- migrate:up
CREATE TABLE document (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  slug text NOT NULL UNIQUE,
  title text NOT NULL,
  doc_type text NOT NULL
    CHECK (doc_type IN ('policy', 'manual', 'error_codes', 'repair', 'riding', 'route', 'station', 'pass')),
  bike_type_id bigint REFERENCES bike_type(id),
  route_geojson jsonb,
  active_version_id bigint,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE document_version (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  document_id bigint NOT NULL REFERENCES document(id),
  version_no int NOT NULL,
  content_hash text NOT NULL,
  source_file_id bigint REFERENCES file_object(id),
  markdown text NOT NULL,
  status text NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'indexing', 'ready', 'failed')),
  error text,
  indexed_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (document_id, content_hash),
  UNIQUE (document_id, version_no)
);

ALTER TABLE document ADD FOREIGN KEY (active_version_id) REFERENCES document_version(id);
ALTER TABLE bike_type ADD FOREIGN KEY (manual_document_id) REFERENCES document(id);

CREATE TABLE chunk (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  version_id bigint NOT NULL REFERENCES document_version(id) ON DELETE CASCADE,
  ord int NOT NULL,
  section_no text NOT NULL,
  section_path text NOT NULL,
  anchor text NOT NULL,
  heading text NOT NULL,
  body text NOT NULL,
  content_hash text NOT NULL,
  token_count int NOT NULL,
  embedding vector(384) NOT NULL,
  tsv tsvector GENERATED ALWAYS AS (
    setweight(to_tsvector('english', section_path), 'A') || setweight(to_tsvector('english', body), 'B')
  ) STORED
);
CREATE INDEX ON chunk USING hnsw (embedding vector_cosine_ops);
CREATE INDEX ON chunk USING gin (tsv);
CREATE INDEX ON chunk (version_id);
CREATE INDEX ON chunk (content_hash);

CREATE TABLE corpus_state (
  id boolean PRIMARY KEY DEFAULT true CHECK (id),
  version bigint NOT NULL DEFAULT 0
);
INSERT INTO corpus_state DEFAULT VALUES;

CREATE TABLE conversation (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  user_id bigint NOT NULL REFERENCES app_user(id),
  kind text NOT NULL DEFAULT 'customer' CHECK (kind IN ('customer', 'admin')),
  title text NOT NULL DEFAULT 'New chat',
  summary text,
  last_message_at timestamptz NOT NULL DEFAULT now(),
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON conversation (user_id, last_message_at DESC);

CREATE TABLE message (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  conversation_id bigint NOT NULL REFERENCES conversation(id) ON DELETE CASCADE,
  role text NOT NULL CHECK (role IN ('user', 'assistant', 'tool', 'note')),
  content text NOT NULL DEFAULT '',
  provider text,
  model text,
  prompt_tokens int,
  completion_tokens int,
  latency_ms int,
  citations jsonb NOT NULL DEFAULT '[]',
  cards jsonb NOT NULL DEFAULT '[]',
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON message (conversation_id, id);

CREATE TABLE tool_call (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  message_id bigint NOT NULL REFERENCES message(id) ON DELETE CASCADE,
  step int NOT NULL,
  tool_name text NOT NULL,
  arguments jsonb NOT NULL,
  result jsonb,
  status text NOT NULL CHECK (status IN ('ok', 'invalid_args', 'not_found', 'error')),
  duration_ms int NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON tool_call (message_id);

CREATE TABLE chat_action (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  conversation_id bigint NOT NULL REFERENCES conversation(id) ON DELETE CASCADE,
  message_id bigint REFERENCES message(id) ON DELETE SET NULL,
  user_id bigint NOT NULL REFERENCES app_user(id),
  kind text NOT NULL CHECK (kind IN ('book', 'cancel', 'extend', 'report_issue')),
  payload jsonb NOT NULL,
  preview jsonb NOT NULL,
  status text NOT NULL DEFAULT 'proposed'
    CHECK (status IN ('proposed', 'executed', 'rejected', 'expired', 'failed')),
  expires_at timestamptz NOT NULL,
  decided_at timestamptz,
  result jsonb,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE answer_cache (
  key bytea PRIMARY KEY,
  question text NOT NULL,
  answer text NOT NULL,
  citations jsonb NOT NULL,
  corpus_version bigint NOT NULL,
  model text NOT NULL,
  hits int NOT NULL DEFAULT 0,
  disabled_at timestamptz,
  source_message_id bigint REFERENCES message(id) ON DELETE SET NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE feedback (
  message_id bigint NOT NULL REFERENCES message(id) ON DELETE CASCADE,
  user_id bigint NOT NULL REFERENCES app_user(id),
  rating smallint NOT NULL CHECK (rating IN (1, -1)),
  comment text,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (message_id, user_id)
);

CREATE TABLE flagged_answer (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  message_id bigint NOT NULL REFERENCES message(id) ON DELETE CASCADE,
  reason text NOT NULL CHECK (reason IN ('thumbs_down', 'no_citation', 'invalid_citation', 'tool_error')),
  status text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'reviewed', 'dismissed')),
  reviewer_id bigint REFERENCES app_user(id),
  note text,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (message_id, reason)
);

CREATE TABLE query_log (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  message_id bigint REFERENCES message(id) ON DELETE CASCADE,
  tool_call_id bigint REFERENCES tool_call(id) ON DELETE CASCADE,
  query text NOT NULL,
  vector_ids bigint[] NOT NULL,
  keyword_ids bigint[] NOT NULL,
  fused_ids bigint[] NOT NULL,
  reranked_ids bigint[] NOT NULL,
  timings jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE eval_case (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  kind text NOT NULL CHECK (kind IN ('retrieval', 'tool', 'faithfulness')),
  question text NOT NULL,
  expected jsonb NOT NULL,
  active boolean NOT NULL DEFAULT true,
  UNIQUE (kind, question)
);

CREATE TABLE eval_run (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  kind text NOT NULL,
  git_sha text,
  provider text NOT NULL,
  model text NOT NULL,
  started_at timestamptz NOT NULL DEFAULT now(),
  finished_at timestamptz,
  metrics jsonb
);

CREATE TABLE eval_result (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  run_id bigint NOT NULL REFERENCES eval_run(id) ON DELETE CASCADE,
  case_id bigint NOT NULL REFERENCES eval_case(id),
  passed boolean NOT NULL,
  details jsonb NOT NULL
);

-- migrate:down
DROP TABLE eval_result, eval_run, eval_case, query_log, flagged_answer, feedback, answer_cache,
  chat_action, tool_call, message, conversation, corpus_state, chunk;
ALTER TABLE bike_type DROP CONSTRAINT IF EXISTS bike_type_manual_document_id_fkey;
ALTER TABLE document DROP CONSTRAINT IF EXISTS document_active_version_id_fkey;
DROP TABLE document_version, document;
