-- #7273 G4: a tag read or write an agent asked for, run by the phone page when a person arms it.
CREATE TABLE filament_nfc_jobs (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    kind text NOT NULL CHECK (kind IN ('read', 'write')),
    records jsonb,
    created_by text NOT NULL,
    armed_by text,
    state text NOT NULL DEFAULT 'pending'
        CHECK (state IN ('pending', 'armed', 'done', 'failed', 'expired')),
    result jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL,
    CHECK ((kind = 'write') = (records IS NOT NULL))
);
CREATE INDEX filament_nfc_jobs_open ON filament_nfc_jobs (created_at) WHERE state IN ('pending', 'armed');
