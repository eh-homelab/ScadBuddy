# Drop the native spool and overlay tables (#6348)

## Goal

Drop the seven tables that #6262 stopped reading and writing, in one new migration,
`backend/scadbuddy/migrations/20261118T1235Z_drop_native_spool_tables.sql`. Prod runs
`sha-24b3a97`, 36 commits after #6262 merged (`7446ae8b3`), so the one-deploy-cycle
condition holds.

## Tables

All seven are named by #6262's plan (Task 12, "No DROP"). `git grep` over
`backend/scadbuddy`, migrations aside, finds no module that names them
(`test_no_code_names_the_old_tables` pins it). The Temporal activities in
`spools/activities.py` and `workflows/spool_refresh.py` only keep the retired activity
names for old histories; they touch no table.

| Table | Created by | Why it is safe |
| --- | --- | --- |
| `native_spool_uses` | `20261101T1849Z` | Carried into `filament_spool_events` by `20261114T1302Z`. Child of `native_spools`. |
| `native_spools` | `20261101T1740Z` | Carried into `filament_spools` by `20261114T1302Z`. |
| `spool_record_links` | `20261018T1744Z` | Bambuddy links carried to `filament_bb_links` by `20261116T0034Z`. |
| `spool_provider_records` | `20261018T1744Z` | A cache of provider answers. Nothing reads it. |
| `spool_provider_refreshes` | `20261018T1744Z` | Refresh status of the removed providers. `e2e-stack/seed.sh` no longer waits on it. |
| `tray_spool_links` | `20261018T1744Z` | Carried to tray places by `20261116T0547Z`. Its foreign key to `printers` goes with it. |
| `physical_spools` | `20261018T1744Z` | Tags carried by `20261114T1302Z`. Parent of the two link tables. |

## Decisions

- One migration, `DROP TABLE IF EXISTS`, children first, no CASCADE. Only the three
  foreign keys named above exist (`native_spool_uses`, `spool_record_links`,
  `tray_spool_links`); all are between dropped tables.
- No merged migration is edited. Tests that exercise the carry migrations stop before the
  drop (`MIGRATIONS[:index]`), so they still insert into and read the old tables.
- `test_a_retired_record_link_is_not_read` is removed: it needed `physical_spools`, and
  the table cannot exist now.
- `test_every_reference_to_a_printer_is_merged` loses its `tray_spool_links` exception.
- `test_revoke.py`'s `PRINT_HELD` list loses the seven names. `auth/revoke.py` names no
  table, so it needs no change.
- No workflow code changes, so the recorded spool-refresh histories replay as before.

## Review Focus

- Rollback: the drop loses data. The carry migrations copied it a deploy cycle ago. A
  rollback to a pre-#6262 build would fail on the missing tables; #6262 is past that.
- Grants and revoke: no role holds these tables now. `RETIRED_ROLES` revokes by role, not
  by table.
- Ledger: the new file sorts last (`20261118T1235Z`, after `20261118T0738Z`).
- Dependencies: no view, function or other table depends on them (checked by the plain
  `DROP` succeeding in the migration test, with no CASCADE).
- Carry tests: `test_from_native.py`, `test_carry_tray_links.py` and
  `test_plan_numbers_migration.py` run up to the drop, not past it.
