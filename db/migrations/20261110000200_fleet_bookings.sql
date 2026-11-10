-- migrate:up
CREATE TABLE station (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  name text NOT NULL UNIQUE,
  address text NOT NULL,
  location geography(Point, 4326) NOT NULL,
  opens_at time NOT NULL,
  closes_at time NOT NULL,
  capacity int NOT NULL CHECK (capacity > 0),
  active boolean NOT NULL DEFAULT true,
  CHECK (closes_at > opens_at)
);
CREATE INDEX ON station USING gist (location);

ALTER TABLE app_user
  ADD COLUMN staff_station_id bigint REFERENCES station(id),
  ADD CONSTRAINT staff_has_station CHECK (role <> 'staff' OR staff_station_id IS NOT NULL);

CREATE TABLE bike_type (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  slug text NOT NULL UNIQUE,
  name text NOT NULL,
  category text NOT NULL CHECK (category IN ('city', 'hybrid', 'mountain', 'ebike')),
  description text NOT NULL DEFAULT '',
  specs jsonb NOT NULL DEFAULT '{}',
  manual_document_id bigint,
  active boolean NOT NULL DEFAULT true
);

CREATE TYPE bike_status AS ENUM ('available', 'maintenance', 'retired');

CREATE TABLE bike (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  code text NOT NULL UNIQUE,
  bike_type_id bigint NOT NULL REFERENCES bike_type(id),
  home_station_id bigint NOT NULL REFERENCES station(id),
  status bike_status NOT NULL DEFAULT 'available',
  odometer_km numeric(8,1) NOT NULL DEFAULT 0,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON bike (home_station_id, bike_type_id);

CREATE TABLE pricing_plan (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  bike_type_id bigint NOT NULL REFERENCES bike_type(id),
  hourly_paise int NOT NULL CHECK (hourly_paise > 0),
  daily_cap_paise int NOT NULL CHECK (daily_cap_paise > 0),
  late_fee_per_hour_paise int NOT NULL CHECK (late_fee_per_hour_paise >= 0),
  valid_during tstzrange NOT NULL,
  CONSTRAINT one_plan_at_a_time EXCLUDE USING gist (bike_type_id WITH =, valid_during WITH &&)
);

CREATE TABLE price_modifier (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  name text NOT NULL,
  pct int NOT NULL CHECK (pct BETWEEN -90 AND 300),
  days_of_week smallint[] NOT NULL,  -- ISO: 1 = Monday ... 7 = Sunday
  starts_at time NOT NULL,
  ends_at time NOT NULL,
  bike_type_id bigint REFERENCES bike_type(id),  -- null = all types
  active boolean NOT NULL DEFAULT true,
  CHECK (ends_at > starts_at)
);

CREATE TABLE setting (
  key text PRIMARY KEY,
  value jsonb NOT NULL,
  updated_by bigint REFERENCES app_user(id),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TYPE booking_status AS ENUM
  ('pending_payment', 'confirmed', 'picked_up', 'returned', 'cancelled', 'expired', 'no_show');

CREATE TABLE booking (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  code text NOT NULL UNIQUE,
  user_id bigint NOT NULL REFERENCES app_user(id),
  bike_id bigint NOT NULL REFERENCES bike(id),
  station_id bigint NOT NULL REFERENCES station(id),
  during tstzrange NOT NULL,
  status booking_status NOT NULL DEFAULT 'pending_payment',
  hold_expires_at timestamptz,
  price_snapshot jsonb NOT NULL,
  quoted_paise int NOT NULL CHECK (quoted_paise >= 0),
  picked_up_at timestamptz,
  returned_at timestamptz,
  late_minutes int,
  overdue_notified_at timestamptz,
  return_note text,
  created_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT no_double_booking
    EXCLUDE USING gist (bike_id WITH =, during WITH &&)
    WHERE (status IN ('pending_payment', 'confirmed', 'picked_up')),
  CHECK (lower(during) < upper(during)),
  CHECK (status <> 'pending_payment' OR hold_expires_at IS NOT NULL),
  CHECK (status NOT IN ('picked_up', 'returned') OR picked_up_at IS NOT NULL),
  CHECK (status <> 'returned' OR returned_at IS NOT NULL)
);
CREATE INDEX ON booking (user_id, (lower(during)));
CREATE INDEX ON booking (station_id, (lower(during)));
CREATE INDEX booking_open ON booking (status) WHERE status IN ('pending_payment', 'confirmed', 'picked_up');

CREATE TABLE booking_status_history (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  booking_id bigint NOT NULL REFERENCES booking(id),
  from_status booking_status,
  to_status booking_status NOT NULL,
  actor_id bigint REFERENCES app_user(id),  -- null = system
  reason text,
  at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON booking_status_history (booking_id);

CREATE TABLE idempotency_key (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  key text NOT NULL,
  user_id bigint NOT NULL REFERENCES app_user(id),
  endpoint text NOT NULL,
  request_hash text NOT NULL,
  status_code int,
  response jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (user_id, endpoint, key)
);

CREATE TABLE audit_log (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  actor_id bigint REFERENCES app_user(id),
  action text NOT NULL,
  entity text NOT NULL,
  entity_id bigint,
  before jsonb,
  after jsonb,
  reason text,
  ip inet,
  at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON audit_log (entity, entity_id);

-- Policy numbers. The help documents state the same values, and a test compares the two.
INSERT INTO setting (key, value) VALUES
  ('hold_minutes', '15'),
  ('min_booking_minutes', '60'),
  ('max_booking_hours', '72'),
  ('max_advance_days', '30'),
  ('max_unpaid_holds', '2'),
  ('early_pickup_minutes', '15'),
  ('late_grace_minutes', '15'),
  ('no_show_after_minutes', '60'),
  ('no_show_refund_pct', '0'),
  ('refund_tiers', '[{"min_hours_before": 24, "pct": 100}, {"min_hours_before": 2, "pct": 50}, {"min_hours_before": 0, "pct": 0}]'),
  ('chat_messages_per_hour', '30');

-- migrate:down
DELETE FROM setting;
DROP TABLE audit_log, idempotency_key, booking_status_history, booking, setting, price_modifier,
  pricing_plan, bike, bike_type;
ALTER TABLE app_user DROP COLUMN staff_station_id;
DROP TABLE station;
DROP TYPE booking_status;
DROP TYPE bike_status;
