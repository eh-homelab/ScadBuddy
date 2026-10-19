-- The printer registry (#3823, plan 2026-10-16-printer-identity-native-path, Task 2): one
-- row per physical printer, keyed by serial, linking the Bambuddy printer and ScadBuddy's
-- own LAN connection. A Bambuddy printer whose serial is withheld gets the synthetic serial
-- `bambuddy-<id>`. The access code is an envelope under the KEK (core/secrets.py), its AAD
-- `printer:<id>:<serial>`; no route returns it. `lan_cert_sha256` pins the printer's
-- self-signed leaf certificate, recorded when the access code was typed. `id` is a uuid4,
-- the printer's identity on every surface (spec 2026-10-16 §6.1).
CREATE TABLE printers (
    id uuid PRIMARY KEY,
    serial text NOT NULL UNIQUE,
    name text NOT NULL,
    model text,
    bambuddy_printer_id integer UNIQUE,
    -- The Bambuddy printer the user last unlinked from this row: never linked again by
    -- its serial (the strip's ensure), only by the user.
    bambuddy_unlinked_id integer,
    lan_host text,
    lan_cert_sha256 text,
    access_code_sealed bytea,
    access_code_dek_sealed bytea,
    access_code_kek_id text,
    preferred_path text NOT NULL DEFAULT 'auto'
        CHECK (preferred_path IN ('bambuddy', 'native', 'auto')),
    -- Whether Bambuddy listed the linked printer as active when it last answered: the
    -- strip shows the same printers while Bambuddy is down.
    bambuddy_active boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK ((access_code_sealed IS NULL) = (access_code_dek_sealed IS NULL)),
    CHECK ((access_code_sealed IS NULL) = (access_code_kek_id IS NULL))
);

-- The latest merged MQTT report of each LAN printer, written by its `PrinterWatch`.
CREATE TABLE printer_reports (
    printer_id uuid PRIMARY KEY REFERENCES printers (id) ON DELETE CASCADE,
    report jsonb NOT NULL,
    seen_at timestamptz NOT NULL
);
