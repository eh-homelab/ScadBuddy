-- #2978: the print dialog's preset catalogue, kept for every API replica.
--
-- Bambuddy's /slicer/presets is about 1 MB and takes seconds; one kept in a single
-- process's memory (#2361) was rarely the one a reopen landed on. `key` hashes the
-- Bambuddy URL and API key, so the key itself is never stored. `catalogue` is the
-- dialog's normalised choices (bambuddy/catalogue.py), not Bambuddy's raw answer.
CREATE TABLE IF NOT EXISTS bambuddy_preset_catalogues (
    key text PRIMARY KEY,
    catalogue jsonb NOT NULL,
    read_at timestamptz NOT NULL
);
