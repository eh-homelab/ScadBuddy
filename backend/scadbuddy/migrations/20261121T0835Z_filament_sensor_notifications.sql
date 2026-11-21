-- A location sensor's alert notifications (#6646). `alert_open` is whether the sensor's
-- last stored reading was outside its band, as the reading that stored it saw the band:
-- a reading that flips it queues one notification when `notify` is on, so a reading that
-- keeps the sensor alerting queues none. It starts as each sensor stands now, so the
-- deploy sends nothing for an alert that was already open (plan Decision 30).
ALTER TABLE filament_location_sensors ADD COLUMN alert_open boolean NOT NULL DEFAULT false;
UPDATE filament_location_sensors
SET alert_open = coalesce(last_value > alert_above, false) OR coalesce(last_value < alert_below, false);

-- One row per notification: queued under the sensor's row lock by the reading that flips
-- `alert_open`, then claimed (`sending`) and sent through Bambuddy's app-message route by
-- the spool refresh's pull, and ended `sent` or `failed`. Never sent twice: a claim is a
-- state change, and a row left `sending` by a crash is not tried again.
CREATE TABLE filament_sensor_notifications (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    sensor_id uuid NOT NULL REFERENCES filament_location_sensors (id) ON DELETE CASCADE,
    kind text NOT NULL CHECK (kind IN ('started', 'stopped')),
    value double precision NOT NULL,
    read_at timestamptz NOT NULL,
    channel text NOT NULL DEFAULT 'bambuddy' CHECK (channel = 'bambuddy'),
    state text NOT NULL DEFAULT 'queued' CHECK (state IN ('queued', 'sending', 'sent', 'failed')),
    -- How many of Bambuddy's channels took the message; null until Bambuddy answered.
    channels integer,
    -- A fixed code (`filament/sensor_notify.py` `Failure`), never Bambuddy's text.
    error text,
    queued_at timestamptz NOT NULL DEFAULT now(),
    sent_at timestamptz,
    CHECK ((state = 'failed') = (error IS NOT NULL))
);
CREATE INDEX filament_sensor_notifications_sensor
    ON filament_sensor_notifications (sensor_id, queued_at DESC);
CREATE INDEX filament_sensor_notifications_queued
    ON filament_sensor_notifications (queued_at) WHERE state = 'queued';
