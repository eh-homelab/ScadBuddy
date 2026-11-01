# The Print Hot Path Without Bambuddy (#3818 tier A) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A render, the print dialog, its background slice, the print and its follow make no Bambuddy call when the run is native (`store_backend=s3`, `library_backend=native`, `slicer_backend=native`, the printer's path `native`), whether or not `bambuddy_url` is set. A run on `store_backend=bambuddy` with the rest native takes the same print path (D2), but its store still reads and writes Bambuddy's library, so the zero-call promise (AC1) is made for `s3` only. Bambuddy stays configured and selectable; the hot path never waits on it.

**Architecture:** One predicate, `hot_path_native`, decides per run whether the print path may build a `BambuddyClient` at all; on a native run it passes `client=None` everywhere, and mypy then finds every call that still needs one. What those calls did is replaced by things ScadBuddy already owns or that the in-flight plans build: the preset catalogue and values from the image's BBL profiles (`slicing/profiles.py`), the laid-out per-print copy in the native library (`print_library/`), the destination as a native library folder, the filament choices as the printer's loaded trays from its MQTT report (`printer_reports`, plan 3a) with an overlay of spool-provider records cached in Postgres (Bambuddy's refreshed by a Schedule of its own, never inline), the `ams_mapping`/`ams_mapping2` and the H2C's `nozzle_mapping` computed from the plan, and the follow from the report. Bambuddy's projects, archives and queue stay on the Bambuddy path, untouched. Proof is a backend test that fails on any `bambuddy.*` client span or any HTTP request to the configured Bambuddy URL during a native render-to-follow run, a mocked e2e walk of the dialog with Bambuddy unconfigured, a real-stack e2e against a recorder that fails on any request, and a prod checklist read off Tempo.

**Tech Stack:** Python 3.12 (FastAPI, pydantic 2, psycopg 3, temporalio 1.34.0 per `backend/uv.lock`), respx, the OTel in-memory exporter the tests share (`backend/tests/conftest.py:60`), React 19, msw, Playwright, Docker Compose v2, `eclipse-mosquitto` 2 (the fake printer's broker), pyftpdlib (its FTPS server). External sources read 2026-10-18 and cited by commit: Doridian/OpenBambuAPI `cc383a2c`, greghesp/ha-bambulab `0e027ff1`, maziggy/bambuddy `0d427510`, bambulab/BambuStudio tag `v02.08.02.61`.

**Spec:** `docs/superpowers/specs/2026-10-16-own-store-slicer-rust-workers-design.md` §1.1 ("A render never waits on Bambuddy"), §5.3, §5.4, §6.2, §6.3 (what stays Bambuddy-only). Inventory: `.superpowers/sdd/bambuddy-dependency-inventory.md` §0, §1, §2 "Tier A" (in the main checkout, not committed). Every `file:line` below was re-read on origin/main `7613580bf` (2026-10-18) unless it names a branch; the inventory's were read on a branch and several have moved (Task-by-task notes say where).

## Decisions

### D1. Filaments on the hot path: trays live, plus an overlay from cached spool-provider records

**Chosen (the user's design, 2026-10-18):** a tray holds one specific *physical* spool; each spool provider (Bambuddy's inventory now, ScadBuddy's own native inventory in tier B, others later) keeps its own record of that physical spool; physical spool to provider records is 1:N. So:

- **`physical_spools`** is ScadBuddy's identity for a spool, keyed by what the printer reports when it can: the AMS tray's `tray_uuid` and `tag_uid` (the RFID fields of each `ams.ams[].tray[]` entry and of `vt_tray`; Doridian/OpenBambuAPI `mqtt.md` at `cc383a2c`, the `pushing.pushall` report example, read 2026-10-18; ha-bambulab `pybambu/models.py` `AMSTray.tag_uid`/`tray_uuid` at `0e027ff1`; both present in this repo's `tests/bambuddy/recordings/printer-status-rack.json:57-58`). An untagged spool reports all zeros (`"0000000000000000"`, 32 zeros for `tray_uuid`: the `mqtt.md` example, and Bambuddy clears an empty slot to those values, `backend/app/services/bambu_mqtt.py:343-344` at maziggy/bambuddy `0d427510`); all zeros is never stored as a key. An untagged spool gets its identity by **manual linking** (a person says which spool is in the tray), remembered against the tray's fingerprint so a changed tray drops the link, as `trays.py`'s `UnknownTray.fingerprint` already does (`bambuddy/trays.py:152`).
- **`spool_provider_records`** links a physical spool to one provider's record: provider name, the provider's record id, and the last-seen snapshot (material, colour, brand, remaining grams, label weight, the provider's own preset id), with when it was refreshed.
- **A provider interface**, `SpoolProvider`, registered the way features register kinds: each feature that provides spools exports `SPOOL_PROVIDERS` from its `scadbuddy/<feature>/spool_providers.py`, found by `core.components.feature_exports` (`core/components.py:169`, as `ACTIVITY_KINDS` is found, `activity/kinds.py:47-52`). No list is written by hand. Bambuddy's is the first (`scadbuddy/bambuddy/spool_providers.py`); tier B's native inventory is the second; consumption rules are designed there, not here.
- **The tray-to-physical-spool mapping** is read from the latest LAN report (`printer_reports`, 3a Task 6) at the time of the read: an RFID tray resolves by `tray_uuid` (else `tag_uid`) against `physical_spools`; an untagged tray by its manual link if the fingerprint still matches. Nothing is written on the read.

**The hot path never waits on Bambuddy:** the dialog reads the trays live from the LAN report and the overlay from `spool_provider_records` in Postgres. Bambuddy's records are refreshed only by a Temporal Schedule on the `library` queue (`spool-providers`, Task 6b), never inline and never from the dialog. An overlay older than `SPOOL_OVERLAY_STALE` (30 min, three missed refreshes at the default five-minute interval), or missing, degrades to trays only with a note naming the provider and when it last answered. Choices stay tray-addressed: the plan's slot choice is the tray (`tray_spool_id`, `bambuddy/trays.py:55`), the filament preset comes from the tray's `tray_info_idx` (`AmsTray.tray_info_idx`, `bambuddy/models.py:398`) through the native catalogue (Task 2), else from the overlay's provider preset for an untagged tray, replacing `spool_filament_presets` (`print_run.py:693`, in `resolve_plates`). The Bambuddy path keeps today's spool-first picker unchanged.

**Why this shape:** it is what a LAN print physically does (with no queue, §6.3, a LAN print draws from what is loaded), the plan keeps the tray ids that already flow through `FilamentPlan`, `resolve`, `tray_colours` and remembered choices, and the native inventory slots in as one more provider without touching the hot path.

**Sizes.** `physical_spools`: one row per physical spool ever loaded or linked, a few hundred in a home farm; `spool_provider_records`: at most one row per (spool, provider), plus one row per unmatched provider record kept for linking (Bambuddy's whole inventory, 12 rows in `tests/bambuddy/recordings/inventory-spools.json`, tens to hundreds live); `tray_spool_links`: at most one row per tray per printer (4 per AMS plus 2 external). The refresh rewrites a provider's rows in one transaction; nothing else writes them.

**Alternative recorded:** trays only (the plan's first draft, rejected by the user), and a minimal native spool table now (tier B's B-inventory, which this design makes "the native provider").

**On the native path until tier B:** shelf spools (not loaded anywhere) are shown in the overlay's linking list only, never as a print choice; the TrayQuestion that writes Bambuddy's assignment (`components/print/TrayQuestion.tsx`, route `post_tray_spool` `api/printing.py:518`) is replaced on a native run by the native manual link, which writes `tray_spool_links` only.

### D2. Which runs are native

`hot_path_native(settings, path)` (Task 9's signature) is true when `settings.slicer_backend == "native"`, `settings.library_backend == "native"`, `settings.store_backend in ("s3", "bambuddy")` and `path`, the printer row's resolved path (plan 3a Task 8), is `native`. Anything else is today's Bambuddy path, unchanged. `store_backend=bambuddy` still counts: its library calls are the store's, outside the print path, and §1.1's render goal is tier 1's (`store_backend=s3`); the acceptance tests run on `s3`.

### D3. A native run never builds a client

The print path's entry points take `client: BambuddyClient | None`. A native run passes `None` even when `bambuddy_url` is set, so "never waits on Bambuddy" is a type, not a convention. The one helper is `maybe_client(settings, *, native: bool)`: an async context yielding `None` when `native` or when no URL is configured, else `client_for(settings)`'s client (`bambuddy/client.py:1511`).

### D4. Printer state from the LAN link first

Confirmed by the user, 2026-10-18; the 3a plan's Tasks 6 and 8 are edited to match in this plan's commits. Plan 3a Task 6 as first written read a printer's status from Bambuddy whenever the row linked one. That is a Bambuddy call on the hot path for a printer with both links, so on a native run the status is `status_from_report` (3a Task 5) from `printer_reports`, and Bambuddy's `printer_status` is read only for a printer whose path is `bambuddy`. Task 9 makes that change in `prepare_run`, `choices_for` and `printers_now`.

### D5. Presets on a native run come from the image only

The catalogue is the image's BBL tree (Task 2). Bambuddy's `local` presets (OrcaSlicer imports, `/local-presets/`, `bambuddy/catalogue.py:109-114`) and cloud *user* presets are not offered on a native run, and a remembered one is refused with a message naming it. The native slicer already refuses a `local` printer or filament preset and reads a `local` process preset's values only from Bambuddy (`slicing/presets.py` docstring on branch `feat/native-slicer-caller`, measured 2026-10-18), which would be a Bambuddy call on the hot path.

Revised by #4645 for `local` presets: a native run offers and slices them from a kept copy (`slicing/held_presets.py`), refreshed off the hot path on the spool providers' Schedule, never from Bambuddy. Cloud user presets stay as above (#5113).

### D6. The destination is a native folder; projects are the Bambuddy path's

Confirmed by the user, 2026-10-18. A native run files its laid-out copy in the native library: in the request's `folder_id` when it names a native folder, else in the output's `<Template title>/` folder the filing already uses (`print_library/filing.py:16`, `folder_name(title)`), else at the top level for a library file. `project_id` is refused on a native run (422 naming the Bambuddy path), and `last_project_id` is ignored there. This revises LIB Ruling 7 (`docs/superpowers/plans/2026-10-17-native-print-library.md`, "Print destinations stay Bambuddy's folders") for native runs only; the Bambuddy path is unchanged.

### D7. Rack prints print natively: measure the command, then send it

The H2C's hotend rack is chosen per filament group, and the sliced file does not say which rack position a group prints from. What the G-code carries is the tool changes and which *extruder* each filament prints on (the plate's `filament_map`/`nozzle_group_result`, which Bambu Studio's slice writes and its print dialog reads, `src/slic3r/GUI/DeviceCore/DevMappingNozzle.cpp` `CtrlGetAutoNozzleMappingV0/V1` at v02.08.02.61; in the 3MF, `slice_info.config`'s `<filament id group_id …>` and `<nozzle id extruder_id>`, as Bambuddy's tests write them, `backend/tests/integration/test_scheduler_nozzle_rack_dispatch_1784.py` at `0d427510`). What the print command must add is the rack-slot-to-filament mapping: `project_file`'s `nozzle_mapping`, one physical nozzle id per filament slot (the fixed hotend `1`, rack positions `16`–`21`, `-1` unused), 32 entries on an H2C (Bambuddy `backend/app/services/bambu_mqtt.py:351-405` `_RACK_NOZZLE_IDS`, `_RACK_WIRE_SLOTS`, `_FIXED_NOZZLE_ID`, measured by its maintainer on an H2C 2026-08-13/14 and recorded there; `resolve_rack_plan_mapping` `:575` builds it from a per-group position pick, which is what ScadBuddy sends Bambuddy as `nozzle_rack_choice`, `bambuddy/dispatch.py:278-311`). Bambu Studio itself gets the mapping by asking the printer (`print.get_auto_nozzle_mapping`, request and reply shapes in `DevMappingNozzle.cpp`) and sends it back as `nozzle_mapping` (`PrintJob.cpp:262`, `SelectMachine.cpp:3322-3323`); the MQTT encoding of the parameters is inside Bambu's closed networking plugin (`src/slic3r/Utils/bambu_networking.hpp:237-240` declares only the strings), which is why Task 8 measures the wire before anything is built on it.

So Task 8 measures first, with the user at the printer, then the LAN path sends `nozzle_mapping` from ScadBuddy's own rack pick (`rack_chooser`, `print_run.py:578-670` at `7613580bf`). No refusal ships. **Decide then:** only if the measurement finds the printer cannot be told the mapping over LAN does the plan choose a fallback (refuse with a reason, let the firmware pick as Bambuddy does when it cannot resolve one, `bambu_mqtt.py` `resolve_rack_nozzle_mapping` docstring, or hand the run to Bambuddy); that choice goes to the user with the measurement.

## Acceptance criteria and the layer each lives in

| # | Criterion | Layer |
|---|---|---|
| AC1 | On `store_backend=s3`, a native render → dialog → background slice → print → follow makes zero `bambuddy.*` client spans and zero HTTP requests to `bambuddy_url`, with Bambuddy configured (on `store_backend=bambuddy` the store's own calls remain, D2) | backend acceptance test (Task 9); real-stack e2e with a failing recorder (Task 13); prod Tempo check (Task 14) |
| AC2 | The same run works with `bambuddy_url` unset, and the app starts | backend acceptance test (Task 10); real-stack e2e second pass (Task 13) |
| AC3 | The dialog shows the native slice's progress bar and its result | mocked e2e (Task 11); real-stack e2e (Task 13) |
| AC4 | Filament choices are the printer's loaded trays with the providers' cached records over them; the chosen trays reach `ams_mapping`/`ams_mapping2` | unit (Tasks 6a, 6c, 7); backend acceptance (Task 9, the fake printer's recorded `project_file`); real-stack e2e (Task 13, the fake printer's record) |
| AC4b | A stale or missing overlay degrades to trays only with a note; the dialog never starts or waits on a refresh | unit (Task 6c); backend acceptance (Task 9, an overlay older than `SPOOL_OVERLAY_STALE`); mocked e2e (Task 11) |
| AC5 | The per-print copy is a native library file; the slicer reads it without a copy | backend (Task 4); acceptance (Task 9) |
| AC6 | The print is followed to `completed` in Now printing from the printer's report | mocked e2e (Task 11); real-stack e2e (Task 13); prod (Task 14) |
| AC7 | A rack print on a native run sends the measured `nozzle_mapping`, and the H2C prints from the hotends ScadBuddy picked | measurement and live test with the user (Task 8); backend (Task 8, the fake LAN's recorded command) |
| Outside AC1 | The overlay's refresh (`SpoolRefresh`, Task 6b) reads Bambuddy on its own Schedule and its own traces; it is not the hot path and is not counted. The backend acceptance test refreshes once before its zero-call window opens and runs with `spool_overlay_refresh_seconds=0`; the real stack triggers the Schedule once in its seed, then resets the recorder; the prod check reads only the hot path's traces. | Tasks 6b, 9, 13, 14 |
| AC8 | The Bambuddy path is unchanged | every existing `tests/api/test_print_*.py`, `tests/test_print_replay.py`, `e2e/print.spec.ts` |

## Global Constraints

- The Bambuddy path's behaviour does not change: every existing print test, every recorded `PrintRunWorkflow` history (`tests/test_print_replay.py`) and `e2e/print.spec.ts` pass unedited, except where a test asserts a call this plan moves (none expected; a task that must edit one says why in its commit).
- `PrintRunWorkflow` changes go behind `workflow.patched(...)`; this plan expects none of its own (the native branch is 3a Task 9's `NATIVE_PATCH`, the native slice is slicer Task 13's `native-slicer`). A task that finds it needs one stops and reports.
- Schema changes are NEW files `backend/scadbuddy/migrations/$(date -u +%Y%m%dT%H%MZ)_<slug>.sql`; never edit a merged one. This plan adds one (Task 6a, `physical_spools`).
- Nothing new on a pod's disk (`CLAUDE.md` "Layout", memory "ScadBuddy uses Postgres, not files"): the native catalogue is built in memory from the image's profiles, never written.
- No trace attribute, log line or metric label carries a preset name a person typed, a file name, a transfer URL or an access code (`core/trace_scrub.py`).
- Every external or measured fact gets a citation in the docstring that relies on it (#259). Where this plan's sources disagree or leave a gap (the `ams_mapping` shape, `nozzle_mapping` on the wire, `tray_uuid` against `tag_uid` per spool), the task measures it first and records how.
- Backend checks, in `backend/`: `uv run --frozen ruff check . && uv run --frozen ruff format --check . && uv run --frozen mypy && uv run --frozen pytest -n auto`, with `SCADBUDDY_TEST_DATABASE_URL` and a Temporal (`CLAUDE.md` "Commands"). Frontend: `pnpm lint && pnpm typecheck && pnpm test && pnpm build`, then `pnpm exec playwright test`.
- Every new `/api/v1` operation needs an agent tool or a `NOT_A_TOOL` entry (`agent/src/tools/coverage.ts`); this plan adds hidden internal routes (`include_in_schema=False`) and the tray-spool link routes (Task 6c), which get agent tools.
- Commits: conventional titles; never amend; end each message with the attribution lines the executing session is given.
- Cleanup of test processes by pid or process group only; never `pkill -f`, `pkill <name>` or `killall`.

## Review Focus

1. **A Bambuddy call hidden behind a default.** A helper that builds its own client (`client_for` inside a reader, `cached_catalogue`'s first read, `read_once`) runs on a native run because nobody passed it `None`. Expected: the acceptance test's recorder fails on it, naming the span. Test in Task 9 (`test_a_native_run_makes_no_bambuddy_call`).
2. **A native file id handed to Bambuddy, or a Bambuddy id to the native library.** `output_bambuddy_uploads` holds both id spaces after Task 4; `_still_there` and `recorded_slice` must route each id by `is_native_id` (`print_library/ids.py:11`). Expected: a native copy is checked against the native library, a Bambuddy one against Bambuddy, and neither is reused across a flip of `library_backend`. Test in Task 4 (`test_a_native_copy_is_never_looked_up_in_bambuddy`).
3. **The wrong tray in `ams_mapping`.** The AMS-HT's id space (128+), the external holders (254/255) and a slot the plate does not print with each change the number. Expected: each case pinned by a vector. Test in Task 7.
4. **A remembered spool id on a native run.** A person's remembered choices name a Bambuddy spool (positive id). Expected: the dialog opens with that slot unchosen and the tray of the closest colour suggested, never a 500 or a call to Bambuddy. Test in Task 6c (`test_a_remembered_spool_is_dropped_on_a_native_run`).
5. **A printer with both links whose report is stale.** `auto` picks Bambuddy (3a Task 8). Expected: the run is then not native, today's path runs, and the acceptance test's mode is not entered. Test in Task 9 (`test_a_stale_report_runs_the_bambuddy_path`).
6. **A rack position off by the namespace.** Rack *positions* (1–6), physical nozzle ids (16–21, the fixed hotend 1) and *extruder indices* (0 the rack, 1 the fixed hotend on the H2C) overlap as small numbers; Bambuddy printed a plate in mid-air by passing one for another (`bambu_mqtt.py:355-404`, #2800). Expected: `nozzle_mapping` is built only from positions through `rack_nozzle_id`, and a vector pins each namespace. Test in Task 8.
7. **Two providers claiming one physical spool, or one provider record claimed by two.** Expected: `UNIQUE (physical_spool_id, provider)` and the `(provider, record_id)` key hold; a manual link moves a record, it never duplicates one. Test in Task 6a.
8. **An overlay refresh on the hot path.** Expected: nothing in the dialog's routes or the run starts, signals or awaits `SpoolRefresh`; Task 9's zero-call window would catch a refresh's Bambuddy read. Test in Tasks 6c and 9.

---

## Ordering against the in-flight branches

| Task | Can start | Depends on | Overlapping files on in-flight branches |
|---|---|---|---|
| 1 Harness | now | — | none |
| 2 Native catalogue | after PR #4162 merges (slicer Task 8, `slicing/profiles.py`) | slicer Task 8 | `bambuddy/print_run.py` (slicer Task 13 `PlannedRun`/`plan_run`; 3a Task 8 `prepare_run`/`plan_run`), `slicing/presets.py` (#4162) |
| 3 Native preset values | after Task 2 | Task 2 | `bambuddy/print_run.py` (as above) |
| 4 Per-print copy native | now | LIB (merged) | `bambuddy/print_source.py` (slicer Task 13, `:489` record), `bambuddy/uploads.py` (slicer Task 13 `SlicedCopy`), `worker.py` `build_print_deps` (slicer Tasks 12–13; 3a Task 6 `PrintWorkerDeps`) |
| 5 Native destination | after Task 4 | Task 4 | `workflows/print_activities.py` `check` (3a Task 8), `bambuddy/print_run.py` `prepare_run` (3a Task 8) |
| 6a Physical spools, providers | after 3a Task 2 merges (the `printers` table its links reference) | 3a Task 2 | none |
| 6b Bambuddy provider, refresh Schedule | after Task 6a; its `seen` half after 3a Task 6 (`PrinterReportStore`) | 6a, 3a Task 6 | `main.py` `_run_library_worker` (slicer Task 11 registers `SliceWorkflow` there) |
| 6c Tray-first options with the overlay | after 6a; its resolver step after Task 2 | 6a, Task 2 | `bambuddy/filaments.py`, `bambuddy/trays.py`, `bambuddy/resolver.py` (none in flight); `agent/src/tools/printers.ts` (3a Task 11 creates it) |
| 7 `ams_mapping`, `ams_mapping2` | now (pure); its live check in Task 8's session | — | none; consumed by 3a Task 9 (`ProjectFile` gains `ams_mapping2`) |
| 8 Rack: measure, then send `nozzle_mapping` | the measurement as soon as 3a Task 4 (`LanPrinter`, uncommitted on `feat/printer-registry`) lands, with the user at the H2C; the sending after 3a Task 9 | 3a Tasks 4, 9; slicer Task 9 (`read_sliced`) | `printers/lan.py` (3a Task 4), `printers/print_activities.py` (3a Task 9), `bambuddy/print_run.py` `rack_chooser` |
| 9 Native mode, optional client, AC1 | last backend task | Tasks 2–8, slicer Tasks 11–13, 3a Tasks 5, 6, 8, 9 | `bambuddy/print_run.py`, `workflows/print_activities.py`, `api/deps.py`, `worker.py`, `bambuddy/progress.py` (3a Task 9), `bambuddy/printers_now.py` (3a Task 6), `bambuddy/choices.py` |
| 10 Unset `bambuddy_url`, AC2 | after Task 9 | Task 9 | `api/printing.py`, `api/library_print.py` (slicer Task 12's read routes) |
| 11 Frontend + mocked e2e | after slicer Task 14, 3a Task 10, Tasks 5, 6c, 10 | those | `PreviewPane.tsx`, `usePreviewSlice.ts`, mocks `printPreview.ts`/`settings.ts` (slicer Task 14); `PrintPicker.tsx`, `NowPrinting.tsx`, mocks `printersNow.ts` (3a Task 10) |
| 12 Fake printer + Bambuddy recorder | now (upload dir configurable); its default after 3a Task 7 | 3a Task 7 (measured `UPLOAD_DIR`, `file_url`) | none |
| 13 Real-stack e2e | after Tasks 10–12 and the slicer image (slicer Task 5, merged in #4058) | those | `frontend/e2e/` only |
| 14 Docs, checklist, handoff | last | all | spec §5.4, §6.3; `README.md` "Deploying" (slicer Task 15, 3a Task 12) |

The `PrintRunWorkflow`/`bambuddy/print_run.py` overlap is the big one: slicer Task 13 adds `PlannedRun.slicer`/`library_queue` and the `slice_run` branch; 3a Task 8 adds `PlannedRun.printer`/`path` and the registry resolution in `prepare_run`; 3a Task 9 adds the LAN branch in `_print`. Tasks 2, 3 and 5 here change only `prepare_run`'s and `plan_run`'s *reads* (the catalogue, the values reader, the destination check), never `PlannedRun`'s fields or the workflow, so they rebase over either branch with textual conflicts only. Task 9 is the one that edits the same functions semantically, which is why it waits for all three.

---

### Task 1: The zero-Bambuddy harness

**Files:**
- Create: `backend/tests/support/no_bambuddy.py`
- Test: `backend/tests/test_no_bambuddy.py`

**Interfaces:**
- Consumes: the shared span exporter (`tests/conftest.py:60` `_SPANS`, fixture `spans` `:84`), the client span name `f"bambuddy.{operation}"` with `SpanKind.CLIENT` (`bambuddy/client.py:200-211`), respx (as `tests/api/test_print_run_choices.py` uses it).
- Produces:
  ```python
  class BambuddyCalledError(Exception): ...      # names each span and each request (ruff N818; an Exception so pytest.raises(AssertionError) cannot swallow it)
  @contextmanager
  def forbid_bambuddy(spans: InMemorySpanExporter, base_url: str) -> Iterator[BambuddyRecord]:
      # clears the exporter, mounts a respx router that records and answers 599 for every request
      # whose URL starts with base_url (assert_all_called=False, assert_all_mocked=False so other
      # hosts pass through), and on exit raises BambuddyCalledError if any request was recorded or any
      # finished span's name starts with "bambuddy." — listing the span names and request paths
  class BambuddyRecord: requests: list[httpx.Request]; spans: list[str]
  ```
  The 599 makes a leaked call fail the code under test too, so a swallowed call (a "best effort" read) is still caught by the record.

- [ ] **Step 1: Write the failing tests**

```python
async def test_a_bambuddy_request_is_named(spans):
    with pytest.raises(BambuddyCalledError, match="/api/v1/printers/"):
        with forbid_bambuddy(spans, "http://bambuddy.test"):
            async with httpx.AsyncClient() as http:
                await http.get("http://bambuddy.test/api/v1/printers/")

async def test_a_bambuddy_span_is_named(spans):
    with pytest.raises(BambuddyCalledError, match="bambuddy.printers"):
        with forbid_bambuddy(spans, "http://unused.test"):
            with span("bambuddy.printers", kind=SpanKind.CLIENT): pass

async def test_other_hosts_pass(spans, respx_mock):
    respx_mock.get("http://api.test/x").respond(200)
    with forbid_bambuddy(spans, "http://bambuddy.test"):
        async with httpx.AsyncClient() as http:
            assert (await http.get("http://api.test/x")).status_code == 200
```

- [ ] **Step 2: Run them to verify they fail** — `cd backend && uv run --frozen pytest tests/test_no_bambuddy.py -v` → `ModuleNotFoundError`.
- [ ] **Step 3: Implement** `no_bambuddy.py`.
- [ ] **Step 4: Run** Step 2's command → PASS; then `uv run --frozen mypy && uv run --frozen ruff check . && uv run --frozen ruff format --check .`.
- [ ] **Step 5: Commit**

```bash
git add backend/tests/support/no_bambuddy.py backend/tests/test_no_bambuddy.py
git commit -m "test(print): a harness that fails on any Bambuddy span or request (#3818)"
```

---

### Task 2: The preset catalogue from the BBL profiles (A-catalogue)

**Files:**
- Create: `backend/scadbuddy/slicing/catalogue.py`
- Modify: `backend/scadbuddy/bambuddy/catalogue.py` (`PresetChoice` `:39` gains `filament_id: str | None = None`; a `catalogue_for` entry point), `backend/scadbuddy/bambuddy/resolver.py` (`resolve` `:238`: a tray choice's preset by `filament_id`), `backend/scadbuddy/bambuddy/print_run.py` (`_catalogue(client)` at `:1319` in `prepare_run` and `:1586` in `plan_run` → `catalogue_for`), `backend/scadbuddy/bambuddy/choices.py` (`cached_catalogue` in `choices_for`, `:156`), `backend/scadbuddy/api/filament_profiles.py` (`:66`, inventory's ref), `backend/scadbuddy/slicing/presets.py` (`_Names` `:102-114` on `feat/native-slicer-caller`: names from the native catalogue when there is no client)
- Test: `backend/tests/slicing/test_catalogue.py`, fixtures under `backend/tests/slicing/profiles/` (slicer Task 8's tree, plus a filament with `filament_id` and `compatible_printers`, and a base preset with `"instantiation": "false"`)

**Interfaces:**
- Consumes: `Profiles` (`slicing/profiles.py` on `feat/native-slicer-caller`: `find`, `machine`, `process`, `filament`, `version`, the `BBL.json` lists), `_Catalogue`/`PresetChoice`/`PresetRef` (`bambuddy/catalogue.py:39,89`; `bambuddy/models.py:34`).
- Produces:
  ```python
  # slicing/catalogue.py
  def native_catalogue(profiles: Profiles) -> _Catalogue
      # every instantiable machine, process and filament preset of the tree, flattened for
      # compatible_printers, filament_type, default_filament_colour (first) and filament_id;
      # ref = PresetRef(source="standard", id=<name>), which is what Bambuddy's `standard` ids
      # are (measured 2026-10-18, slicing/profiles.py docstring), so a remembered standard ref
      # resolves unchanged. Built once per Profiles.version in a thread and kept in memory
      # (functools.cache on the version): the tree is immutable in an image.
  def native_ref(profiles: Profiles, ref: PresetRef) -> PresetRef | None
      # a `cloud` ref (a setting_id, same measurement) → the standard ref of the preset it names;
      # `standard` as is when found; `local`, `orca_cloud` and anything not in the tree → None
  # bambuddy/catalogue.py
  async def catalogue_for(client: BambuddyClient | None, *, native: Profiles | None) -> _Catalogue
      # native given → native_catalogue(native); else _catalogue(client) (client required)
  ```
  `resolve` (`resolver.py:238`): for a slot whose option is a tray (`spool_id < 0`) with `slicer_filament` set, the preset is the catalogue filament whose `filament_id == option.slicer_filament` and that `_fits` the printer (`resolver.py:143`), before the name fallbacks; a `filament_overrides` ref or a remembered process/printer ref is passed through `native_ref` first on a native catalogue, and one that maps to `None` is the error "the preset <name> is Bambuddy's (<source>); a native print uses Bambu Studio's own presets: pick one under Advanced" (D5).
  The resolver's H2C assumption (`PRINTER_MODEL = "H2C"`, `resolver.py:54`; `"@BBL H2C"` names `:61-78`, `:307-309`) is kept, not widened.

Facts to verify before relying on them (record the answers, with how measured, in `catalogue.py`'s docstring):
- That base presets carry `"instantiation": "false"` in the v02.08.02.61 tree and are not offered by Bambu Studio (`PresetBundle::load_vendor_configs_from_json`, the same file slicer Task 8 read); count instantiable presets per kind.
- That an AMS `tray_info_idx` equals a BBL filament preset's `filament_id` (compare `tests/bambuddy/recordings/printer-status*.json` `tray_info_idx` values with the tree's `filament_id`s; e.g. Bambu Studio's `GFA00` for "Bambu PLA Basic").
- How long `native_catalogue` takes on the full tree in the test image (`time` around one call); if above 2 s, say so in the docstring: it runs once per process.

- [ ] **Step 1: Write the failing tests**

```python
def test_the_native_catalogue_offers_only_instantiable_presets(profiles):
    names = {c.name for c in native_catalogue(profiles).filament}
    assert "fdm_filament_base" not in names and "Bambu PLA Basic @BBL H2C" in names

def test_a_standard_ref_is_the_preset_name(profiles):
    row = next(c for c in native_catalogue(profiles).process if c.name == "0.20mm Standard @BBL H2C")
    assert row.ref == PresetRef(source="standard", id="0.20mm Standard @BBL H2C")

def test_a_cloud_ref_maps_to_its_standard_ref(profiles):
    assert native_ref(profiles, PresetRef(source="cloud", id="GFSA00_99")).id == "Bambu PLA Basic @BBL H2C"

def test_a_local_ref_has_no_native_ref(profiles):
    assert native_ref(profiles, PresetRef(source="local", id="7")) is None

def test_a_tray_resolves_its_preset_by_filament_id(profiles):
    options = tray_options(tray_info_idx="GFA00")                # helper: one slot, one tray choice
    resolved = resolve(options, plan_for_tray(options), h2c_choices(), native_catalogue(profiles), {})
    assert resolved.filament_presets[0].id == "Bambu PLA Basic @BBL H2C" and not resolved.errors

def test_a_local_override_is_refused_on_native(profiles):
    choices = h2c_choices(filament_overrides={1: PresetRef(source="local", id="7")})
    resolved = resolve(tray_options("GFA00"), plan_for_tray(...), choices, native_catalogue(profiles), {})
    assert "Bambuddy's (local)" in resolved.errors[0].message

async def test_catalogue_for_native_needs_no_client(profiles):
    assert (await catalogue_for(None, native=profiles)).process

@pytest.mark.requires_slicer_profiles
def test_the_images_tree_builds(): ...                          # counts per kind, > 0 each
```

- [ ] **Step 2: Run them to verify they fail** — `cd backend && uv run --frozen pytest tests/slicing/test_catalogue.py -v` → import errors.
- [ ] **Step 3: Implement** `slicing/catalogue.py`, `catalogue_for`, the resolver branch and `native_ref`. Every existing caller passes `native=None` in this task; Task 9 decides when to pass `Profiles`.
- [ ] **Step 4: Run** — `uv run --frozen pytest tests/slicing tests/bambuddy tests/api/test_print_choices.py tests/api/test_print_run_choices.py -n auto -v` → PASS (the Bambuddy path unchanged); then the image test: `docker build --target test -t scadbuddy:test . && docker run --rm scadbuddy:test uv run --frozen pytest tests/slicing/test_catalogue.py -v`.
- [ ] **Step 5: Commit** — `git add backend && git commit -m "feat(slicing): the preset catalogue from Bambu Studio's own profiles (#3818)"`

---

### Task 3: Preset values from the profiles (A-values)

**Files:**
- Create: `backend/scadbuddy/slicing/values.py`
- Modify: `backend/scadbuddy/bambuddy/print_run.py` (`plan_run`'s `read = read_once(client)` `:1590`, and `check_print`'s `:848`), `backend/scadbuddy/bambuddy/filament_profiles.py` (`profile_overrides` `:258` and its read at `:177` take a `ReadPresetValues` instead of reaching for `client.preset_values`)
- Test: `backend/tests/slicing/test_values.py`

**Interfaces:**
- Consumes: `ReadPresetValues` (`bambuddy/client.py:1519`), `PresetValues` (`bambuddy/models.py:764`), `Profiles.process`, `native_ref` (Task 2).
- Produces:
  ```python
  def native_values(profiles: Profiles) -> ReadPresetValues
      # ref → native_ref → Profiles.process(name) in a thread → PresetValues(resolved=True, values=flat);
      # no native ref or a ProfileError → PresetValues(resolved=False, reason="not_in_profiles"),
      # never an exception: the callers already treat an unresolved read as "unknown" (#717, #1899)
  def values_for(client: BambuddyClient | None, *, native: Profiles | None) -> ReadPresetValues
      # native → native_values(native); else read_once(client)
  ```
  `support_overrides` (`supports.py:85`) and `prime_tower_warnings` (`prime_tower.py:89`) already take `read=`; their `client` parameter becomes `BambuddyClient | None`, used only when `read` is `None`.

- [ ] **Step 1: Write the failing tests** — `native_values` answers the flattened process for a standard ref and for a cloud ref; `resolved is False` with `reason == "not_in_profiles"` for a local ref; `profile_overrides(None, ref, profiles_request, read=native_values(p))` gives the same overrides the Bambuddy reader gives for the same values (feed the Bambuddy fake `read` the fixture's flattened dict and compare).
- [ ] **Step 2: Run to verify they fail** — `cd backend && uv run --frozen pytest tests/slicing/test_values.py -v`.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** — `uv run --frozen pytest tests/slicing tests/bambuddy/test_supports.py tests/bambuddy/test_prime_tower.py tests/bambuddy/test_filament_profiles.py tests/api/test_print_run_choices.py -n auto -v` → PASS (adjust the module names to what `git ls-files backend/tests | grep -E 'supports|prime_tower|filament_profiles'` lists).
- [ ] **Step 5: Commit** — `git commit -am "feat(slicing): process preset values from the profiles, for the dialog's override checks (#3818)"`

---

### Task 4: The laid-out per-print copy in the native library (A-copy)

Inventory finding 2, re-read: `PrintPipeline.file_to_print` (`bambuddy/print_source.py:413-467`) lays the copy out for the printer (`target_for`, `send.py:251`, which itself reads `client.printers()` for the plate model, `:281`) and uploads it with `ensure_uploaded` (`send.py:1160`) into Bambuddy's library, recording it in `output_bambuddy_uploads` (`migrations/20260928T0720Z_output_bambuddy_uploads.sql`). `print_plan` runs on the print worker, which has no content store (`worker.py:888` `build_print_deps`), so the native write goes through a hidden internal route, the way `RemoteLibrary` reads (`print_library/remote.py`, LIB Task 7a).

**Files:**
- Create: `backend/scadbuddy/print_library/copies.py`
- Modify: `backend/scadbuddy/print_library/reads.py` (a `LibraryWrites` protocol beside `LibraryReads` `:33`), `backend/scadbuddy/print_library/remote.py` (`RemoteLibrary.upload`), `backend/scadbuddy/api/internal_library.py` (`PUT {INTERNAL_LIBRARY}/files`, hidden, guarded as the existing internal routes are), `backend/scadbuddy/bambuddy/send.py` (`target_for` takes `model: str | None` when the caller knows it, so it reads no printer list; `_ensure_copy` `:876`/`_still_there` route by `is_native_id`), `backend/scadbuddy/bambuddy/print_source.py` (`PrintPipeline` gains `native: LibraryWrites | None = None`; `file_to_print` takes `native_copy: bool`), `backend/scadbuddy/bambuddy/print_source.py` `recorded_slice` `:362` (a native id is checked with the native reads, never `client.library_file`), `backend/scadbuddy/worker.py` (`build_print_deps` gives the `RemoteLibrary` as writes too; the in-process deps give the API's `NativeLibrary`)
- Test: `backend/tests/print_library/test_copies.py`, `backend/tests/api/test_print_copy_native.py` (`requires_postgres`)

**Interfaces:**
- Consumes: `NativeLibrary.upload(filename, data, folder_id, *, output_id=, filed=)` (`print_library/native.py:253`), `is_native_id` (`print_library/ids.py:11`), `BambuddyUploadStore` (`bambuddy/uploads.py:119`), `Printable` and `_laid_out_for` (`send.py`).
- Produces:
  ```python
  # print_library/reads.py
  class LibraryWrites(Protocol):
      async def upload(self, filename: str, data: BinaryIO, folder_id: int | None, *,
                       output_id: str | None = None, filed: bool = False) -> LibraryFile: ...
  # print_library/copies.py
  async def ensure_native_copy(native: LibraryReads & LibraryWrites, uploads: BambuddyUploadStore,
                               printable: Printable, *, target: Target, folder_id: int | None) -> int
      # as send._ensure_copy: a recorded copy with this (subject, folder, target.key) whose native file
      # still has its recorded sha256 is reused; else the laid-out bytes are uploaded (filed=False:
      # a per-print copy, not the filed output) and recorded in output_bambuddy_uploads, whose ids are
      # native (>= NATIVE_ID_BASE) and never collide with Bambuddy's (LIB Ruling 1). Never deletes:
      # a superseded native copy stays until the library sweep's rules remove it (none today; Task 14
      # files the follow-up for a per-print copy's lifetime).
  ```
  `file_to_print(..., native_copy=True)` calls `target_for(None, settings, model=<the registry row's model>, …)` and `ensure_native_copy`; `native_copy=False` is today's code. Task 9 sets it from D2; this task's callers pass `False`.

- [ ] **Step 1: Write the failing tests**
  - `test_a_native_copy_is_uploaded_and_reused`: two `ensure_native_copy` calls with one target → one `NativeLibrary.upload`, one `output_bambuddy_uploads` row with a native id.
  - `test_another_target_uploads_another_copy`.
  - `test_a_native_copy_is_never_looked_up_in_bambuddy` (Review Focus 2): a recorded native id and a recorded Bambuddy id for one subject; `ensure_native_copy` reads only the native one (a respx Bambuddy that fails on any request), and the Bambuddy path's `ensure_uploaded` never reads the native one.
  - `test_the_print_worker_writes_through_the_internal_route`: `RemoteLibrary.upload` against the app; the file lists under `/print/library` on `library_backend=native`.
  - `test_target_for_with_a_model_reads_no_printers`.
  - `test_the_native_slicer_reads_the_copy_without_a_copy` (AC5; after slicer Task 11 lands, else `xfail(strict=True, reason="slicer Task 11")`): a native slice of the copy's id puts no `slice` blob for the input (`native_slices` row's input key is `print_library.native.blob_key(id)`).
- [ ] **Step 2: Run to verify they fail** — `cd backend && uv run --frozen pytest tests/print_library/test_copies.py tests/api/test_print_copy_native.py -v`.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** — the Step 2 command, then `uv run --frozen pytest tests/bambuddy tests/api/test_print_*.py tests/api/test_send*.py tests/api/test_library_native.py -n auto` → PASS.
- [ ] **Step 5: Commit** — `git commit -am "feat(print): a native run's laid-out copy goes to ScadBuddy's own library (#3818)"`

---

### Task 5: A native destination (A-destination)

**Files:**
- Modify: `backend/scadbuddy/bambuddy/print_run.py` (`prepare_run` `:1250-1251`: on a native run `require_destination_folder` becomes `require_native_folder`), `backend/scadbuddy/workflows/print_activities.py` (`check` `:207`: `resolve_project` `:233` is skipped on a native run and `ResolvedProject(project_id=None)` recorded), `backend/scadbuddy/print_library/copies.py` (`native_destination`), `backend/scadbuddy/api/printing.py` (`get_projects` `:1224`, `get_default_project` `:1255`: a `native=true` query answers the native library's folders as `ProjectChoices` with no projects), `backend/scadbuddy/api/library_print.py` (the same for the library dialog)
- Test: `backend/tests/api/test_print_destination_native.py`

**Interfaces:**
- Consumes: `NativeLibrary.folders()` (`native.py:143`), `folder_name(title)` and `NativeLibraryStore.file_for_output` (filing, `print_library/filing.py:16-40`), `ProjectChoices` (`bambuddy/projects.py`).
- Produces:
  ```python
  async def require_native_folder(native: LibraryReads, folder_id: int) -> None    # 422 unless a live native folder
  async def native_destination(native: NativeLibrary, *, folder_id: int | None, title: str | None) -> int | None
      # folder_id when given (checked); else the output's <Template title>/ folder (made under the filing's
      # advisory lock when missing); else None (top level) for a library file
  NATIVE_PROJECT_REFUSAL = "A project is Bambuddy's: a print on the native path is filed in ScadBuddy's library. Pick a folder, or print through Bambuddy."
  ```
  A native run's request with `project_id` set is refused at `print_check` with `NATIVE_PROJECT_REFUSAL` (422), before anything is recorded (D6).

- [ ] **Step 1: Write the failing tests** — a native run with no folder files its copy under `<Template title>/` (the output's title); with a native `folder_id`, there; with a Bambuddy-range `folder_id`, 422; with `project_id`, 422 with the refusal; `GET /print/projects?native=true` lists native folders and no projects, with a Bambuddy that fails on any request.
- [ ] **Step 2: Run to verify they fail.** `cd backend && uv run --frozen pytest tests/api/test_print_destination_native.py -v`
- [ ] **Step 3: Implement** (the native flag reaches `check` and `prepare_run` from Task 9's predicate; until then tests set it through the function's parameter).
- [ ] **Step 4: Run** the Step 2 command and `uv run --frozen pytest tests/api/test_print_projects*.py tests/api/test_print_run*.py -n auto` → PASS.
- [ ] **Step 5: Commit** — `git commit -am "feat(print): a native print is filed in a native library folder, never a Bambuddy project (#3818)"`

---

### Task 6a: Physical spools, provider records and the provider interface (A-trays, D1)

**Files:**
- Create: a NEW migration `backend/scadbuddy/migrations/$(date -u +%Y%m%dT%H%MZ)_physical_spools.sql`
- Create: `backend/scadbuddy/spools/__init__.py`, `spools/models.py`, `spools/store.py`, `spools/providers.py`, `spools/component.py`
- Test: `backend/tests/spools/test_store.py` (`requires_postgres`), `backend/tests/spools/test_providers.py`

**Interfaces:**
- Consumes: `feature_exports` (`core/components.py:169`), the `Component` pattern (`core/components.py`, `CLAUDE.md` "Layout"), the `printers` table (3a Task 2, migration `20261018T1659Z_printers.sql` on `feat/printer-registry`).
- Produces:
  ```sql
  CREATE TABLE physical_spools (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tray_uuid text UNIQUE,              -- the AMS's spool UUID; NULL when untagged (all zeros is never stored)
    tag_uid text,                       -- the RFID tag's UID last read; NULL when untagged. Not UNIQUE:
                                        -- a Bambu spool has a tag on each side, two tag_uids, one tray_uuid
    material text, colour text, tray_info_idx text,   -- as last seen in a tray or set when linked
    created_at timestamptz NOT NULL DEFAULT now(),
    last_seen_at timestamptz
  );
  CREATE TABLE spool_provider_records (
    provider text NOT NULL,             -- a SpoolProvider.name: 'bambuddy', later 'native', ...
    record_id text NOT NULL,            -- the provider's own id, as text
    physical_spool_id uuid REFERENCES physical_spools ON DELETE SET NULL,  -- NULL: not linked yet (offered for linking)
    linked_by text CHECK (linked_by IN ('rfid', 'manual')),
    snapshot jsonb NOT NULL,            -- SpoolSnapshot
    refreshed_at timestamptz NOT NULL,
    PRIMARY KEY (provider, record_id),
    UNIQUE (physical_spool_id, provider)  -- one record per provider per physical spool; 1:N across providers
  );
  CREATE TABLE spool_provider_refreshes (  -- one row per provider: the overlay's staleness note
    provider text PRIMARY KEY, refreshed_at timestamptz, failed_at timestamptz, failure text
  );
  CREATE TABLE tray_spool_links (        -- manual links for untagged trays only; RFID trays resolve by join
    printer_id uuid NOT NULL REFERENCES printers ON DELETE CASCADE,
    ams_id integer NOT NULL, tray_id integer NOT NULL,
    physical_spool_id uuid NOT NULL REFERENCES physical_spools ON DELETE CASCADE,
    fingerprint text NOT NULL,           -- trays.py's material|colour|state when linked; a changed tray drops the link
    linked_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (printer_id, ams_id, tray_id)
  );
  ```
  ```python
  # spools/models.py
  class SpoolSnapshot(BaseModel):          # what a provider says of one record; never a provider's raw payload
      record_id: str; material: str | None = None; colour: str | None = None   # #RRGGBB
      brand: str | None = None; name: str | None = None
      remaining_g: float | None = None; label_weight_g: int | None = None
      tray_uuid: str | None = None; tag_uid: str | None = None                 # when the provider knows them
      preset: str | None = None                # the provider's filament preset id, when it has one
  class TraySpool(BaseModel):              # one tray as the dialog's overlay sees it
      ams_id: int; tray_id: int; physical_spool_id: UUID | None
      linked_by: Literal["rfid", "manual"] | None
      records: dict[str, SpoolSnapshot]    # provider name -> its record
  # spools/providers.py
  class SpoolProvider(Protocol):
      name: str                            # stable key; the column value
      label: str                           # "Bambuddy", for the note
      async def configured(self) -> bool   # false: the refresh skips it and its rows are left as they are
      async def records(self) -> list[SpoolSnapshot]   # the whole inventory, read only by the refresh
  SpoolProviderBuild = Callable[[Core, Components], SpoolProvider | None]
  PROVIDER_MODULE, PROVIDER_ATTR = "spool_providers", "SPOOL_PROVIDERS"
  def discover_providers(core: Core, components: Components) -> list[SpoolProvider]
      # every SPOOL_PROVIDERS list's builds, by feature name; a duplicate name raises at start
  # spools/store.py
  class SpoolStore:
      async def replace_records(self, provider: str, snapshots: list[SpoolSnapshot], at: datetime) -> None
          # one transaction: upsert each record; link by tray_uuid, else (no tray_uuid) tag_uid, to physical_spools (creating
          # the physical spool for a provider record carrying a tag we have never seen), linked_by='rfid';
          # keep an existing 'manual' link; delete the provider's rows not in snapshots; set the refresh row
      async def failed(self, provider: str, at: datetime, failure: str) -> None   # the rows stay; the note says so
      async def seen(self, trays: list[SeenTray]) -> None
          # the refresh's other half: an RFID tray in a printer's latest report creates/updates its physical spool
      async def overlay(self, printer_id: UUID, trays: list[SeenTray], *, now: datetime) -> Overlay
          # read-only: TraySpool per tray (join by tray_uuid/tag_uid, else a manual link whose fingerprint matches),
          # plus `stale: dict[provider, datetime | None]` for every provider whose refresh is older than
          # SPOOL_OVERLAY_STALE or missing (those providers' records are left out)
      async def link(self, printer_id: UUID, tray: SeenTray, *, physical_spool_id: UUID | None = None,
                     provider: str | None = None, record_id: str | None = None) -> TraySpool
      async def unlink(self, printer_id: UUID, ams_id: int, tray_id: int) -> None
  class SeenTray(BaseModel): ams_id: int; tray_id: int; tray_uuid: str | None; tag_uid: str | None
                             material: str | None; colour: str | None; tray_info_idx: str | None; fingerprint: str
  SPOOL_OVERLAY_STALE = timedelta(minutes=30)
  ```

- [ ] **Step 1: Write the failing tests** — `replace_records` links an RFID record to the physical spool its `tray_uuid` names, keeps a manual link across a refresh, and drops a record the provider no longer lists; two providers' records of one tray's spool both appear in `overlay(...).records` (1:N); an all-zero `tray_uuid` or `tag_uid` is stored as `NULL`; two records of one spool, one per side's tag (different `tag_uid`s, one `tray_uuid`), link to one physical spool (measure it first: read both sides of one spool in the AMS and record the two reports' `tag_uid`/`tray_uuid` in the test's docstring, #4182); a manual link is dropped when the tray's fingerprint changes; `overlay` leaves out a provider refreshed 31 minutes ago and names it in `stale`; `discover_providers` refuses two providers named alike (a test package with two `spool_providers.py`, as `tests/test_components.py` builds features).
- [ ] **Step 2: Run them to verify they fail** — `cd backend && uv run --frozen pytest tests/spools -v`.
- [ ] **Step 3: Implement** (the migration a NEW file named by `date -u` when created).
- [ ] **Step 4: Run** Step 2's command and `uv run --frozen mypy` → PASS.
- [ ] **Step 5: Commit** — `git add backend && git commit -m "feat(spools): physical spools, provider records and the provider interface (#3818)"`

---

### Task 6b: The Bambuddy provider and the refresh Schedule, off the hot path

**Files:**
- Create: `backend/scadbuddy/bambuddy/spool_providers.py` (exports `SPOOL_PROVIDERS`)
- Create: `backend/scadbuddy/workflows/spool_refresh.py` (`SpoolRefresh`, `ensure_spool_schedule`), `backend/scadbuddy/spools/activities.py` (`spool_providers_refresh`)
- Modify: `backend/scadbuddy/bambuddy/models.py` (`Spool` `:844` gains `tag_uid: str | None = None`, `tray_uuid: str | None = None`, which Bambuddy's `GET /inventory/spools` carries: `tests/bambuddy/recordings/inventory-spools.json`), `backend/scadbuddy/core/settings.py` (`spool_overlay_refresh_seconds: int = 300`, `APPLIES` `live`; 0 deletes the Schedule), `backend/scadbuddy/library/settings_store.py` and `api/settings.py` (the field), `backend/scadbuddy/main.py` `_run_library_worker` (the workflow and activity registered on `library`, as `PreviewBackfill` is, `workflows/previews.py`)
- Test: `backend/tests/spools/test_bambuddy_provider.py`, `backend/tests/test_spool_refresh_workflow.py` (`requires_temporal`), `backend/tests/test_spool_refresh_replay.py`, `backend/tests/fixtures/spool_refresh_histories/` (new)

**Interfaces:**
- Consumes: `BambuddyClient.spools()` (`client.py:521`), `SpoolStore.replace_records/failed/seen` (Task 6a), `PrinterReportStore.get` (3a Task 6), the Schedule pattern of `ensure_preview_schedule` (`workflows/previews.py`, `CLAUDE.md` "Layout": "on its own hourly Schedule on the same queue").
- Produces: `BambuddyProvider(name="bambuddy", label="Bambuddy")`, `configured()` true iff `bambuddy_url` is set; `records()` maps each unarchived `Spool` to a `SpoolSnapshot` (`colour` from `rgba` normalised as `filaments.normalise_colour` does, `remaining_g` from `Spool.remaining_g`, `preset=slicer_filament`). Workflow `SpoolRefresh` (id `spool-providers`, a Schedule every `spool_overlay_refresh_seconds`, overlap SKIP) runs one activity: for each discovered provider that is `configured()`, `records()` under a 60 s timeout, then `replace_records`, or `failed` on any error (logged by type only); then `seen` over every printer's latest report. The dialog never starts, signals or waits on it.

- [ ] **Step 1: Write the failing tests** — the provider maps the recorded inventory (12 rows; the RFID ones carry `tray_uuid`); the activity with a Bambuddy that answers 503 records `failed` and leaves the previous rows; with `bambuddy_url` unset the provider is skipped and nothing is read; the Schedule exists after `ensure_spool_schedule` with 300 s and is deleted at 0; a replay test over the recorded histories (`["refreshed", "failed"]`), with a `SCADBUDDY_RECORD_HISTORIES=1` recorder, as `tests/test_housekeeping_replay.py` does.
- [ ] **Step 2: Run them to verify they fail** — `cd backend && uv run --frozen pytest tests/spools/test_bambuddy_provider.py tests/test_spool_refresh_workflow.py tests/test_spool_refresh_replay.py -v`.
- [ ] **Step 3: Implement, then record the histories** — `SCADBUDDY_RECORD_HISTORIES=1 uv run --frozen pytest tests/test_spool_refresh_replay.py -v`.
- [ ] **Step 4: Run** Step 2's command, `tests/api/test_settings_runtime.py` (the new live setting) and `uv run --frozen mypy` → PASS.
- [ ] **Step 5: Commit** — `git commit -am "feat(spools): Bambuddy's inventory as a spool provider, refreshed on its own Schedule (#3818)"`

---

### Task 6c: Tray-first filament options with the overlay

**Files:**
- Modify: `backend/scadbuddy/bambuddy/trays.py` (`loaded_trays(status, printer_id, printer_name) -> list[SpoolOption]`: every filled tray, via `_trays` `:104` and `_filled` `:96`, and `seen_trays(status) -> list[SeenTray]`), `backend/scadbuddy/bambuddy/filaments.py` (`SpoolOption` `:131` gains `remain_percent: int | None = None`, `physical_spool_id: UUID | None = None` and `records: dict[str, SpoolSnapshot] = {}`, with `tray_info_idx` carried in `slicer_filament`; `FilamentOptions` `:220` gains `source: Literal["inventory", "trays"] = "inventory"` and `overlay_notes: list[str] = []`; `gather_plate_options` `:915` gains `trays_from: PrinterStatus | None = None` and `overlay: Overlay | None = None`, which replace the inventory read and `inventory_remain` when given)
- Modify: `backend/scadbuddy/bambuddy/resolver.py` `resolve` `:238` (a tray's preset by `tray_info_idx` through Task 2's `filament_id` lookup, else its overlay record's `preset` through `native_ref`), `backend/scadbuddy/bambuddy/print_run.py` `resolve_plates` `:678` (no `spool_filament_presets` read on a native run; a positive spool id there is a slot with no choice)
- Create: `backend/scadbuddy/api/tray_spools.py` (`PUT /api/v1/printers/{printer_id}/trays/{ams_id}/{tray_id}/spool` body `{physical_spool_id} | {provider, record_id}`, `DELETE` the same path), and its agent tools in `agent/src/tools/printers.ts` (`link_tray_spool`, `unlink_tray_spool`, writes; or `NOT_A_TOOL` entries with a reason, `agent/src/tools/coverage.ts`)
- Test: `backend/tests/bambuddy/test_trays_first.py`, `backend/tests/api/test_tray_spools.py`, `agent/test/printers.test.ts`

**Interfaces:**
- Consumes: `SpoolStore.overlay/link/unlink` (Task 6a), `AmsTray` (`models.py:389`), `tray_spool_id`, `tray_option`, `extruder_of`, `side_of`, `build_options`, `suggest` (`filaments.py:393,508`).
- Produces: `gather_plate_options(None, …, trays_from=status, overlay=overlay)` makes no client call. Each tray option: `tray_only=True`, `loaded` its AMS slot, `colour`, `material`, `slicer_filament=tray.tray_info_idx`, `remain_percent=tray.remain if tray.remain >= 0 else None`, `physical_spool_id` and `records` from the overlay, `remaining_g` the first fresh provider record's (in discovery order) or `None`; `source="trays"`; `overlay_notes` one sentence per stale or missing provider ("Bambuddy's spool records are from 14:02; showing the trays only." / "No spool records from Bambuddy yet."). `suggest` runs over them as over spools.

- [ ] **Step 1: Write the failing tests**
  - `test_every_filled_tray_is_a_choice`: AMS-A (4 trays, one empty), an AMS-HT (`id` 128) and an external holder → 5 choices labelled "AMS-A slot 1" … "HT-A", "External".
  - `test_trays_first_reads_nothing_from_bambuddy`: under Task 1's `forbid_bambuddy`.
  - `test_an_rfid_tray_shows_its_bambuddy_record`: a fresh Bambuddy record with the tray's `tray_uuid` → `records["bambuddy"].brand` and `remaining_g` on the option.
  - `test_a_stale_overlay_degrades_with_a_note`: the record refreshed 31 min ago → no `records`, one note naming Bambuddy and the time.
  - `test_a_manual_link_overlays_an_untagged_tray`, and is gone when the tray's colour changes.
  - `test_a_remembered_spool_is_dropped_on_a_native_run` (Review Focus 4).
  - `test_the_inventory_path_is_unchanged`: without `trays_from`, today's reads in today's order.
  - `test_tray_spool_links_routes` and the agent coverage test.
- [ ] **Step 2: Run them to verify they fail** — `cd backend && uv run --frozen pytest tests/bambuddy/test_trays_first.py tests/api/test_tray_spools.py -v`.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** — `uv run --frozen pytest tests/bambuddy tests/spools tests/api/test_print_filaments.py tests/api/test_print_run_choices.py -n auto` and `cd agent && pnpm test -- printers coverage` → PASS.
- [ ] **Step 5: Commit** — `git add backend agent && git commit -m "feat(print): a native run's filaments are the loaded trays, with the spool providers' records over them (#3818)"`

---

### Task 7: `ams_mapping` and `ams_mapping2` from the plan's trays (A-ams)

The 3a plan's Task 9 said `lan_print`'s `ams_mapping` comes "as `queue_plate`'s rack pick does today (`print_run.py:491-520`)". No such code exists: ScadBuddy has never computed an `ams_mapping` (Bambuddy's scheduler does for a queue item without one, `bambuddy/filaments.py:1-8`, `:253`, `:786`; `print_run.py:1572`), and those lines are `resolve_project`/`read_status`. The 3a plan now points here.

What the wire carries, from two sources that agree with each other and disagree with a third:
- Bambu Studio v02.08.02.61 sends both `ams_mapping` and `ams_mapping2` (`src/slic3r/GUI/Jobs/PrintJob.cpp:262-265`); `ams_mapping2` is a list of `{ams_id, slot_id}`, `{255, 255}` for an unmapped filament and `VIRTUAL_TRAY_MAIN_ID` for the external spool (`src/slic3r/GUI/SelectMachine.cpp:1466-1525`).
- Bambuddy, which prints over LAN MQTT itself, sends the flat `ams_mapping` as one global tray id per filament slot (`ams_id * 4 + slot_id`, the AMS-HT's own id at 128+, `-1` unmapped *and* `-1` for an external spool, since "firmware doesn't accept raw 254/255" there), plus `ams_mapping2` with the external spool as `{ams_id: 255, slot_id: 0}` (`254`/`255` by side on a dual-nozzle printer) (`backend/app/services/bambu_mqtt.py:5987-6033`, `:6172-6174` at maziggy/bambuddy `0d427510`, with the H2D failure `0700_8012` it records for raw 254/255).
- OpenBambuAPI `mqtt.md` (`cc383a2c`, "AMS Mapping Configuration") describes a fixed five-element array filled from the right with AMS slot numbers. It matches neither of the above and is not followed; Step 1's live check settles it.

**Files:**
- Create: `backend/scadbuddy/printers/ams_mapping.py`, `backend/tests/printers/fixtures/ams-mapping-vectors.json`
- Test: `backend/tests/printers/test_ams_mapping.py`

**Interfaces:**
- Consumes: `tray_of_spool_id` (`trays.py:60`), `EXTERNAL` (`trays.py:37`), `SlotMaterial.global_tray_id`'s encoding (`bambuddy/models.py:946-959`).
- Produces:
  ```python
  class AmsSlot(BaseModel): ams_id: int; slot_id: int
  class AmsMapping(BaseModel):
      ams_mapping: list[int]            # per filament slot: global tray id, -1 unmapped or external
      ams_mapping2: list[AmsSlot]       # per filament slot: {ams_id, slot_id}; {255,255} unmapped; external {255|254, 0}
      use_ams: bool                     # False only when every mapped slot is external
  def ams_mapping(plan: FilamentPlan, filament_count: int, *, dual_nozzle: bool) -> AmsMapping
      # filament_count from the sliced plate (slicer Task 9's read_sliced); a positive (inventory) spool id
      # raises ValueError: a native plan is trays only (D1)
  ```
  3a Task 9's `ProjectFile` gains `ams_mapping2: list[AmsSlot]`, sent beside `ams_mapping`.

- [ ] **Step 1: Confirm the wire on the printer** (with the user, during Task 8's session): one two-filament plate sent from Bambu Studio on the LAN, its `project_file` captured as Task 8 Step 1 captures it; record `ams_mapping`, `ams_mapping2` and `use_ams` in the module docstring with the firmware version and date. If they differ from the encoding above, stop and report.
- [ ] **Step 2: Write the failing tests** — vectors (Review Focus 3): AMS-A slot 1 → `0` / `{0,0}`; AMS-B slot 3 → `6` / `{1,2}`; AMS-HT 128 → `128` / `{128,0}`; external on a single-nozzle printer → `-1` / `{255,0}`; external left on a dual-nozzle → `-1` / `{254,0}`; slots 1 and 3 used, 2 unchosen → `[x, -1, y]` with `{255,255}` in the middle; only external → `use_ams` false; a positive id → `ValueError`.
- [ ] **Step 3: Run them to verify they fail** — `cd backend && uv run --frozen pytest tests/printers/test_ams_mapping.py -v`.
- [ ] **Step 4: Implement.**
- [ ] **Step 5: Run** Step 3's command → PASS.
- [ ] **Step 6: Commit** — `git add backend/scadbuddy/printers/ams_mapping.py backend/tests/printers && git commit -m "feat(printers): ams_mapping and ams_mapping2 from a native plan's trays (#3818)"`

---

> **Measured 2026-10-24, for Task 8 below** (H2C, firmware 01.02.00.00):
> - `nozzle_mapping` is 32 entries, one per filament slot (index 0 = slot 1), each a **physical** nozzle id: `1` the fixed hotend, `16`-`21` rack positions, `-1` a slot the plate does not print. Extruder indices are another namespace: extruder 0 is the rack carriage, extruder 1 the fixed hotend. A plate that needs no rack slot sends no `nozzle_mapping` at all. A wrong id levels with one nozzle and prints with another (a first layer in mid-air), so the failure mode is to omit the field and let the firmware pick (Bambuddy `bambu_mqtt.py` `resolve_rack_nozzle_mapping` and its comments, deployed image, read 2026-10-24).
> - Completed prints Bambuddy sent this printer, from its `print_queue` (`ams_mapping` → `nozzle_mapping`): item 370 `[4, 1, 6, 12, 5, 13]` → `[21, 18, 20, 17, 1, 17, -1 x26]`; 367 `[3, 15, 0, 11, 8, 5, 6]` → `[18, 1, 1, 20, 1, 18, 20, -1 x25]`; 374 `[128]` (the AMS-HT) → `[18, -1 x31]`. The positions follow which nozzle type and diameter each rack slot holds (`device.nozzle.info`), so they are computed per print from the report, never stored.
> - This H2C has a Filament Track Switch: every AMS unit's `info` has extruder bits `0xE` (not fixed), its inlet in bits 24-27 (A for units 0, 3 and 128, B for 1 and 2), so any tray reaches either carriage (`printers/status.py` maps it since this measurement).

### Task 8: The H2C rack over LAN: measure the command, then send it (A-rack, D7)

**Files:**
- Create: `backend/tests/printers/sniff.py` (a capture script over 3a Task 4's `LanPrinter`), `backend/tests/printers/test_rack_live.py` (marker `requires_printer`, 3a Task 4's), `backend/tests/printers/fixtures/rack-project-file.json` and `rack-auto-nozzle-mapping.json` (captures, serial redacted)
- Create: `backend/scadbuddy/printers/nozzle_mapping.py`
- Modify: `backend/scadbuddy/printers/lan.py` (3a Task 4's `ProjectFile` gains `nozzle_mapping: list[int] | None = None`, omitted from the command when `None`), `backend/scadbuddy/printers/print_activities.py` (3a Task 9's `lan_print` input gains `nozzle_mapping`), `backend/scadbuddy/bambuddy/print_run.py` (the rack pick on a native run reads the status from the report, never `client.printer_status`: `rack_chooser` `:578-670`, its status read `:615`), `docs/superpowers/specs/2026-10-16-own-store-slicer-rust-workers-design.md` §2 (a row per measured fact)
- Test: `backend/tests/printers/test_nozzle_mapping.py`, `backend/tests/test_print_native_workflow.py` (3a Task 9's, a rack case)

**Interfaces:**
- Consumes: `rack_chooser` and its `RackChoice.nozzle_rack_choice` (`{str(group_id): position}`, `print_run.py:578-670`), `rack_groups`, `rank_rack`, `rack_positions` (the rack module), the plate's filament groups and their extruders from the sliced 3MF (`slice_info.config`, slicer Task 9's `read_sliced`, extended here to read `<filament group_id>` and `<nozzle extruder_id>` if it does not yet), `LanPrinter.publish/reports` (3a Task 4).
- Produces:
  ```python
  # printers/nozzle_mapping.py
  RACK_WIRE_SLOTS: int                   # 32 on the H2C until Step 1 says otherwise (Bambuddy bambu_mqtt.py:363-372)
  FIXED_NOZZLE_ID: int                   # 1 (Bambuddy bambu_mqtt.py:405)
  def rack_nozzle_id(position: int) -> int | None   # 15 + position for positions 1..6 (Bambuddy :489-499)
  def nozzle_mapping(slot_groups: list[int], fixed_groups: set[int],
                     picks: Mapping[int, int]) -> list[int] | None
      # one physical nozzle id per filament slot: FIXED_NOZZLE_ID for a group on the fixed hotend, the picked
      # rack position's id for a rack group, -1 for a slot the plate does not print; padded to RACK_WIRE_SLOTS;
      # None when no group prints from the rack (Bambu Studio omits the field then, Bambuddy :431-433)
  ```
  On a native run `queue_plate`'s rack pick runs before `lan_upload` with the report's status, and its `picks` become `nozzle_mapping`; the same `save_picks` record keeps the rack usage (#836) as on the Bambuddy path.

- [ ] **Step 1: Measure, with the user at the H2C** (bed clear; every print cancelled once it starts):
  1. Run `cd backend && uv run --frozen python -m tests.printers.sniff --out <a temp dir outside the repo>` with `SCADBUDDY_TEST_PRINTER_HOST`, `_SERIAL`, `_ACCESS_CODE` set: it subscribes to `device/<serial>/report` and `device/<serial>/request` and writes every message.
  2. From Bambu Studio 02.08 on the LAN, send a three-filament plate whose groups use the fixed hotend and two rack positions. Record what a second client sees of Bambu Studio's `get_auto_nozzle_mapping` request, the printer's reply (`mapping`), and the `project_file` with `nozzle_mapping`, `ams_mapping`, `ams_mapping2`. The firmware echoes a command's fields back in its acknowledgement on the report topic (Bambuddy `bambu_mqtt.py:89-100` `is_printer_status_frame`, #3040), so the `project_file` fields are expected there even if the request topic is not relayed. Compare with Bambuddy's recorded capture (`[16, 1, 18, -1 x29]`, `bambu_mqtt.py:363-372`).
  3. Send the same plate from `sniff.py --replay <capture>` with the rack positions swapped by hand, and confirm on the printer's screen that the swapped hotends are the ones it picks.
  4. Record each fact (field names, lengths, ids, firmware version, date, how measured) in the spec §2 rows, `nozzle_mapping.py`'s docstring and the fixtures.
- [ ] **Step 2: Write the failing tests** — `nozzle_mapping`: groups `[2, 0, 1]` with group 0 fixed and picks `{1: 1, 2: 3}` → `[18, 1, 16, -1 × 29]`; no rack group → `None`; a pick outside 1..6 → `None` with the run's warning; the native run's rack case sends the mapping in `project_file` (the fake LAN's recorded command) and saves the picks; the report's status, not Bambuddy's, feeds the pick (Task 1's harness).
- [ ] **Step 3: Run them to verify they fail** — `cd backend && uv run --frozen pytest tests/printers/test_nozzle_mapping.py tests/test_print_native_workflow.py -k rack -v`.
- [ ] **Step 4: Implement** with the measured constants.
- [ ] **Step 5: Run** Step 3's command, `uv run --frozen pytest tests/printers tests/api/test_print_run*.py -n auto`, and the live test with the user once: `uv run --frozen pytest tests/printers/test_rack_live.py -v` → the printer picks the hotends sent.
- [ ] **Step 6: Commit** — `git commit -am "feat(printers): a native rack print sends its nozzle_mapping, as measured on an H2C (#3818)"`

**Decide then (only if Step 1 finds the printer cannot be told the mapping over LAN):** stop before Step 2 and take the capture to the user with three options: refuse a rack print on the native path with its reason; send no `nozzle_mapping` and let the firmware pick, as Bambuddy does when it cannot resolve one (`resolve_rack_nozzle_mapping`'s docstring, `bambu_mqtt.py:408-440`); or run rack prints on the Bambuddy path. Nothing is built for any of them before that answer.

---

### Task 9: The native mode, the optional client and AC1 (A-optional-client)

**Files:**
- Create: `backend/scadbuddy/printers/hot_path.py`
- Modify: `backend/scadbuddy/bambuddy/client.py` (`maybe_client` beside `client_for` `:1511`), `backend/scadbuddy/bambuddy/print_run.py` (`prepare_run` `:1228`, `plan_run` `:1552`, `check_print` `:758`, `_hardware_warnings` `:2042`: `client: BambuddyClient | None`; on `None` the last-bed-type read `:2058` is skipped, as an unreadable archive list already is), `backend/scadbuddy/bambuddy/choices.py` (`choices_for` `:133`: the printers from the registry and the status from the report on a native run, D4), `backend/scadbuddy/bambuddy/printers_now.py` (3a Task 6's version: LAN report first, D4), `backend/scadbuddy/bambuddy/preview.py` (`start_preview` `:89`: client optional), `backend/scadbuddy/workflows/print_activities.py` (`check` `:207`, `plan` `:268`, `record` `:376`, `_keep_slice_hash` `:396`, `finish` `:437`: `maybe_client`), `backend/scadbuddy/api/deps.py` (`read_progress`/`read_library` `:390-406`) and `backend/scadbuddy/worker.py` (`:915-922`): the readers built outside `client_for`, with `maybe_client`, so a `route="lan"` follow (3a Task 9) never opens one; `backend/scadbuddy/bambuddy/progress.py` (`progress_for` `:377`, `library_progress` `:443`: `client: BambuddyClient | None`)
- Modify, handed over by Tasks 4 and 5 (#4312 review M6, M7):
  - `PrintDeps.native_run` (`workflows/print_activities.py`), always False until this task: set it from `hot_path_native`, in the API's in-process `PrintDeps` (`main.py` `_run_print_worker`) and the print worker's (`worker.py` `build_print_deps`).
  - `prepare_run(native=)`: pass the native library from `check_print` (`print_run.py`, the dialog's check) and from `bambuddy/preview.py` (`start_preview`), not only from `print_check`, or a native folder id reaches `require_destination_folder` there.
  - `plan_run`: on a native run, call `native_destination(native, folder_id=request.folder_id, title=<the output's template title>)` (`print_library/copies.py`; `None` for a library file) and pass the result to `file_to_print(..., native_copy=True, printer_model=<the registry row's model>)`. Never call `planned_project`: it falls back to `resolve_project`, a Bambuddy read, whenever `prepared.project` is `None`.
  - The misuse guards (`print_source.py` `_misuse`, `send.target_for`) are 500 `ApiError`s, which the plan activity raises as non-retryable. One the predicate can reach, such as a native run's Bambuddy-range source, is refused at the check instead, as a 422.
  - `reads.py`'s `served_reads` and `reads_of` still raise `TypeError` when there is neither a client nor a native library. Once the client is optional, make them `_misuse` too.
- Test: `backend/tests/api/test_hot_path_native.py` (`requires_postgres`, `requires_temporal`)

**Interfaces:**
- Consumes: Tasks 1–7; `PrinterRow`, `PrinterStore`, `PrinterReportStore`, `status_from_report`, `PlannedRun.path` (3a Tasks 2, 5, 6, 8); the fake LAN (3a Task 9's `fake_lan`); the fake slicer (slicer Task 11's `tests/support/fake_slicer.py`); `Slicing.profiles` (slicer Task 11).
- Produces:
  ```python
  # printers/hot_path.py
  def hot_path_native(settings: StoredSettings, path: Literal["bambuddy", "native"]) -> bool     # D2
  # bambuddy/client.py
  @asynccontextmanager
  async def maybe_client(settings: StoredSettings, *, native: bool) -> AsyncIterator[BambuddyClient | None]  # D3
  ```
  On a native run: `catalogue_for(None, native=profiles)`, `values_for(None, native=profiles)`, `file_to_print(..., native_copy=True)` into `native_destination`, `gather_plate_options(None, trays_from=status, overlay=await spools.overlay(...))` (the overlay a Postgres read, Task 6a), no `resolve_project`, no `read_inventory`, `plan_for(None, …)` given the trays as its inventory (nozzle_plan.py `:317`: `inventory=` already skips the read), the rack pick from the report's status, and 3a Task 9's `lan_print` given `ams_mapping(plan, filament_count, dual_nozzle=…)` (Task 7) and `nozzle_mapping` (Task 8). `PlannedRun` carries the decision as 3a Task 8's `path`; nothing new crosses the history.

- [ ] **Step 1: Write the failing acceptance test** (AC1, AC4, AC5, Review Focus 1, 5)

```python
async def test_a_native_run_makes_no_bambuddy_call(native_app, spans, fake_lan, fake_slicer):
    # native_app: store s3 (moto), library native, slicer native, the fixture profiles, a KEK,
    # bambuddy_url="http://bambuddy.test" set; one registry printer, LAN-linked, preferred_path native,
    # a fresh report (fixture lan-report-h2c.json with two filled AMS trays, one RFID-tagged);
    # spool_overlay_refresh_seconds=0, and the overlay refreshed once here, before the window opens,
    # from a respx Bambuddy serving inventory-spools.json (the refresh is outside AC1)
    await refresh_spool_providers(native_app)
    with forbid_bambuddy(spans, "http://bambuddy.test"):
        output = render_and_save(native_app, "name-keychain")               # POST render, wait, save
        choices = native_app.get(f"/api/v1/print/outputs/{output}/choices", params={"printer": ROW}).json()
        filaments = native_app.get(f"/api/v1/print/outputs/{output}/filaments", params={"printer": ROW}).json()
        assert filaments["source"] == "trays" and filaments["spools"][0]["records"]["bambuddy"]
        started = native_app.post(f"/api/v1/print/outputs/{output}/preview-slice", json=run_body(filaments)).json()
        wait_preview(native_app, output, started["job_id"], slicer="native")  # completed
        run = accept_and_wait(native_app, output, run_body(filaments))       # POST .../run, 202, then done
        follow_until(native_app, output, "completed", fake_lan)               # fake_lan advances gcode_state
    sent = fake_lan.published[-1]["print"]
    assert sent["command"] == "project_file" and sent["ams_mapping"] == [0, 1]
    assert sent["ams_mapping2"] == [{"ams_id": 0, "slot_id": 0}, {"ams_id": 0, "slot_id": 1}]

async def test_a_stale_overlay_still_prints(native_app, spans, fake_lan, fake_slicer):     # AC4b
    # the overlay refreshed 31 minutes ago: the filaments answer trays only with a note, and the run prints
    assert is_native_id(run["library_file_id"])

async def test_a_stale_report_runs_the_bambuddy_path(native_app, ...):   # preferred_path auto, report 2 min old
    # the run's PlannedRun.path == "bambuddy"; Bambuddy's routes are called (respx), no forbid_bambuddy
```

- [ ] **Step 2: Run it to verify it fails** — `cd backend && uv run --frozen pytest tests/api/test_hot_path_native.py -v` → `BambuddyCalledError` listing every remaining span (keep that list in the task's notes: it is the work list for Step 3).
- [ ] **Step 3: Implement** until the list is empty, one call site at a time; `uv run --frozen mypy` after making each signature optional finds the callers.
- [ ] **Step 4: Run the whole print suite and the replay** — `uv run --frozen pytest tests/api/test_hot_path_native.py tests/test_print_replay.py tests/bambuddy tests/printers $(ls tests/api/test_print*.py | tr '\n' ' ') -n auto` → PASS; every recorded history replays.
- [ ] **Step 5: Commit** — `git commit -am "feat(print): a native run never builds a Bambuddy client, render to follow (#3818)"`

---

### Task 10: With `bambuddy_url` unset (AC2)

**Files:**
- Modify: the print dialog routes that open `client_for` before knowing the path (`api/printing.py:1009, 1060, 1086, 1128, 1160, 1208, 1236, 1267`; `api/library_print.py:1072, 1136, 1265, 1299, 1375, 1396, 1423`, the lines that open it): decide the path first (the request's `printer`, else the configured default's row), then `maybe_client`; a route that is Bambuddy-only by nature (projects without `native=true`, the camera, Bambuddy's inventory routes, the Bambuddy TrayQuestion assignment) keeps its 409 "no Bambuddy URL is configured" (`client.py:238`)
- Test: `backend/tests/api/test_hot_path_native.py` (a second mode), `backend/tests/api/test_settings_runtime.py` (the app starts with `bambuddy_url` unset on s3/native/native)

**Interfaces:** none new; `native_app` gains a `bambuddy_url=None` parametrisation.

- [ ] **Step 1: Write the failing tests** — `test_a_native_run_without_bambuddy` (Task 9's test with `bambuddy_url=None`, no respx at all: any client construction raises `not_configured` and fails the run); `test_the_dialog_reads_answer_without_bambuddy` (choices, filaments, check, preview, progress, `/print/printers/now`, `/print/projects?native=true` each 200); `test_bambuddy_only_routes_still_say_why` (camera, inventory → 409 naming Settings).
- [ ] **Step 2: Run to verify they fail** — `cd backend && uv run --frozen pytest tests/api/test_hot_path_native.py -k without -v`.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** — the full backend suite, `uv run --frozen pytest -n auto`, → PASS.
- [ ] **Step 5: Commit** — `git commit -am "feat(print): the native hot path works with no Bambuddy configured (#3818)"`

---

### Task 11: The dialog without Bambuddy, and the mocked e2e walk (layer 1)

**Files:**
- Modify: `frontend/src/components/FilamentPicker.tsx` (a `source: "trays"` answer shows each choice as "<material> <colour word> in AMS-A slot 1", the AMS percent when known, and over it each provider record's brand, name and remaining grams, labelled by provider; `overlay_notes` as a quiet note above the slots), `frontend/src/components/print/TrayQuestion.tsx` (when `source === "trays"`, an untagged tray's question offers the providers' unlinked records and links through Task 6c's `PUT …/trays/{ams_id}/{tray_id}/spool`, never Bambuddy's assignment), `frontend/src/components/ProjectPicker.tsx` (on a native printer: native folders from `/print/projects?native=true`, no projects), `frontend/src/components/PrintPicker.tsx` (passes the registry printer to the dialog reads; 3a Task 10 adds the picker)
- Create: `frontend/src/mocks/features/hotPath.ts` (exports `handlers`, `reset`, and `setHotPath(on: boolean)` for the unit tests): with it on, `GET /settings` answers `bambuddy_url: null`, `slicer_backend: 'native'`, `library_backend: 'native'`, `store_backend: 's3'`; `GET /printers` (3a's) one LAN-linked H2C with `preferred_path: 'native'`; the filaments route answers `source: 'trays'` from two trays, one with a `records.bambuddy` overlay and one untagged (its question lists one unlinked record), and, with `localStorage['sb-mock-hot-path-stale'] === '1'`, no records and one `overlay_notes` entry; the tray link `PUT` answers the linked tray; `/print/projects?native=true` answers two folders; the preview start answers `slicer: 'native'` and the read answers `running` with `progress` 20, then 60, then `completed` (as slicer Task 14's mock does); the run answers 202 then a result with no `bambuddy_url`; `/print/printers/now` (3a Task 6's shape, `path: 'native'`) goes `printing` 30 %, 80 %, then `completed`; and every Bambuddy-only route (camera, inventory, `/print/projects` without `native`) answers 409 "no Bambuddy URL is configured", so a component that still calls one shows an error the spec catches
- Create: `frontend/e2e/hot-path.spec.ts`
- Test: `frontend/src/components/FilamentPicker.test.tsx`, `frontend/src/components/ProjectPicker.test.tsx`, `frontend/src/mocks/features/hotPath.test.ts`

**Interfaces:**
- Consumes: `slice-progress` and `slicer` on the reads (slicer Task 14), `PrinterNow.path`/`id` and the Printers settings section (3a Task 10), `FilamentOptions.source`/`overlay_notes`, `SpoolOption.remain_percent`/`records` and the tray link routes (Task 6c) from the regenerated `schema.d.ts`.
- The switch reaches the msw handlers through `localStorage`, because the handlers' state lives in the page and a full navigation resets it, and because Playwright's `page.request` does not pass through the service worker: `hotPath.ts` reads `localStorage['sb-mock-hot-path'] === '1'` in its `reset()` and at module load (in a `try`, as every browser-storage read is), and the spec sets it with `page.addInitScript`. Inside the walk the spec navigates by the app's own links, so the mock's per-run progress is kept. If the mocks already have a scenario switch by the time this task runs (`git grep -n "localStorage" frontend/src/mocks`), use it instead.

- [ ] **Step 1: Write the failing component tests** — FilamentPicker with `source: 'trays'` lists trays by slot label and percent, with a tagged tray's Bambuddy record (brand, grams) and the note when `overlay_notes` is set; TrayQuestion on a native run links a record through the tray route; ProjectPicker on a native printer lists folders only.
- [ ] **Step 2: Write the failing e2e spec** (AC3, AC6)

```ts
test.describe('native hot path (msw)', () => {
  test.skip(!!process.env.E2E_BASE_URL, 'msw-backed; the real stack is real-hot-path.spec.ts')
  test.beforeEach(async ({ page }) => { await page.addInitScript(() => localStorage.setItem('sb-mock-hot-path', '1')) })

  test('renders, slices with progress, prints on a LAN printer and follows it, with no Bambuddy', async ({ page }) => {
    // hotPath.ts answers every Bambuddy-only route 409, so any 409 is a component still asking Bambuddy.
    const bambuddy: string[] = []
    page.on('response', (r) => { if (r.status() === 409) bambuddy.push(r.url()) })
    await page.goto('/m/name-keychain')
    await expect(page.getByTestId('bbox-readout')).toBeVisible()
    await page.getByTestId('generate').click()
    await expect(page.getByText(/^Saved /)).toBeVisible()
    await page.getByTestId('print').click()
    const dialog = page.getByRole('dialog', { name: 'Print' })
    await expect(dialog.getByTestId('slice-progress')).toHaveAttribute('value', '20')
    await expect(dialog.getByTestId('slice-progress')).toHaveAttribute('value', '60')
    await expect(dialog.getByTestId('slice-status')).toContainText(/sliced|min/i)
    await expect(dialog.getByTestId('filament-slot-1')).toContainText('AMS-A slot 1')
    await expect(dialog.getByTestId('filament-slot-1')).toContainText(/Bambuddy/)       // the overlay
    await expect(dialog.getByText(/project/i)).toHaveCount(0)
    await dialog.getByRole('button', { name: 'Print' }).click()
    await page.getByRole('link', { name: 'Prints' }).click()      // in-app, so the mock keeps the run
    const card = page.getByTestId('now-printing').getByText('H2C')
    await expect(card).toBeVisible()
    await expect(page.getByTestId('now-printing')).toContainText('LAN')
    await expect(page.getByTestId('now-printing')).toContainText(/completed|finished/i)
    expect(bambuddy).toEqual([])
  })

  test('a stale overlay shows the trays with a note, and still prints', async ({ page }) => {
    await page.addInitScript(() => localStorage.setItem('sb-mock-hot-path-stale', '1'))
    // open the dialog as above; the note names Bambuddy; Print succeeds
  })
})
```

(The test ids not yet defined, `now-printing`, `slice-status`, come from `NowPrinting.tsx` and `PreviewPane.tsx` as they stand after 3a Task 10 and slicer Task 14; use theirs.)

- [ ] **Step 3: Run to verify they fail** — `cd frontend && pnpm test -- FilamentPicker ProjectPicker hotPath && pnpm exec playwright test e2e/hot-path.spec.ts`.
- [ ] **Step 4: Implement** the components and the mocks.
- [ ] **Step 5: Run** — `pnpm lint && pnpm typecheck && pnpm test && pnpm build && pnpm exec playwright test` → PASS (`print.spec.ts` unchanged).
- [ ] **Step 6: Commit** — `git add frontend && git commit -m "feat(frontend): the print dialog on the native hot path, and its mocked e2e walk (#3818)"`

---

### Task 12: A fake printer and a Bambuddy recorder

**Files:**
- Create: `e2e-stack/fake-printer/` — `Dockerfile` (`python:3.12-slim` pinned by digest; `aiomqtt`, `pyftpdlib` pinned), `printer.py`, `mosquitto.conf`, (usage in `printer.py`'s module docstring, no README)
- Create: `e2e-stack/bambuddy-recorder/` — `Dockerfile`, `recorder.py`
- Test: `backend/tests/printers/test_fake_printer.py` (runs `printer.py` in-process against the 3a `LanPrinter`)

**Interfaces:**
- **Fake printer.** A Mosquitto 2 broker (`eclipse-mosquitto:2` pinned by digest) with one TLS listener on 8883 (a self-signed certificate generated at container start; `password_file` with user `bblp` and the access code from `FAKE_PRINTER_ACCESS_CODE`), because the printer is the broker a LAN client connects to (spec §2: MQTT over TLS on 8883, user `bblp`). `printer.py` connects to it as the printer: subscribes `device/<serial>/request`; answers `pushing.pushall` with a full report on `device/<serial>/report` (3a Task 5's recorded `lan-report-h2c.json`, serial replaced); on `print.project_file` records the command and runs `gcode_state` `PREPARE` → `RUNNING` (`mc_percent` 0→100 over `FAKE_PRINT_SECONDS`, default 20) → `FINISH`; on `print.stop`, `FAILED`. It serves implicit FTPS on 990 as `bblp` with the same certificate (pyftpdlib's `TLS_FTPHandler`; pyftpdlib speaks explicit TLS, so the listening socket is wrapped in TLS at accept, as ha-bambulab's client wraps its socket at connect, `pybambu/bambu_client.py` `ImplicitFTP_TLS`; if pyftpdlib cannot run over a pre-wrapped socket, put `stunnel` in front of a plain pyftpdlib and say so in the docstring), storing uploads under `/sdcard` and the upload directory 3a Task 7 measured (`FAKE_PRINTER_UPLOAD_DIR`, defaulting to it once measured). `GET :8081/__received` answers `{"mqtt": [...commands], "ftps": [{"name", "size", "md5"}]}`; `POST :8081/__reset` clears it.
- **Bambuddy recorder.** An HTTP server on 8000 that records every request `{method, path, at}`. It answers `GET /api/v1/inventory/spools` and `GET /api/v1/inventory/assignments` from this repo's recordings (`tests/bambuddy/recordings/inventory-spools.json`, one spool's `tray_uuid` set to the fake printer's tagged tray), so the seed's one overlay refresh succeeds, and every other path with `599`. `GET /__recorded` and `POST /__reset` are its own paths, never Bambuddy's `/api/v1/*`. Pointed at by `bambuddy_url`, so any hot-path call both fails and is listed. The fake printer's report gives one tray a non-zero `tag_uid`/`tray_uuid` (the fields as `mqtt.md`'s report example has them).

- [ ] **Step 1: Write the failing test** — with `printer.py` running against a local mosquitto (skip unless `mosquitto` is on PATH or `SCADBUDDY_TEST_MQTT` names one), 3a's `LanPrinter.probe` gets a report, `upload` stores the file (md5 matches), `publish(project_file_command(...))` is recorded and the reports reach `FINISH`.
- [ ] **Step 2: Run to verify it fails** — `cd backend && uv run --frozen pytest tests/printers/test_fake_printer.py -v`.
- [ ] **Step 3: Implement**, then `docker build -t scadbuddy-fake-printer:dev e2e-stack/fake-printer && docker build -t scadbuddy-bambuddy-recorder:dev e2e-stack/bambuddy-recorder`, and `hadolint --config .hadolint.yaml e2e-stack/*/Dockerfile`.
- [ ] **Step 4: Run** Step 2's command → PASS.
- [ ] **Step 5: Commit** — `git add e2e-stack backend/tests/printers/test_fake_printer.py && git commit -m "test(e2e): a fake LAN printer and a Bambuddy recorder for the hot-path stack (#3818)"`

---

### Task 13: The real-stack e2e (layer 2)

No compose file exists in the repo today (`git ls-files | grep -i compose` lists only vendored Go files on `7613580bf`), and the `image` CI job runs `real-backend.spec.ts` against one container (`.github/workflows/ci.yml:1018-1103`). The hot path needs six, so it gets a stack of its own.

**Files:**
- Create: `e2e-stack/compose.yaml`, `e2e-stack/run.sh`, `e2e-stack/seed.sh`
- Create: `frontend/e2e/real-hot-path.spec.ts`
- Create: `.github/workflows/hot-path-e2e.yml`
- Modify: `.github/scripts/` shellcheck coverage is automatic (`shellcheck .github/scripts/*.sh`); add `e2e-stack/*.sh` to the `lint` job's shellcheck line in `ci.yml`
- Decide and do #4187 (Task 12's fake-printer and recorder tests run in no CI job, and `e2e-stack/*.py` is never linted): either mount `e2e-stack/` into the backend-pytest run and give the test image mosquitto, or run both test files and `ruff check e2e-stack` in `hot-path-e2e.yml`

**Interfaces:**
- `compose.yaml` services, every image pinned by digest except the two built here:
  - `postgres` (`postgres:17`), `temporal` (the test image's `/usr/local/bin/temporal server start-dev --ip 0.0.0.0`, `CLAUDE.md` "Commands"), `s3` (`motoserver/moto` or `minio/minio`; the builder picks one and says why; the LIB Ruling 16 CI run already uses moto from the test image),
  - `app` (`scadbuddy:app`, `docker build --target app`; `SCADBUDDY_TEMPORAL_WORKER_INPROCESS=true` so the render, library and print queues run in it; `SCADBUDDY_SECRET_KEY_FILE` from a generated key; `SCADBUDDY_API_INTERNAL_URL=http://app:8080`),
  - `slicer` (`scadbuddy-slicer:dev`, `docker build --target slicer`, slicer Task 5; `SCADBUDDY_TEMPORAL_ADDRESS=temporal:7233`),
  - `printer` (Task 12), `bambuddy` (Task 12's recorder).
- `seed.sh MODE` (`MODE` = `recorder` | `unset`): `PUT /api/v1/settings` with `store_backend=s3` and the `store_s3_*` of the `s3` service, `library_backend=native`, then restarts `app` (both apply at restart), then `slicer_backend=native` and `bambuddy_url=http://bambuddy:8000` (recorder) or `null` (unset); in recorder mode it triggers the `spool-providers` Schedule once (`temporal schedule trigger --schedule-id spool-providers`), waits for `spool_provider_refreshes` to show it, sets `spool_overlay_refresh_seconds=0` and `POST`s the recorder's `/__reset`, so what the recorder sees afterwards is the hot path alone; `POST /api/v1/printers` with the fake's serial and `PUT /api/v1/printers/{id}/lan` with `printer:8883` and the access code (3a Task 3), `preferred_path: native`.
- `run.sh [--mode recorder|unset|both] [--keep]`: builds the images (unless `--no-build`), `docker compose -p sb-hotpath up -d --wait`, seeds, runs `cd frontend && E2E_BASE_URL=http://127.0.0.1:18080 E2E_HOT_PATH=1 E2E_PRINTER_URL=http://127.0.0.1:18081 E2E_RECORDER_URL=http://127.0.0.1:18000 pnpm exec playwright test e2e/real-hot-path.spec.ts`, then `docker compose -p sb-hotpath down -v` unless `--keep`. It never kills anything by name; teardown is compose's.
- `real-hot-path.spec.ts`: skips unless `E2E_BASE_URL` and `E2E_HOT_PATH=1`. One test: the Task 11 walk against the real stack (a real OpenSCAD render, the real slicer worker's progress bar, the trays from the fake printer's report, Print, Now printing to `completed`), then asserts `GET $E2E_PRINTER_URL/__received` has one `project_file` whose `ams_mapping` names the two trays chosen and one FTPS upload whose md5 matches the command's, the tagged tray shows its Bambuddy record in the dialog, and `GET $E2E_RECORDER_URL/__recorded` is `[]` (AC1); in `unset` mode the recorder is not configured and the same flow passes (AC2).
- `hot-path-e2e.yml`: `workflow_dispatch` and a weekly `schedule`, `ubuntu-latest` (never a self-hosted pool, `CLAUDE.md` "CI and caching rules"), `permissions: contents: read`, buildx with `driver-opts: image=mirror.gcr.io/moby/buildkit:buildx-stable-1` and `type=gha` cache scopes `hot-path-app`/`hot-path-slicer` with `mode=min`, runs `e2e-stack/run.sh --mode both`, uploads the Playwright report. It is not a PR check and not in `CI Summary`'s `needs` (decision 6 below).

- [ ] **Step 1: Write the spec and the stack.**
- [ ] **Step 2: Run it locally** — `e2e-stack/run.sh --mode both` → both passes green. Then prove it can fail: `e2e-stack/run.sh --mode recorder --keep`, switch the printer's `preferred_path` to `bambuddy` with `curl -X PATCH`, run the spec again → it fails on the recorder's list; `docker compose -p sb-hotpath down -v`.
- [ ] **Step 3: Lint** — `shellcheck e2e-stack/*.sh && actionlint && hadolint --config .hadolint.yaml e2e-stack/*/Dockerfile`.
- [ ] **Step 4: Run the workflow once** — `gh workflow run hot-path-e2e.yml --ref <branch>` and watch it with `gh run watch`.
- [ ] **Step 5: Commit** — `git add e2e-stack frontend/e2e/real-hot-path.spec.ts .github && git commit -m "test(e2e): the native hot path against a real stack with a fake printer and a failing Bambuddy (#3818)"`

---

### Task 14: Docs, spec corrections, the prod checklist and its handoff (layer 3)

**Files:**
- Modify: `docs/superpowers/specs/2026-10-16-own-store-slicer-rust-workers-design.md` — §1.2 and §6.3: a native run reads no Bambuddy project, archive or queue, and reads Bambuddy's inventory only through the off-path provider refresh; filaments are the loaded trays with the physical-spool overlay (D1), the native inventory named as tier B's native provider; §5.3: a native run offers only the image's presets (D5); §6.2: printer state from the LAN link first (D4), `ams_mapping`/`ams_mapping2` and `nozzle_mapping` computed by ScadBuddy (Tasks 7, 8); §2: Task 7's and Task 8's measurements
- Modify: `docs/superpowers/plans/2026-10-17-native-print-library.md` Ruling 7: one sentence, "revised for native runs by the 2026-10-18 hot-path plan, D6"
- Modify: `README.md` "Deploying": the four switches for the native hot path and their order; the real-stack e2e (`e2e-stack/run.sh`)
- Modify: `CLAUDE.md` "Layout": `printers/hot_path.py`, `printers/ams_mapping.py`, `slicing/catalogue.py`, `slicing/values.py`, `print_library/copies.py`, `e2e-stack/`; "Commands": the real-stack e2e line
- Modify: `agent/src/tools/print.ts` descriptions that say every print goes to Bambuddy (search `Bambuddy` in the print tools' descriptions; the MCP server instructions' "sends multi-colour 3MFs to Bambuddy" stays until tier B's capabilities), with `cd agent && pnpm test`

**The prod checklist** (posted, not committed as a file; the comment is its record):

1. Preconditions: the release with Tasks 1–13, the slicer worker and the print worker on the same build; `store_backend=s3` (prod since 2026-10-18 03:51 UTC); KEK mounted on the API and the print worker.
2. Settings, in order: `library_backend=native` (restart), `slicer_backend=native` (live); Settings → Printers: the H2C linked by LAN (3a), `preferred_path=native`; `bambuddy_url` left set.
3. One print of a two-colour template from the Customize page: the dialog shows "Slicing… N%" with a bar, filament slots list AMS trays with Bambuddy's spool records over the tagged ones (Settings shows the last refresh), no project picker; then one print of a plate that uses the rack, after Task 8's measurement, and the H2C picks the hotends the dialog showed; Print; Now printing shows the card with "LAN" and reaches completed (cancel after the first layer if the bed is not clear, and record that).
4. Tempo (Grafana Explore, the `DS_TEMPO` datasource): find the run's request trace with TraceQL `{ name = "POST /api/v1/print/outputs/{output_id}/run" }` in the print's minute, then `{ trace:id = "<that id>" && name =~ "bambuddy\\..*" }` → no spans; the same for the preview slice's trace (`{ name =~ "POST /api/v1/print/outputs/.*/preview-slice" }`) and the `FollowPrint` workflow's trace (linked from the run's, `TracingInterceptor`, `workflows/client.py:75`). Housekeeping sweeps and the `spool-providers` refresh still call Bambuddy on their own traces (`workflows/housekeeping.py`, `main.py:643,758,774` `_with_bambuddy`; Task 6b); they are not the hot path and are not counted.
5. Bambuddy's own log for the print's minute shows no request from ScadBuddy's pods except those sweeps.
6. Flip back: `preferred_path=bambuddy`, one print through Bambuddy, which still works (AC8).
7. Report: the trace ids, a screenshot of the empty TraceQL result, and anything that did not match, as a comment on the discussion and on #3818.

- [ ] **Step 1: The doc and spec edits, and the agent descriptions.** Run `bash .github/scripts/lint-plugin.sh && cd agent && pnpm lint && pnpm test`.
- [ ] **Step 2: File the follow-ups** with `gh issue create --repo eh-homelab/ScadBuddy`, each `Refs #3818`: the per-print native copy's lifetime (Task 4 never deletes), the native spool provider (tier B: its records, consumption from `used_g`, and its editor), `output_last_prints` for a LAN print (§6.3 says it is kept either way; 3a Task 9 records no queue item), and Bambuddy `local` presets on the native path (D5).
- [ ] **Step 3: Commit** — `git add docs README.md CLAUDE.md agent && git commit -m "docs: the native hot path, its decisions and its checks (#3818)"`
- [ ] **Step 4: Hand the checklist to the fleet's exploratory testing session** on https://github.com/orgs/WonderForgeLabs/discussions/66, as #3820's cut-over was: a comment with the checklist above, the release tag, and "report on #3818". `gh api graphql -f query='mutation($id:ID!,$body:String!){addDiscussionComment(input:{discussionId:$id,body:$body}){comment{url}}}' -f id=<discussion node id> -f body=@checklist.md` (the node id from `gh api graphql -f query='{repository(owner:"WonderForgeLabs",name:"WonderForgeLabs"){discussion(number:66){id}}}'`).

---

## Self-review

- **Scope coverage.** A-copy → Task 4 (and AC5); A-catalogue → Task 2; A-values → Task 3; A-ams → Task 7, plus the 3a plan correction in this plan's commit; A-rack → D7, Task 8 (measure with the user, then send `nozzle_mapping`; a fallback only on a negative measurement, decided then); A-trays → D1, Tasks 6a–6c (physical spools, providers, the overlay); A-optional-client → D3, Task 9 (readers outside `client_for`: `api/deps.py:390-406`, `worker.py:915-922`), Task 10; A-destination → D6, Task 5; acceptance → Tasks 1, 9, 10 (backend), 11 (mocked e2e), 13 (real stack, both modes), 14 (prod). Slicer Tasks 11–14 and 3a Tasks 1–12 are prerequisites, not repeated. #3824 (render tools) is not touched: the render's Bambuddy calls were tier 1's and are gone on `s3`.
- **Inventory drift found on re-reading main:** `prepare_run` `:1228` (inventory `:1219`), `_catalogue` in `prepare_run` `:1319` (`:1307`), `require_destination_folder` `:1251` (`:1240`), `client.spools()` in `rack_preview` `:1075` (`:1066`), `client.projects()` `:493` (`:492`), last bed type `:2058` (`:1978`), `gather_plate_options` `:915` (`:902`). `slicing/` on main holds only `models.py`; `profiles.py`/`presets.py` are on `feat/native-slicer-caller` (#4162).
- **Not in tier A, and why:** the native spool provider and its consumption rules (D1, tier B), projects (D6), archives/history and last bed type (skipped on a native run: an advisory warning), the queue (none on LAN, §6.3), camera, timelapse, attachments, remembered choices for a LAN-only printer (3a "Deferred to 3b").
- **Proportion.** Tests and signatures in code blocks; bodies only where the harness and the stack's protocols are fixed.

## Decisions that need the user

Settled by the user on 2026-10-18 and built in: the overlay design (D1), the native-library destination (D6), the LAN report as the state source on a native run (D4, with 3a Tasks 6 and 8 edited to match), and rack prints natively after a measurement (D7). Still open:

1. D5: Bambuddy `local` (OrcaSlicer-import) presets are not offered on a native run; or read a kept copy (a Bambuddy dependency off the hot path), or import them into ScadBuddy (inventory open question 7).
2. D7, decide then: only if Task 8's measurement finds the H2C cannot be told `nozzle_mapping` over LAN, which fallback (refuse with a reason, let the firmware pick, or the Bambuddy path).
3. D1 details: `SPOOL_OVERLAY_STALE` 30 minutes and a five-minute refresh by default; one record per provider per physical spool (a provider's duplicate rows for one spool are not both shown).
4. The real-stack e2e runs by hand and weekly/dispatch, not on PRs or in the required `CI Summary`; adding it to PRs costs a slicer image build (the AppImage) per run on hosted runners.
5. A LAN print records no `output_last_prints` row and no archive (tier B history), so the output page shows no last print for it.
6. A session at the H2C with the user for Task 8's measurement (and Task 7's live check), and who presses Print on prod for Task 14's check, with the bed clear.
