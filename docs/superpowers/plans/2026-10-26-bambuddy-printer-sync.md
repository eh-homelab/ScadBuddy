# Bambuddy Printer Sync and Virtual Printers: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.

**Goal:** One idempotent sync fills the printer registry from Bambuddy. It does three things. It creates or links a row for each Bambuddy printer that has a serial (#4439). It fills an empty LAN link from what Bambuddy reports (#4747 item 2). It imports Bambuddy's virtual printers as rows of their own kind (#4747 item 3). A second, stacked PR lets ScadBuddy manage Bambuddy's virtual printers (#4747 item 4).

**Issues:** #4439 and #4747. The body of #4747 is the design of record, and its 2026-10-26 comment on #4439 changes "never touch LAN links" to "fill empty ones".

## What Bambuddy gives (measured, with sources)

All code references are to `maziggy/bambuddy` at tag `v1.3.0b1-daily.20261025`, read on 2026-10-26.

- **`GET /api/v1/printers/` and `/printers/{id}`** carry `access_code` only for a caller with an Admin or Operator JWT, or when auth is off. "Viewers and API keys never receive it" (`backend/app/api/routes/printers.py`, `_caller_can_view_printer_secrets`, lines 126-150, and `get_printer` at 393-410). ScadBuddy uses an API key. So:
  - With Bambuddy auth **off**, the code arrives and the sync can fill the LAN link. Prod is in this state: the coordinator measured `GET /api/v1/auth/status` as `{"auth_enabled":false}` on 2026-10-26.
  - With auth **on**, the code is null. The row is reported as skipped with the reason "Bambuddy withholds the access code from API keys; link the LAN in Settings > Printers".
- **`GET /api/v1/virtual-printers`** returns `{printers: [...], models: {code: name}}` and needs `SETTINGS_READ`, which an API key gets through `can_read_status` (`backend/app/core/auth.py` `_APIKEY_SCOPE_BY_PERMISSION`, line 130).
- **A VP's `serial` is its target's real serial in `proxy` mode.** In the other modes it is a self-generated one (`routes/virtual_printers.py` `_vp_to_dict`, lines 94-135). So a VP row is keyed by its VP id, never adopted by serial from a row that links a physical Bambuddy printer.
- **VP writes** (`POST`, `PUT /{id}` and `DELETE /{id}` on `/virtual-printers`) need `SETTINGS_UPDATE`. That permission is admin-only, and an API key cannot hold it (`auth.py` lines 79-82 and 250). With auth on, a write is refused with 403, so ScadBuddy reports a scope of its own for it (PR 2).
- **The VP modes** are `archive`, `review`, `queue` and `proxy`. `immediate` is an alias for `archive`, and `print_queue` for `queue` (`backend/app/models/virtual_printer.py` `VP_MODE_VALUES`, `_VP_MODE_ALIASES`). An access code is exactly 8 characters (`virtual_printers.py`, lines 189-190). A non-proxy VP with a target always inherits the target's code (lines 236-240). Proxy mode needs a target when the VP is enabled.
- **Models:** `GET /api/v1/settings/virtual-printer/models` returns `{models: {code: name}, default}` (`routes/settings.py`, line 1809). The codes are SSDP codes such as `O1C` for H2C.
- **Diagnostic:** `GET /virtual-printers/{id}/diagnostic` returns `VPDiagnosticResult` `{vp_id, vp_name, mode, overall, checks: [{id, status, params}]}`. A check's `status` is `pass`, `fail`, `warn` or `skip` (`schemas/printer.py` `DiagnosticCheck`).
- **`GET /api/v1/auth/status`** returns `{auth_enabled, requires_setup}`. The client already reads it (`sees_every_users_trash`).

## Rulings (what the issues leave open)

1. **Schema.** One new migration adds these columns to `printers`:
   - `kind` (`physical` | `bambuddy_virtual`, default `physical`).
   - `bambuddy_vp_id` (integer, unique).
   - `forwards_to` (uuid, references `printers(id)` `ON DELETE SET NULL`). It is listed in `PRINTER_REFERENCES`, so a merge repoints it.
   - `vp_mode` (text).
   - `bambuddy_view` (jsonb): Bambuddy's values when the row was last synced.
   - `lan_sync_error` (text): the last reason the sync could not fill or move the LAN link. It is a fixed sentence and never names the code.
2. **The physical rules** (from #4439, as a pure planner over Bambuddy's active printers):
   - Linked already: the row is `already_linked`. Its name is refreshed only when it still equals Bambuddy's last-seen name (`bambuddy_view.name`), so a name the user typed is kept. Its model is refreshed when it is empty.
   - No serial: `skipped`, with `discover_printers`' reason.
   - The serial's row is unlinked and is not a VP: `linked`, unless the user unlinked this Bambuddy printer from that row (`bambuddy_unlinked_id`), which is `skipped`.
   - The serial's row links another Bambuddy printer or is a VP: `conflict`.
   - No row: `created`, with `preferred_path` `auto`.
   - A store refusal raised by a race is reported as `conflict`. The sync never unlinks or deletes a row.
3. **The LAN rules** run per linked row whose serial is real:
   - No LAN link, and Bambuddy gives an IP and a code: probe through the same function as `PUT /printers/{id}/lan`. On success, store the link. On failure, set `lan_sync_error`, and the next sync tries again.
   - No LAN link, and the code is withheld: `skipped`, and the reason is recorded.
   - A LAN link exists and Bambuddy's IP moved: this applies only when `lan_host` equals the previous `bambuddy_view.ip_address` and the new IP differs. Probe the new IP with the stored code first, then with Bambuddy's code if it differs. Re-link on the first that passes. Otherwise keep the old link and record the failure.
   - Any other existing link is never touched.
   - The code is read from Bambuddy inside the process that runs the sync: the API for the route, and the `library` worker, which is in the API process, for the sweep. It never enters a Temporal payload, a log, a span, an activity row or a response. `BambuddyClient.printer_access_code` returns it alone, and the `Printer` model still excludes it.
4. **The VP rules.** A row is matched by `bambuddy_vp_id`.
   - Else a row holds the VP's serial with no Bambuddy link and no VP id: that row is adopted. It keeps its LAN link and gets the kind. This is prod's hand-made row for `09400A391800001`, the queue-mode VP's own serial.
   - Else a new row is made with the VP's serial, or with `bambuddy-vp-<id>` when the serial is taken (a proxy VP) or not a serial's shape.
   - `forwards_to` is the row that links `target_printer_id`. With no target it is null, which means "any <model>".
   - `model` is Bambuddy's `model_name`. `vp_mode` is the mode.
   - The sync makes no LAN link for a VP. A VP Bambuddy no longer lists is reported as `missing` and never deleted.
5. **A VP is not hardware.**
   - `/print/printers/now` skips it.
   - `lan_printers` and `all_native` skip it, so `hot_path_native` never sees it.
   - `native_choices_for` skips it, and so do the tray-spool routes, with a 409.
   - The PrintPicker lists only Bambuddy printers and `lan_printers`, so it never offers one.
   - `PrinterRow` carries `kind`, `bambuddy_vp_id`, `forwards_to`, `vp_mode`, `bambuddy_view` and `lan_sync_error`.
6. **Where the sync runs.**
   - `POST /api/v1/printers/sync-bambuddy?dry_run=` runs it in the API. It is idempotent, so it is a plain route and not an operation. A dry run plans and writes nothing.
   - The Settings > Bambuddy page calls it after a save whose connection test passes, and shows the summary.
   - The `housekeeping_sync_bambuddy_printers` sweep runs it, behind `workflow.patched("housekeeping-sync-bambuddy-printers")`. It is on both Schedules, so a failed probe is tried again within `PRUNE_INTERVAL`. A new replay history is recorded beside the old ones.
   - The connection test itself stays write-free, because the agent's `read` tier reaches it.
7. **Agent:** `sync_printers_from_bambuddy` is `outward`, since it stores access codes. Its `dry_run` is a plain argument. `list_printers` and `get_printer` already return the row, which now says its kind and Bambuddy's values.
8. **PR 2.**
   - The routes are `GET/POST /api/v1/printers/virtual`, `GET/PUT/DELETE /api/v1/printers/virtual/{vp_id}`, `GET /api/v1/printers/virtual/{vp_id}/diagnostic` and `GET /api/v1/printers/virtual/meta` (the models, the default and `auth_enabled`).
   - The writes are the operation kinds `bambuddy_vp_create`, `bambuddy_vp_update` and `bambuddy_vp_delete` on the `bambuddy` queue, through `run_operation` with an `Idempotency-Key`.
   - A target is given as a registry printer id, and the check maps it to `bambuddy_printer_id`.
   - **The access code never enters the operation record or the Temporal payload in the clear.** The route seals it under the KEK, with the AAD `vp-access-code:<idempotency key>`, before `run_operation`. The run opens it. With no KEK, a request that carries a code is refused with 503.
   - A 403 on a VP write maps to a new `Scope.ADMIN_SETTINGS`, which says the key needs Bambuddy auth off or an admin.
   - After each write the run calls the sync through the API's internal route, so the registry follows.
   - The panel shows "Bambuddy auth: on/off" from `/auth/status`.

## Tasks, PR 1 (`feat/bambuddy-printer-sync`)

- [ ] 1. Migration `<ts>_printer_kinds.sql`; extend `PrinterRow`, `_COLUMNS`, `PRINTER_REFERENCES` (`forwards_to`); store methods `create_linked`, `record_bambuddy_view`, `set_lan_sync_error`, `upsert_virtual`, `rename_if_unchanged`. Test: `tests/printers/test_store.py` FK coverage, the new methods.
- [ ] 2. `BambuddyClient.printer_access_code(id)` and `virtual_printers()`; the models `VirtualPrinter` and `VirtualPrinterList`. Recorded fixture `virtual-printers.json`, built from the shape #4747 records and with the access code absent.
- [ ] 3. Factor the LAN probe and link out of `api/printers.py` `link_lan` into `printers/lan_link.py` `probe_and_link`, which raises typed errors. The route maps them as it does today.
- [ ] 4. `printers/bambuddy_sync.py`: `plan_sync` (pure) and `run_sync(printers, client, dry_run)` → `PrinterSyncResult`. Tests cover every rule in Rulings 2-4, idempotence (a second run changes nothing), and that the code appears in no log record or response.
- [ ] 5. Route `POST /printers/sync-bambuddy`. Tests in `tests/api/test_printers.py`.
- [ ] 6. VP exclusions (Ruling 5). Tests in `test_printers_now.py`, print_run `lan_printers`, and tray spools.
- [ ] 7. The housekeeping sweep and its patch, the activity in `main.py`, the timeout, a new replay history (`printer_sync.json`), and the `test_housekeeping` sweep lists.
- [ ] 8. Frontend:
  - Settings > Printers: an "Import from Bambuddy" button (disabled, with a hint, when Bambuddy is not configured), a result summary, Bambuddy's values beside each linked row with differences marked, and a VP row's line.
  - Settings > Bambuddy: run the import after a save whose test passes.
  - The msw mocks in `mocks/features/printers.ts`.
- [ ] 9. Agent: the `sync_printers_from_bambuddy` tool, and `coverage.ts` needs nothing, since the tool covers the route. Tests.
- [ ] 10. Checks, push, draft PR (`Fixes #4439`, `Fixes #4747`), watch CI.

## Tasks, PR 2 (`feat/bambuddy-virtual-printers`, stacked)

- [ ] 11. Client: `virtual_printer`, `create_virtual_printer`, `update_virtual_printer`, `delete_virtual_printer`, `virtual_printer_diagnostic`, `virtual_printer_models`, `auth_status`, and `Scope.ADMIN_SETTINGS`.
- [ ] 12. Operation kinds in `bambuddy/operations.py`, with the sealed access code, then the sync.
- [ ] 13. Routes under `/printers/virtual`.
- [ ] 14. Frontend "Bambuddy virtual printers" panel, with its mocks.
- [ ] 15. Agent tools (reads `read`, writes `outward`), coverage.
- [ ] 16. Checks, push, draft PR (`Part of #4747`), watch CI.
