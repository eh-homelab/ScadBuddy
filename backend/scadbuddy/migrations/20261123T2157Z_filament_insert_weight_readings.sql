-- An insert's empty weight from weighing new, unused spools (#7280).
--
-- R-7280-3: `empty_weight_basis` says where the insert's empty weight came from. Every
-- existing row takes the default `catalog`; no other data is written. A person's edit sets
-- `person` or `placeholder`, an accepted derivation `derived`; `weighed_empty` is only a
-- value here (recording it is a follow-up).
--
-- R-7280-2: each derived reading is its own row, one per spool at most. The insert's
-- weight is the mean of its rows (`filament/insert_weights.py`). `derived_g` is
-- `reading_g - label_weight_g`, kept unrounded.
ALTER TABLE filament_insert_types
    ADD COLUMN empty_weight_basis text NOT NULL DEFAULT 'catalog'
        CHECK (empty_weight_basis IN ('catalog', 'person', 'placeholder', 'derived', 'weighed_empty'));

CREATE TABLE filament_insert_weight_readings (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    insert_type_id uuid NOT NULL REFERENCES filament_insert_types (id) ON DELETE CASCADE,
    spool_id uuid NOT NULL UNIQUE REFERENCES filament_spools (id) ON DELETE CASCADE,
    reading_g double precision NOT NULL CHECK (reading_g > 0),
    label_weight_g integer NOT NULL CHECK (label_weight_g > 0),
    derived_g double precision NOT NULL CHECK (derived_g > 0),
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX filament_insert_weight_readings_insert
    ON filament_insert_weight_readings (insert_type_id);
