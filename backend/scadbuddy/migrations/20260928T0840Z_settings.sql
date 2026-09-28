-- The settings (`library.settings_store.SettingsStore`), formerly
-- data/settings.json. One row per setting, so a write touches only its own row.
-- A JSON `null` value is an env-seeded setting the UI cleared; no row is "never
-- set" (the environment's value, or the default). The print dialog's per-model
-- choices and per-printer plates get tables of their own, since it writes both on
-- every print.
CREATE TABLE settings (
    name       text PRIMARY KEY,
    value      jsonb NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE model_print_choices (
    model_id   text PRIMARY KEY,
    choices    jsonb NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE printer_bed_types (
    printer_id bigint PRIMARY KEY,
    bed_type   text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);
