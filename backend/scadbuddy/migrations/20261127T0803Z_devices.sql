-- #8244 (design: docs/superpowers/specs/2026-11-27-devices-design.md): paired devices.
-- core writes these rows (tools/go/core/internal/devices). A device is the phone app,
-- paired to a person by a one-time code. Only the SHA-256 of a code or a token is stored.
-- A call's events are events on the bus (kinds `device.*`), not rows here.

-- A paired device. `owner` is the principal who made the pairing; the device's calls act
-- as that person. Only the four methods of the design are capabilities.
CREATE TABLE devices (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    owner        text NOT NULL,
    name         text NOT NULL CHECK (char_length(name) BETWEEN 1 AND 100),
    model        text NOT NULL CHECK (char_length(model) <= 100),
    app_version  text NOT NULL CHECK (char_length(app_version) <= 50),
    capabilities text[] NOT NULL
        CHECK (capabilities <@ ARRAY['nfc.read', 'nfc.write', 'code.scan', 'camera.capture']),
    created_at   timestamptz NOT NULL DEFAULT now(),
    last_seen_at timestamptz,
    revoked_at   timestamptz
);
CREATE INDEX devices_owner ON devices (owner, created_at DESC);

-- A pairing request: single-use, and it expires 10 minutes after it was made.
CREATE TABLE device_pairings (
    code_hash  bytea PRIMARY KEY CHECK (octet_length(code_hash) = 32),
    owner      text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL,
    spent_at   timestamptz,
    device_id  uuid REFERENCES devices (id) ON DELETE SET NULL
);
CREATE INDEX device_pairings_expires ON device_pairings (expires_at);

-- A device's token. `expires_at` moves to now + the idle time at each authenticated
-- request, never past `hard_cap_at`.
CREATE TABLE device_tokens (
    token_hash  bytea PRIMARY KEY CHECK (octet_length(token_hash) = 32),
    device_id   uuid NOT NULL REFERENCES devices (id) ON DELETE CASCADE,
    created_at  timestamptz NOT NULL DEFAULT now(),
    expires_at  timestamptz NOT NULL,
    hard_cap_at timestamptz NOT NULL,
    revoked_at  timestamptz,
    CHECK (expires_at <= hard_cap_at)
);
CREATE INDEX device_tokens_device ON device_tokens (device_id);

-- A call of a device's method. `outcome` is NULL while the call is open. A call that waits
-- for an approval (an assistant's or a flow's nfc.write) names its ai_approvals row in
-- `approval_id` and has no `sent_at` until the approval is used.
CREATE TABLE device_calls (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    device_id   uuid NOT NULL REFERENCES devices (id) ON DELETE CASCADE,
    caller      text NOT NULL,
    caller_kind text NOT NULL CHECK (caller_kind IN ('person', 'assistant', 'flow')),
    method      text NOT NULL CHECK (method IN ('nfc.read', 'nfc.write', 'code.scan', 'camera.capture')),
    args        jsonb NOT NULL DEFAULT '{}' CHECK (octet_length(args::text) <= 4096),
    timeout_s   integer NOT NULL CHECK (timeout_s BETWEEN 1 AND 120),
    outcome     text CHECK (outcome IN ('done', 'error', 'offline', 'timeout', 'denied')),
    result      jsonb CHECK (octet_length(result::text) <= 4096),
    approval_id uuid,
    created_at  timestamptz NOT NULL DEFAULT now(),
    deadline    timestamptz NOT NULL,
    sent_at     timestamptz,
    ended_at    timestamptz,
    CHECK ((outcome IS NULL) = (ended_at IS NULL))
);
CREATE INDEX device_calls_device ON device_calls (device_id, created_at DESC);
CREATE INDEX device_calls_open ON device_calls (deadline) WHERE outcome IS NULL;

-- What a call read: a tag dump (`nfc`, blob kind `tag_dump`) or a photo (`photo`, blob kind
-- `scan_image`). The blob is held in blob_refs as (`device_call`, <call id>).
CREATE TABLE device_reads (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    device_id      uuid NOT NULL REFERENCES devices (id) ON DELETE CASCADE,
    call_id        uuid NOT NULL REFERENCES device_calls (id) ON DELETE CASCADE,
    at             timestamptz NOT NULL DEFAULT now(),
    kind           text NOT NULL CHECK (kind IN ('nfc', 'photo')),
    uid            text CHECK (uid ~ '^[0-9A-F]{8,20}$'),
    tech           text CHECK (char_length(tech) <= 40),
    blob_key       text NOT NULL,
    media_type     text NOT NULL,
    complete       boolean NOT NULL DEFAULT true,
    missing_blocks integer[] NOT NULL DEFAULT '{}'
);
CREATE UNIQUE INDEX device_reads_call ON device_reads (call_id);

-- A call's events, replayed from the event log by the call's id
-- (GET /api/v1/devices/calls/{id}). The build locks writes to events while it runs (no
-- CONCURRENTLY: a migration runs in a transaction), as events_own_retention's did. The
-- log keeps only its retention's rows, so that is a moment; a log far larger needs this
-- index built CONCURRENTLY by hand first.
CREATE INDEX events_device_call ON events ((payload ->> 'call'), seq) WHERE kind LIKE 'device.%';
