-- A location sensor's alert notifications (#6646). `alert_open` is whether the sensor's
-- alert is open, as the readings that stored it saw the band: a reading outside the band
-- opens it at once, and it closes only once the readings have stayed inside the band for
-- `sensors.ALERT_CLEAR_AFTER` (`alert_clear_since` is the first inside reading's time
-- while it is open; plan Decision 39). A flip queues one notification when `notify` is
-- on, so a reading that keeps the sensor alerting queues none. It starts as each sensor
-- stands now, so the deploy sends nothing for an alert that was already open (plan
-- Decision 30).
ALTER TABLE filament_location_sensors ADD COLUMN alert_open boolean NOT NULL DEFAULT false;
ALTER TABLE filament_location_sensors ADD COLUMN alert_clear_since timestamptz;
UPDATE filament_location_sensors
SET alert_open = coalesce(last_value > alert_above, false) OR coalesce(last_value < alert_below, false);

-- One row per notification: queued under the sensor's row lock by the reading that flips
-- `alert_open`, with its title and message made then, from the band and names of that
-- moment. The print worker claims it (`sending`) through the CRUD route
-- `…/print/filament/sensor-notifications/claim`, sends it through Bambuddy's app-message
-- route and ends it `sent` or `failed` through `…/{id}/end` (plan Decision 40). Never sent
-- twice: a claim is a state change, and a row left `sending` is ended `interrupted` by a
-- later claim, never tried again.
CREATE TABLE filament_sensor_notifications (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    sensor_id uuid NOT NULL REFERENCES filament_location_sensors (id) ON DELETE CASCADE,
    kind text NOT NULL CHECK (kind IN ('started', 'stopped')),
    value double precision NOT NULL,
    -- The reading's time: newer than every earlier row's of the sensor (`record_on`
    -- refuses a reading not newer than the last stored one), so it orders them.
    read_at timestamptz NOT NULL,
    title text NOT NULL,
    message text NOT NULL,
    channel text NOT NULL DEFAULT 'bambuddy' CHECK (channel = 'bambuddy'),
    state text NOT NULL DEFAULT 'queued' CHECK (state IN ('queued', 'sending', 'sent', 'failed')),
    -- How many of Bambuddy's channels took the message; null until Bambuddy answered.
    channels integer,
    -- A fixed code (`filament/models.py` `NotificationError`), never Bambuddy's text.
    error text,
    queued_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    claimed_at timestamptz,
    sent_at timestamptz,
    CHECK ((state = 'failed') = (error IS NOT NULL)),
    CHECK ((state = 'queued') = (claimed_at IS NULL) OR state = 'failed')
);
CREATE INDEX filament_sensor_notifications_sensor
    ON filament_sensor_notifications (sensor_id, read_at DESC);
CREATE INDEX filament_sensor_notifications_open
    ON filament_sensor_notifications (read_at) WHERE state IN ('queued', 'sending');
