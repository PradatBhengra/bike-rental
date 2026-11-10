-- migrate:up
-- Rollups: tables shaped for reading, rebuilt from booking, payment and refund at any time.
CREATE TABLE user_daily_stats (
  user_id bigint NOT NULL REFERENCES app_user(id),
  day date NOT NULL,
  rides int NOT NULL DEFAULT 0,
  ride_minutes int NOT NULL DEFAULT 0,
  distance_m int NOT NULL DEFAULT 0,
  spend_paise bigint NOT NULL DEFAULT 0,
  late_fee_paise bigint NOT NULL DEFAULT 0,
  refund_paise bigint NOT NULL DEFAULT 0,
  PRIMARY KEY (user_id, day)
);

CREATE TABLE station_daily_stats (
  station_id bigint NOT NULL REFERENCES station(id),
  day date NOT NULL,
  pickups int NOT NULL DEFAULT 0,
  revenue_paise bigint NOT NULL DEFAULT 0,
  cancellations int NOT NULL DEFAULT 0,
  no_shows int NOT NULL DEFAULT 0,
  late_returns int NOT NULL DEFAULT 0,
  PRIMARY KEY (station_id, day)
);

CREATE TABLE bike_daily_usage (
  bike_id bigint NOT NULL REFERENCES bike(id),
  day date NOT NULL,
  booked_minutes int NOT NULL DEFAULT 0,
  ridden_minutes int NOT NULL DEFAULT 0,
  PRIMARY KEY (bike_id, day)
);

CREATE MATERIALIZED VIEW station_hourly_demand AS
SELECT station_id,
       extract(isodow FROM picked_up_at AT TIME ZONE 'Asia/Kolkata')::int AS day_of_week,
       extract(hour FROM picked_up_at AT TIME ZONE 'Asia/Kolkata')::int AS hour,
       count(*)::int AS pickups
FROM booking
WHERE picked_up_at IS NOT NULL
GROUP BY 1, 2, 3;
-- REFRESH ... CONCURRENTLY needs a unique index
CREATE UNIQUE INDEX ON station_hourly_demand (station_id, day_of_week, hour);

-- migrate:down
DROP MATERIALIZED VIEW station_hourly_demand;
DROP TABLE bike_daily_usage, station_daily_stats, user_daily_stats;
