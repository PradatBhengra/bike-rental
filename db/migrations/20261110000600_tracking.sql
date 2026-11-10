-- migrate:up
-- Raw GPS points, one partition a month: dropping an old month is instant, unlike a large DELETE.
CREATE TABLE ride_point (
  booking_id bigint NOT NULL REFERENCES booking(id) ON DELETE CASCADE,
  recorded_at timestamptz NOT NULL,
  location geography(Point, 4326) NOT NULL,
  accuracy_m real,
  speed_mps real,
  received_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (booking_id, recorded_at)  -- a retried batch inserts nothing twice
) PARTITION BY RANGE (recorded_at);
CREATE TABLE ride_point_default PARTITION OF ride_point DEFAULT;

CREATE FUNCTION ensure_ride_point_partition(month date) RETURNS void LANGUAGE plpgsql AS $$
DECLARE
  first date := date_trunc('month', month)::date;
  name text := format('ride_point_%s', to_char(first, 'YYYY_MM'));
BEGIN
  EXECUTE format('CREATE TABLE IF NOT EXISTS %I PARTITION OF ride_point FOR VALUES FROM (%L) TO (%L)',
                 name, first, (first + interval '1 month')::date);
END $$;
SELECT ensure_ride_point_partition(now()::date);
SELECT ensure_ride_point_partition((now() + interval '1 month')::date);

-- One summary line per ride, built at return; it outlives the raw points.
CREATE TABLE ride_path (
  booking_id bigint PRIMARY KEY REFERENCES booking(id) ON DELETE CASCADE,
  path geography(LineString, 4326) NOT NULL,
  replay_path geometry(LineString, 4326) NOT NULL,
  distance_m int NOT NULL,
  point_count int NOT NULL,
  gap_count int NOT NULL,
  started_at timestamptz NOT NULL,
  ended_at timestamptz NOT NULL
);

-- migrate:down
DROP TABLE ride_path;
DROP TABLE ride_point;
DROP FUNCTION ensure_ride_point_partition(date);
