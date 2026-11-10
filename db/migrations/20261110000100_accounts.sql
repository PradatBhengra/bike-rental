-- migrate:up
CREATE EXTENSION IF NOT EXISTS citext;
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS btree_gist;  -- lets a GiST index compare plain columns with =

CREATE TYPE user_role AS ENUM ('customer', 'staff', 'admin');

CREATE TABLE app_user (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  email citext NOT NULL UNIQUE,
  password_hash text NOT NULL,
  full_name text NOT NULL,
  phone text,
  role user_role NOT NULL DEFAULT 'customer',
  tracking_consent boolean NOT NULL DEFAULT false,
  email_verified_at timestamptz,
  blocked_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE refresh_token (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  user_id bigint NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
  token_hash text NOT NULL UNIQUE,
  family_id uuid NOT NULL,
  expires_at timestamptz NOT NULL,
  revoked_at timestamptz,
  replaced_by bigint REFERENCES refresh_token(id),
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON refresh_token (family_id);

CREATE TABLE email_token (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  user_id bigint NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
  purpose text NOT NULL CHECK (purpose IN ('verify', 'reset')),
  token_hash text NOT NULL UNIQUE,
  expires_at timestamptz NOT NULL,
  used_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE auth_event (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  user_id bigint REFERENCES app_user(id) ON DELETE SET NULL,
  email_tried citext,
  event text NOT NULL CHECK (event IN ('signup', 'login_ok', 'login_fail', 'refresh', 'refresh_reuse', 'logout', 'reset')),
  ip inet,
  at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON auth_event (user_id, at);

-- migrate:down
DROP TABLE auth_event, email_token, refresh_token, app_user;
DROP TYPE user_role;
