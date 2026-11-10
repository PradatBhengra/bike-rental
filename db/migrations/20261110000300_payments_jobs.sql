-- migrate:up
CREATE TYPE payment_kind AS ENUM ('rental', 'extension', 'late_fee', 'damage', 'pass');
CREATE TYPE payment_status AS ENUM ('due', 'pending', 'succeeded', 'failed', 'cancelled');

CREATE TABLE payment (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  user_id bigint NOT NULL REFERENCES app_user(id),
  booking_id bigint REFERENCES booking(id),
  kind payment_kind NOT NULL,
  amount_paise int NOT NULL CHECK (amount_paise > 0),
  status payment_status NOT NULL,
  description text NOT NULL DEFAULT '',
  razorpay_order_id text UNIQUE,
  razorpay_payment_id text UNIQUE,
  paid_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  CHECK (status <> 'succeeded' OR paid_at IS NOT NULL)
);
CREATE INDEX ON payment (user_id, status);
CREATE INDEX ON payment (booking_id);
-- a booking has one rental payment; a failed attempt is retried on the same row
CREATE UNIQUE INDEX one_rental_payment ON payment (booking_id) WHERE kind = 'rental';

CREATE TABLE refund (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  payment_id bigint NOT NULL REFERENCES payment(id),
  amount_paise int NOT NULL CHECK (amount_paise > 0),
  reason text NOT NULL,
  status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'succeeded', 'failed')),
  razorpay_refund_id text UNIQUE,
  idempotency_key text NOT NULL UNIQUE,
  created_at timestamptz NOT NULL DEFAULT now(),
  processed_at timestamptz
);
CREATE INDEX ON refund (payment_id);

CREATE TABLE payment_event (
  event_id text PRIMARY KEY,
  type text NOT NULL,
  payload jsonb NOT NULL,
  received_at timestamptz NOT NULL DEFAULT now(),
  processed_at timestamptz
);

CREATE TABLE outbox_event (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  type text NOT NULL,
  aggregate_id bigint,
  payload jsonb NOT NULL DEFAULT '{}',
  created_at timestamptz NOT NULL DEFAULT now(),
  dispatched_at timestamptz
);
CREATE INDEX outbox_undispatched ON outbox_event (id) WHERE dispatched_at IS NULL;

-- failed = waiting for a retry at run_at; dead = out of attempts
CREATE TYPE job_status AS ENUM ('queued', 'running', 'done', 'failed', 'dead');

CREATE TABLE job (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  kind text NOT NULL,
  payload jsonb NOT NULL DEFAULT '{}',
  run_at timestamptz NOT NULL DEFAULT now(),
  status job_status NOT NULL DEFAULT 'queued',
  attempts int NOT NULL DEFAULT 0,
  max_attempts int NOT NULL DEFAULT 5,
  dedupe_key text UNIQUE,
  locked_by text,
  locked_at timestamptz,
  last_error text,
  created_at timestamptz NOT NULL DEFAULT now(),
  finished_at timestamptz
);
CREATE INDEX job_ready ON job (run_at, id) WHERE status IN ('queued', 'failed');

CREATE TABLE file_object (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  bucket text NOT NULL,
  object_key text NOT NULL UNIQUE,
  content_type text NOT NULL,
  size_bytes bigint NOT NULL,
  sha256 text NOT NULL,
  uploaded_by bigint REFERENCES app_user(id),
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE notification (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  user_id bigint NOT NULL REFERENCES app_user(id),
  kind text NOT NULL,
  channel text NOT NULL CHECK (channel IN ('email', 'in_app', 'push')),
  payload jsonb NOT NULL DEFAULT '{}',
  sent_at timestamptz,
  read_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON notification (user_id, created_at);

ALTER TABLE booking ADD COLUMN receipt_file_id bigint REFERENCES file_object(id);

-- migrate:down
ALTER TABLE booking DROP COLUMN receipt_file_id;
DROP TABLE notification, file_object, job, outbox_event, payment_event, refund, payment;
DROP TYPE job_status;
DROP TYPE payment_status;
DROP TYPE payment_kind;
