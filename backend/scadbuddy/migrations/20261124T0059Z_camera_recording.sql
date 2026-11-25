-- Always-on printer camera recording, phase 1 (plan 2026-11-23 camera recording).
-- `camera_recording` is the printer's toggle: a printer records when it is on, the LAN
-- link has a sealed code and the last report names an rtsps:// camera.
ALTER TABLE printers ADD COLUMN camera_recording boolean NOT NULL DEFAULT true;

-- One row per uploaded fMP4 segment. `object_key` is the blob key; `init_sha256` names the
-- segment's init section, which a player needs to start at the segment.
CREATE TABLE camera_segments (
    id bigserial PRIMARY KEY,
    printer_id uuid NOT NULL REFERENCES printers (id) ON DELETE CASCADE,
    started_at timestamptz NOT NULL,
    duration_ms integer NOT NULL CHECK (duration_ms > 0),
    object_key text NOT NULL UNIQUE,
    bytes bigint NOT NULL CHECK (bytes > 0),
    init_bytes integer NOT NULL CHECK (init_bytes > 0),
    init_sha256 text NOT NULL,
    recorder text NOT NULL,
    discontinuity boolean NOT NULL DEFAULT false,
    indexed_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX camera_segments_printer_started ON camera_segments (printer_id, started_at);

-- The recorder that holds one printer's feed now, until `expires_at`.
CREATE TABLE camera_leases (
    printer_id uuid PRIMARY KEY REFERENCES printers (id) ON DELETE CASCADE,
    holder text NOT NULL,
    expires_at timestamptz NOT NULL
);

-- What the relay and the recorders last said about one printer's feed.
CREATE TABLE camera_feeds (
    printer_id uuid NOT NULL REFERENCES printers (id) ON DELETE CASCADE,
    part text NOT NULL CHECK (part IN ('relay', 'recorder')),
    report jsonb NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (printer_id, part)
);
