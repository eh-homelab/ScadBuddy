-- An insert type the Bambuddy pull made or linked names its BB spool catalog entry
-- (`GET /inventory/catalog` id; `filament/bambuddy_pull.py`). Read only to a person: the
-- catalog routes take no such field. Nullable: a native type has none (#6262).
ALTER TABLE filament_insert_types ADD COLUMN bb_catalog_id integer UNIQUE;
