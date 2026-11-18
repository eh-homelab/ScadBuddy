-- #6348: drop ScadBuddy's old spool tables. #6262 stopped reading and writing them and
-- carried their data into the `filament_*` tables (`20261114T1302Z_filament_from_native.sql`,
-- `20261116T0034Z_filament_plan_spool_numbers.sql`, `20261116T0547Z_filament_carry_tray_links.sql`).
-- Children first: `native_spool_uses` names `native_spools`, and `spool_record_links` and
-- `tray_spool_links` name `physical_spools`. No CASCADE: nothing else depends on them.
DROP TABLE IF EXISTS native_spool_uses;
DROP TABLE IF EXISTS native_spools;
DROP TABLE IF EXISTS spool_record_links;
DROP TABLE IF EXISTS spool_provider_records;
DROP TABLE IF EXISTS spool_provider_refreshes;
DROP TABLE IF EXISTS tray_spool_links;
DROP TABLE IF EXISTS physical_spools;
