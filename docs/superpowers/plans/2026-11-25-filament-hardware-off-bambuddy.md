# Filament Hardware off Bambuddy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A printer's K-profiles, its AMS slot control and the RFID override reconcile's slot configure reach the printer over ScadBuddy's own LAN link (MQTT), never through Bambuddy, keeping the stopgaps' routes, UI and agent tools (#7276).

**Architecture:** One new Temporal activity, `lan_command` (beside `lan_print`, on the print worker's `bambuddy` queue), publishes one command with a fresh `sequence_id` on a short MQTT connection and waits a bounded time for the reply whose `command` and `sequence_id` match. A small workflow, `LanCommand`, runs it once per command; the API's routes and the RFID reconcile start that workflow and wait. Payloads are pure builders in `printers/lan_commands.py`, each citing its public source. State checks read the printer's stored LAN report (`PrinterReportStore`); the data Bambuddy kept (K-profile notes, slot presets, AMS labels) moves to ScadBuddy's tables and places.

**Tech Stack:** Python 3.12, FastAPI, temporalio, aiomqtt, psycopg (backend); TypeScript, zod (agent); React, msw, vitest (frontend); mosquitto + `e2e-stack/fake-printer/printer.py` (fake printer).

**Spec:** the rulings R-7276-1 to R-7276-7 in the last comment of eh-homelab/ScadBuddy#7276 (binding; not reopened here), plus this plan's "Rulings" section. Background specs: `docs/superpowers/specs/2026-09-24-print-flow-design.md` (§2, the LAN path) and the hot-path plan `docs/superpowers/plans/2026-10-18-hot-path-without-bambuddy.md`. The stopgaps it ports: `docs/superpowers/plans/2026-11-23-ams-slot-control.md` (#7153, PR #7252, #7508) and `docs/superpowers/plans/2026-11-23-printer-kprofiles.md` (#7154, PR #7173). There is no separate design doc.

## Global Constraints

- Bambuddy is a sync target only (owner, 2026-11-23). No new or kept code in this plan acts on a printer, an AMS, a scale or a tag reader through Bambuddy. When this plan is done, no filament route or agent tool writes to hardware through Bambuddy.
- Tests touch live printers and Bambuddy with GET only, and in this plan not at all: Bambuddy is respx (with `assert_all_called=False` and no route for any Bambuddy path, so any call fails the test), the printer is the fake printer or an injected `LanDeps.connect`.
- The live H2C check of the payloads (R-7276-5) is a manual step (Task 14), run only after the owner says OK. Never automate it, never run it from CI, never run it without that OK.
- Every payload builder cites a public source link in its docstring (R-7276-2): Bambuddy's `bambu_mqtt.py` at the pinned commit `d3be761f3bd0b6bac78045dc53599331d64c3672` (tag v1.3.1), OpenBambuAPI `mqtt.md`, or ha-bambulab. Link form: `https://github.com/maziggy/bambuddy/blob/d3be761f3bd0b6bac78045dc53599331d64c3672/backend/app/services/bambu_mqtt.py#L<a>-L<b>`.
- A printer without a LAN link (not `physical`, or no `lan_host`, `lan_cert_sha256` or access code) gets 409 with `code` `not_lan_linked` and a detail that says to link it over the LAN in Settings > Printers. No Bambuddy fallback (R-7276-4).
- Every setting rememberable, no per-setting allowlists (root CLAUDE.md "PR conventions"; #2998, #6219). This plan adds no setting; a slot configure takes the values it is sent, it does not allowlist slicer keys.
- Only the CRUD service (the API) gets SQL (root CLAUDE.md #5295). The print worker reaches the new tables through nothing: only the API's routes read and write them. New tables are new migration files only; never edit a merged one.
- Workflow and activity rules (`backend/CLAUDE.md`): `LanCommand` is a new workflow type on the `bambuddy` queue, AUTO_UPGRADE by `print_deployment`'s default; it gets a replay fixture, and any later change to its commands goes behind `workflow.patched`. A release with a new workflow type rolls `scadbuddy-print` with `Recreate` (README "A release that needs `Recreate`").
- A change to what a native run reads keeps `e2e-stack/`'s recorder pass at zero Bambuddy calls.
- Backend tests that build the app or use Postgres need `SCADBUDDY_TEST_DATABASE_URL`; on this machine run one Postgres per run in Docker (`docker run -d --rm --name sbtest-7276 -p 55437:5432 -e POSTGRES_HOST_AUTH_METHOD=trust mirror.gcr.io/library/postgres:18 -c timezone=UTC`, then `docker exec sbtest-7276 createdb -U postgres scadbuddy_test`, then `SCADBUDDY_TEST_DATABASE_URL=postgresql://postgres@127.0.0.1:55437/scadbuddy_test`; remove the container by name when done). `tests/api` also needs Temporal (`temporal` CLI on PATH). Fake-printer tests need `mosquitto` (or `SCADBUDDY_TEST_MQTT`).
- Plugin files: this plan changes none. If a task does change `plugins/`, bump the plugin version in both copies in that task.
- Docs cite sources (#259): every measured fact says where and how; no counts in docs.

## Review Focus

1. **A stale or missing report.** The printer's watch is down, or its report is older than `is_fresh` allows. A person expects the AMS view to say the printer is offline and every write to refuse with 409 `printer_offline`, not to send blind. Test: Task 8 `test_a_stale_report_refuses_every_write_printer_offline`.
2. **No reply, or a refusal.** The printer never answers within the wait, or answers `result: "fail"`. A person expects a write to answer `confirmed: false` (sent, not acknowledged), a read to answer 502 `printer_no_answer`, and a refusal to be 409 `printer_refused` with the printer's reason. Tests: Task 2 `test_no_reply_is_unconfirmed_not_failed`, `test_a_fail_reply_is_refused_with_its_reason`, `test_a_read_with_no_reply_is_no_answer`.
3. **A resend after a lost answer.** The browser or the agent sends a K-profile write again with the same `Idempotency-Key`. A person expects the printer to get the command once. Test: Task 7 `test_a_resent_key_publishes_once`.
4. **A printer linked both ways, or only to Bambuddy.** A row with a LAN link and a Bambuddy link must use the LAN and make zero Bambuddy calls; a Bambuddy-only row gets 409 `not_lan_linked`. Tests: Task 7 and Task 8 `test_a_bambuddy_only_printer_is_not_lan_linked`, `test_a_printer_linked_both_ways_never_calls_bambuddy`.
5. **Unit addressing.** An AMS-HT (128), the external holder on a printer with one or two `vt_tray` entries, and Ext-R (255) each need their own wire ids. A person expects the slot they clicked to be the one configured. Test: Task 1 `test_wire_ids` (parametrized over every unit kind and both builders that differ: `ams_filament_setting` local, `extrusion_cali_sel` global).

---

## File structure

Create:
- `backend/scadbuddy/printers/lan_commands.py`: pure payload builders and reply matching (one function per command, each with its citation).
- `backend/scadbuddy/printers/lan_command_models.py`: what crosses the history (`LanCommandInput`, `LanCommandReply`, `LanCommandRun`, `LanCommandOutcome`).
- `backend/scadbuddy/workflows/lan_command.py`: `LanCommandWorkflow` and `execute_lan_commands`.
- `backend/scadbuddy/printers/native.py`: the LAN-link gate and the stored-report status read shared by both features.
- `backend/scadbuddy/printers/command_data.py`: `PrinterCommandData`, the store of K-profile notes and slot presets.
- `backend/scadbuddy/printers/kprofiles.py`: the K-profile bodies (moved from `bambuddy/kprofiles.py`) and the LAN reads and writes.
- `backend/scadbuddy/printers/ams_slots.py`: the slot views, the guard and the configure values (moved from `bambuddy/slot_control.py`), now on the stored report.
- `backend/scadbuddy/migrations/<UTC>_printer_kprofile_notes_slot_presets.sql`.
- `backend/tests/printers/test_lan_commands.py`, `test_lan_command_activity.py`, `test_lan_command_fake.py`, `test_command_data.py`; `backend/tests/test_lan_command_replay.py`; `backend/tests/fixtures/lan_command_histories/`.
- `agent/src/tools/printerKProfiles.ts`, `agent/test/printerKProfiles.test.ts`.

Modify: `printers/lan.py` (one request method), `printers/print_activities.py` (activity), `workflows/print_models.py` (names), `workflows/client.py` and `main.py` (registration), `printers/status.py` and `bambuddy/models.py` (`AmsTray` fields), `printers/store.py` (`PRINTER_REFERENCES`), `api/printer_kprofiles.py`, `api/printer_slots.py`, `bambuddy/operations.py`, `filament/rfid_slots.py`, `spools/activities.py`, `api/filament.py`, `e2e-stack/fake-printer/printer.py` and `report.json`, the agent's `printerSlots.ts`, `coverage.ts`, `index.ts`, `sessions/touched.ts`, the frontend's `Printers.tsx`, `PrinterAmsSlots.tsx`, `PrinterKProfiles.tsx`, their tests and mocks, docs.

Delete (Task 12): `bambuddy/kprofiles.py`, `bambuddy/slot_control.py`, the client methods and models only they used, `tests/bambuddy/test_client_kprofiles.py`, `tests/bambuddy/test_client_slots.py`.

---

### Task 1: Payload builders with citations

**Files:**
- Create: `backend/scadbuddy/printers/lan_commands.py`
- Test: `backend/tests/printers/test_lan_commands.py`

**Interfaces:**
- Consumes: nothing new.
- Produces (every builder returns `{"print": {...}}` with `"sequence_id": seq`):
  - `extrusion_cali_get(nozzle_diameter: str, seq: str) -> dict[str, Any]` — source L7542-L7619.
  - `extrusion_cali_set(rows: Sequence[KProfileRow], nozzle_diameter: str, seq: str) -> dict[str, Any]` — L7666-L7825. `KProfileRow` is a frozen dataclass here: `cali_idx: int, extruder_id: int, nozzle_id: str, nozzle_diameter: str, filament_id: str, name: str, k_value: str, setting_id: str, n_coef: str, ams_id: int, tray_id: int`.
  - `extrusion_cali_del(*, cali_idx: int, filament_id: str, nozzle_id: str, nozzle_diameter: str, extruder_id: int, setting_id: str | None, dual_nozzle: bool, seq: str) -> dict[str, Any]` — L7827-L7906 (the two shapes).
  - `ams_filament_setting(slot: WireSlot, *, tray_info_idx: str, tray_type: str, tray_sub_brands: str, tray_color: str, nozzle_temp_min: int, nozzle_temp_max: int, setting_id: str | None, seq: str) -> dict[str, Any]` — L8680-L8785; `tray_color` upper-cased `RRGGBBAA` (L8757 comment, #2987 there).
  - `ams_reset(slot: WireSlot, seq: str) -> dict[str, Any]` — L8786-L8852: `ams_filament_setting` with empty values, `tray_color` `"00000000"`, temps 0.
  - `extrusion_cali_sel(ams_id: int, tray_id: int, *, external_count: int, cali_idx: int, filament_id: str, nozzle_diameter: str, seq: str) -> dict[str, Any]` — L8854-L8942 (global tray id, no `setting_id`).
  - `extrusion_cali_set_slot(global_tray: int, *, k_value: float, nozzle_diameter: str, filament_id: str, setting_id: str, name: str, seq: str) -> dict[str, Any]` — L8944-L9020 (`n_coef` `"1.400000"`, `cali_idx` -1).
  - `ams_get_rfid(ams_id: int, slot_id: int, seq: str) -> dict[str, Any]` — L8567-L8648.
  - `ams_change_filament_load(global_tray: int, *, extruder_id: int | None, right_temp: int, seq: str) -> dict[str, Any]` — L8337-L8429, wire ids L239-L254.
  - `ams_change_filament_unload(source_tray: int, *, nozzle_temp: int, seq: str) -> dict[str, Any]` — L8431-L8543.
  - `WireSlot` (frozen dataclass `ams_id, tray_id, slot_id`) and `wire_slot(ams_id: int, tray_id: int, *, external_count: int) -> WireSlot`: the local ids `ams_filament_setting` and reset use (L8706-L8752). `external_count` is the report's `vt_tray` length.
  - `matches(reply: Mapping[str, Any], command: str, seq: str) -> bool` and `refusal(reply: Mapping[str, Any]) -> str | None` (the reason when `result` is `fail`, case-folded, as `await_cali_ack` L7636-L7664 reads it).
  - `UNLOAD_FLOOR = 180`, `UNLOAD_DEFAULT = 210`, `RIGHT_DEFAULT = 215` (L8380-L8390, L8505-L8510).

- [ ] **Step 1: Write the failing tests**

```python
def test_cali_get_shape() -> None:
    assert extrusion_cali_get("0.4", "7") == {"print": {"command": "extrusion_cali_get",
        "filament_id": "", "nozzle_diameter": "0.4", "sequence_id": "7"}}

@pytest.mark.parametrize(("ams_id", "tray_id", "external_count", "local", "cali_sel_ids"), [
    (0, 2, 1, (0, 2, 2), (0, 2, 2)),          # AMS 0: local tray 2, global 2
    (1, 3, 1, (1, 3, 3), (1, 7, 3)),          # AMS 1 slot 3: global 7
    (128, 0, 1, (128, 0, 0), (128, 0, 0)),    # AMS-HT
    (255, 0, 1, (255, 254, 0), (254, 254, 0)),  # single external holder
    (255, 1, 2, (255, 0, 0), (255, 255, 0)),  # Ext-R on a printer with two
])
def test_wire_ids(ams_id, tray_id, external_count, local, cali_sel_ids) -> None:
    assert astuple(wire_slot(ams_id, tray_id, external_count=external_count)) == local
    body = extrusion_cali_sel(ams_id, tray_id, external_count=external_count, cali_idx=3,
                              filament_id="GFL05", nozzle_diameter="0.4", seq="1")["print"]
    assert (body["ams_id"], body["tray_id"], body["slot_id"]) == cali_sel_ids
    assert "setting_id" not in body

def test_colour_goes_upper_case() -> None: ...  # "ff00aaff" -> "FF00AAFF"
def test_reset_clears_every_value() -> None: ...
def test_cali_del_dual_has_no_setting_id_and_single_does() -> None: ...
def test_load_names_the_extruder_only_when_given() -> None: ...
def test_unload_takes_the_floor_temperature() -> None: ...  # nozzle 25 -> 210
def test_every_builder_cites_a_pinned_source() -> None:
    for fn in BUILDERS:  # the module's tuple of builders
        assert "d3be761f3bd0b6bac78045dc53599331d64c3672" in (fn.__doc__ or "")
def test_matches_needs_command_and_sequence() -> None:
    assert matches({"command": "extrusion_cali_set", "sequence_id": "9"}, "extrusion_cali_set", "9")
    assert not matches({"command": "extrusion_cali_set", "sequence_id": "0"}, "extrusion_cali_set", "9")
def test_refusal_reads_fail_case_folded() -> None:
    assert refusal({"result": "FAIL", "reason": "ERROR STATE"}) == "ERROR STATE"
    assert refusal({"result": "success"}) is None
```

The AMS 1 slot 3 `extrusion_cali_sel` global id is `legacy_tray_index` (ams*4+slot), L8907-L8910. The external rows follow L8706-L8721 (local) and L8889-L8901 (global).

- [ ] **Step 2: Run, expect FAIL** (`ModuleNotFoundError`)

Run: `cd backend && uv run --frozen pytest tests/printers/test_lan_commands.py -q`

- [ ] **Step 3: Implement `backend/scadbuddy/printers/lan_commands.py`**

Pure functions, no I/O. The module docstring names the sources and that the H2C specifics (R-7276-5) are unverified until Task 14. `BUILDERS` is a module tuple of every builder.

- [ ] **Step 4: Run, expect PASS**; then `uv run --frozen ruff check . && uv run --frozen ruff format --check . && uv run --frozen mypy`

- [ ] **Step 5: Commit**

```bash
git add backend/scadbuddy/printers/lan_commands.py backend/tests/printers/test_lan_commands.py
git commit -m "feat(printers): LAN payloads for K-profiles and AMS slots, each with its source (#7276)"
```

---

### Task 2: The `lan_command` activity and the `LanCommand` workflow

**Files:**
- Modify: `backend/scadbuddy/printers/lan.py` (add `LanPrinter.request`)
- Create: `backend/scadbuddy/printers/lan_command_models.py`, `backend/scadbuddy/workflows/lan_command.py`
- Modify: `backend/scadbuddy/printers/print_activities.py` (`LanActivities.lan_command`, in `all()`), `backend/scadbuddy/workflows/print_models.py` (names), `backend/scadbuddy/workflows/client.py` (`print_worker` `workflows=`), `backend/scadbuddy/main.py` (the in-process print worker gets `LanCommandWorkflow` wherever it lists the print workflows)
- Test: `backend/tests/printers/test_lan_command_activity.py`, `backend/tests/test_lan_command_replay.py`, fixture `backend/tests/fixtures/lan_command_histories/one_command.json`

**Interfaces:**
- Consumes: Task 1's builders (callers build payloads; the activity takes a built payload).
- Produces:
  - `LanPrinter.request(command: dict[str, Any], *, name: str, seq: str, wait: float) -> dict[str, Any] | None` in `lan.py`: subscribe to the report topic, publish `command`, return the first raw `print` object (not merged) for which `lan_commands.matches(print, name, seq)`; `None` after `wait` seconds. One connection, closed on return. Never touches `PrinterReportStore`.
  - `lan_command_models.py`: `LanCommandInput(BaseModel)`: `printer_id: str`, `name: str` (the `command` value), `payload: dict[str, Any]`, `wait_s: float`, `kind: Literal["read", "write"]`; `LanCommandReply(BaseModel)`: `confirmed: bool`, `reply: dict[str, Any] | None`, `received_at: datetime | None`; `LanCommandRun(BaseModel)`: `commands: list[LanCommandInput]` (1 to 3); `LanCommandOutcome(BaseModel)`: `replies: list[LanCommandReply]`.
  - `print_models.py`: `LAN_COMMAND_ACTIVITY = "lan_command"`, `LAN_COMMAND_WORKFLOW = "LanCommand"`, `LAN_REPLY_WAIT_WRITE = 6.0` (Bambuddy `await_cali_ack` L7636), `LAN_REPLY_WAIT_AMS = 2.0` (R-7276-10), `LAN_REPLY_WAIT_READ = 4.0`, `LAN_READ_TRIES = 2` (Bambuddy retries the get, L7560-L7618), `LAN_COMMAND_DEADLINE = timedelta(seconds=13)` (under Envoy's 15 s).
  - `LanActivities.lan_command(input: LanCommandInput) -> LanCommandReply`: opens the row and code as `_printer` does (its 409 and 503 unchanged), sets a fresh `seq = str(secrets.randbelow(10**9))` into `payload["print"]["sequence_id"]` (R-7276-1), calls `request`. A matching reply with `refusal(...)` set raises `ApplicationError(type=LAN_REFUSED, non_retryable=True)` with the reason. No reply: `kind="write"` answers `confirmed=False` (R-7276-9); `kind="read"` tries `LAN_READ_TRIES` times, then raises `ApplicationError(type=LAN_NO_ANSWER, non_retryable=True)`. A connect failure raises `LAN_UNSURE` (as `lan_print`). `LAN_NO_ANSWER = "lan_no_answer"` joins the print_models type names.
  - `LanCommandWorkflow.run(run: LanCommandRun) -> LanCommandOutcome`: runs `lan_command` once per command, in order, `maximum_attempts=1`, `start_to_close_timeout` = wait × tries + 5 s; stops at the first failure (re-raised).
  - `execute_lan_commands(client: Client, task_queue: str, printer_id: str, run: LanCommandRun, *, key: str | None) -> LanCommandOutcome`: workflow id `lan-command-<printer_id>-<key or uuid4>`, `id_reuse_policy=REJECT_DUPLICATE`, `id_conflict_policy=USE_EXISTING`, `execution_timeout=LAN_COMMAND_DEADLINE`; on `WorkflowAlreadyStartedError` it reads the closed execution's result by id (R-7276-8). It maps `LAN_REFUSED` to `ApiError(409, code="printer_refused")`, `LAN_NO_ANSWER` and a timeout to `ApiError(502, code="printer_no_answer")`, `LAN_UNSURE` to `ApiError(502, code="printer_unreachable")`, an activity's own 409/503 `ApiError` as it is.

- [ ] **Step 1: Write the failing tests** (`test_lan_command_activity.py`; a `Scripted` `LanPrinter` stand-in injected through `LanDeps.connect`, as `tests/printers/test_lan_start.py` does; Postgres for the printer row)

```python
async def test_the_reply_matching_command_and_sequence_is_answered(pg_pool) -> None:
    lan = Scripted(replies=[{"command": "extrusion_cali_set", "sequence_id": SEQ, "result": "success"}])
    reply = await activities(pg_pool, lan).lan_command(write_input())
    assert reply.confirmed and lan.published[0]["print"]["sequence_id"] == SEQ
async def test_no_reply_is_unconfirmed_not_failed(pg_pool) -> None: ...   # confirmed False
async def test_a_fail_reply_is_refused_with_its_reason(pg_pool) -> None:  # ApplicationError LAN_REFUSED, "ERROR STATE" in message
async def test_a_read_with_no_reply_is_no_answer(pg_pool) -> None:       # 2 publishes, then LAN_NO_ANSWER
async def test_each_command_gets_a_fresh_sequence_id(pg_pool) -> None:   # payload's "0" replaced, two calls differ
async def test_a_row_without_a_lan_link_is_409(pg_pool) -> None:
async def test_the_reply_never_reaches_the_report_store(pg_pool) -> None:  # reports.get unchanged
```

`Scripted.request` returns the first reply whose `matches` holds for the seq it was given (the test reads the seq from `lan.published`). Patch `secrets.randbelow` to fix `SEQ`.

`test_lan_command_replay.py`: `Replayer(workflows=[LanCommandWorkflow])` over every file in `tests/fixtures/lan_command_histories/`, as `tests/test_filament_sync_replay.py` does. Record `one_command.json` from a run against a test worker with a stub activity (the history export is the fixture).

- [ ] **Step 2: Run, expect FAIL**

Run: `cd backend && SCADBUDDY_TEST_DATABASE_URL=… uv run --frozen pytest tests/printers/test_lan_command_activity.py tests/test_lan_command_replay.py -q`

- [ ] **Step 3: Implement** `LanPrinter.request`, the models, the activity, the workflow and `execute_lan_commands`; register `LanCommandWorkflow` in `print_worker`'s list (comment: one LAN command, #7276; AUTO_UPGRADE by the worker's default) and in `main.py`'s in-process print worker. The activity is in `LanActivities.all()`, so both workers get it.

- [ ] **Step 4: Run, expect PASS**, plus `uv run --frozen pytest tests/printers -q -k "lan"` (the print path is unchanged) and ruff, mypy.

- [ ] **Step 5: Commit**

```bash
git add backend/scadbuddy/printers/lan.py backend/scadbuddy/printers/lan_command_models.py \
  backend/scadbuddy/printers/print_activities.py backend/scadbuddy/workflows/lan_command.py \
  backend/scadbuddy/workflows/print_models.py backend/scadbuddy/workflows/client.py backend/scadbuddy/main.py \
  backend/tests/printers/test_lan_command_activity.py backend/tests/test_lan_command_replay.py \
  backend/tests/fixtures/lan_command_histories/
git commit -m "feat(printers): lan_command, one MQTT command and its reply, on the print worker (#7276)"
```

---

### Task 3: The fake printer answers the new commands

**Files:**
- Modify: `e2e-stack/fake-printer/printer.py` (`FakePrinter._handle` branches, state), `e2e-stack/fake-printer/report.json` (each AMS tray gets `cali_idx` -1 and `k` `0.020000`; `ams.ams_status` 0), the module docstring (each command and its reply)
- Test: `backend/tests/printers/test_fake_printer.py` (new tests), `backend/tests/printers/test_lan_command_fake.py`

**Interfaces:**
- Consumes: Task 1 builders, Task 2 `LanPrinter.request`.
- Produces, in the fake: a K table `{nozzle_diameter: list[dict]}` seeded empty; replies always echo `command` and `sequence_id` with `result: "success"` unless refused:
  - `extrusion_cali_get` → `{"command", "sequence_id", "nozzle_diameter", "filaments": [...]}` (Bambuddy's parse, `_parse_kprofile_entries` L7392-L7438).
  - `extrusion_cali_set` → a row with `cali_idx` -1 gets the next free index; any other replaces that index.
  - `extrusion_cali_del` → removes by `cali_idx` and `filament_id`.
  - `ams_filament_setting` → the tray's fields change in the next report; empty values reset it.
  - `extrusion_cali_sel` → the tray's `cali_idx`.
  - `ams_change_filament` → `ams.tray_now` becomes the target (255 on unload); `ams_status` reads `0x0100` for one report, then 0.
  - `ams_get_rfid` → a reply only.
  - Control: `POST /__refuse` with `{"command": "<name>", "reason": "<text>"}` makes the next such command answer `result: "fail"` with that reason; `POST /__silent` with `{"command": "<name>"}` makes the next one answer nothing. `/__reset` clears both and the K table.

- [ ] **Step 1: Write the failing tests** (`needs_fake_lan`)

```python
async def test_a_k_profile_set_then_get_lists_it(lan) -> None: ...
async def test_a_slot_configure_shows_in_the_next_report(lan) -> None: ...
async def test_a_load_moves_tray_now_and_unload_returns_255(lan) -> None: ...
async def test_a_refused_command_answers_fail_with_the_reason(lan) -> None: ...
async def test_a_silent_command_answers_nothing(lan) -> None: ...
```

`test_lan_command_fake.py` runs `LanActivities.lan_command` (with `LanDeps.connect` pointed at the fake through `tests/support/fake_lan.py`) for one write and one read, and `/__received` shows each command once.

- [ ] **Step 2: Run, expect FAIL**

Run: `cd backend && uv run --frozen pytest tests/printers/test_fake_printer.py tests/printers/test_lan_command_fake.py -q`

- [ ] **Step 3: Implement the branches and the state in `printer.py`**

- [ ] **Step 4: Run, expect PASS**; also `uv run --frozen pytest tests/printers/test_fake_printer.py -q` whole, and the e2e-stack's own checks it documents (`e2e-stack/CLAUDE.md`).

- [ ] **Step 5: Commit**

```bash
git add e2e-stack/fake-printer/ backend/tests/printers/test_fake_printer.py backend/tests/printers/test_lan_command_fake.py
git commit -m "test(fake-printer): K-profile and AMS commands with their replies (#7276)"
```

---

### Task 4: `status_from_report` maps `tray_now`, `ams_status`, `cali_idx` and `k`

**Files:**
- Modify: `backend/scadbuddy/printers/status.py` (`status_from_report`, `_trays`, `_vt_tray`), `backend/scadbuddy/bambuddy/models.py` (`AmsTray.cali_idx: int | None = None`, `AmsTray.k: float | None = None`, numeric validators as `_count`)
- Test: `backend/tests/printers/test_status.py`

**Interfaces:**
- Produces: `PrinterStatus.tray_now` from `print.ams.tray_now` (a numeric string); `PrinterStatus.ams_status_main = (int(print.ams_status) >> 8) & 0xFF` when `ams_status` is a number (Bambuddy L2819-L2831, L3644-L3655), else `None`; each tray's `cali_idx` and `k` from the report's tray object. Cite those lines in the docstring.

- [ ] **Step 1: Write the failing tests**

```python
def test_tray_now_and_the_ams_state(report_h2c: LanReport) -> None:
    status = status_from_report(ROW, with_print(report_h2c, ams={"tray_now": "2"}, ams_status=0x0100), now=NOW)
    assert status.tray_now == 2 and status.ams_status_main == 1
def test_a_report_without_ams_status_says_nothing_of_the_ams(report_h2c) -> None: ...  # None
def test_each_tray_carries_its_cali_idx_and_k(report_h2c) -> None: ...  # cali_idx 3, k 0.025
```

- [ ] **Step 2: Run, expect FAIL** — `cd backend && uv run --frozen pytest tests/printers/test_status.py -q`
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run, expect PASS**; also `uv run --frozen pytest tests/printers tests/filament -q` (capture reads these trays).
- [ ] **Step 5: Commit** — `git commit -m "feat(printers): the LAN status says tray_now, the AMS state and each tray's K (#7276)"`

---

### Task 5: ScadBuddy's tables for K-profile notes and slot presets

**Files:**
- Create: `backend/scadbuddy/migrations/$(date -u +%Y%m%dT%H%MZ)_printer_kprofile_notes_slot_presets.sql`, `backend/scadbuddy/printers/command_data.py`
- Modify: `backend/scadbuddy/printers/store.py` (`PRINTER_REFERENCES`), `backend/scadbuddy/printers/component.py` (expose the store)
- Test: `backend/tests/printers/test_command_data.py`, `backend/tests/printers/test_store.py` (its FK check covers the new tables)

**Interfaces:**
- Tables:
  - `printer_kprofile_notes (printer_id uuid NOT NULL REFERENCES printers (id) ON DELETE CASCADE, note_key text NOT NULL CHECK (length(note_key) BETWEEN 1 AND 256 AND strpos(note_key, '/') = 0), note text NOT NULL CHECK (length(note) BETWEEN 1 AND 2000), updated_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY (printer_id, note_key))`.
  - `printer_slot_presets (printer_id uuid NOT NULL REFERENCES printers (id) ON DELETE CASCADE, ams_id smallint NOT NULL, tray_id smallint NOT NULL, preset_id text NOT NULL CHECK (length(preset_id) BETWEEN 1 AND 64), preset_name text NOT NULL CHECK (length(preset_name) BETWEEN 1 AND 128), preset_source text NOT NULL CHECK (preset_source IN ('cloud','local','builtin','orca_cloud')), tray_info_idx text CHECK (length(tray_info_idx) <= 64), updated_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY (printer_id, ams_id, tray_id))`.
- `PrinterCommandData(pool)` in `command_data.py`: `notes(printer_id: UUID) -> dict[str, str]`; `set_note(printer_id: UUID, key: str, note: str) -> None` (a blank note deletes); `delete_note(printer_id: UUID, key: str) -> None`; `slot_presets(printer_id: UUID) -> dict[tuple[int, int], SavedSlotPreset]`; `set_slot_preset(printer_id: UUID, ams_id: int, tray_id: int, body: SlotPresetBody) -> SavedSlotPreset`; `clear_slot_preset(printer_id: UUID, ams_id: int, tray_id: int) -> None`. `SavedSlotPreset` and `SlotPresetBody` are the models Task 8 moves into `printers/ams_slots.py`; until then import them from `bambuddy/slot_control.py`. On the shared `scadbuddy-rows` pool (`rows/component.py` `ROWS`), not a new pool.
- `PRINTER_REFERENCES`: both tables as a `PrinterReference` with a `repoint` hook that keeps the kept printer's row on a key clash (notes by `note_key`, presets by `(ams_id, tray_id)`) and moves the rest.

- [ ] **Step 1: Write the failing tests** (`requires_postgres`)

```python
def test_a_blank_note_deletes_it(pg_pool) -> None: ...
def test_a_note_key_with_a_slash_is_refused(pg_pool) -> None: ...  # CheckViolation
def test_a_slot_preset_is_one_row_per_slot(pg_pool) -> None: ...
def test_a_printer_delete_takes_its_notes_and_presets(pg_pool) -> None: ...
def test_a_merge_keeps_the_kept_printers_note_on_a_clash(pg_pool) -> None: ...
```

- [ ] **Step 2: Run, expect FAIL** — `cd backend && SCADBUDDY_TEST_DATABASE_URL=… uv run --frozen pytest tests/printers/test_command_data.py tests/printers/test_store.py -q`
- [ ] **Step 3: Implement** the migration (new file only), the store and the hooks.
- [ ] **Step 4: Run, expect PASS**
- [ ] **Step 5: Commit** — `git commit -m "feat(printers): ScadBuddy keeps K-profile notes and slot presets (#7276)"`

---

### Task 6: The LAN-link gate and the stored-report status

**Files:**
- Create: `backend/scadbuddy/printers/native.py`
- Modify: `backend/scadbuddy/printers/component.py` (a `LanCommands` dependency: `client_getter`, `task_queue` = `settings.temporal_task_queue_bambuddy`, as `FilamentSyncNudger` holds them)
- Test: `backend/tests/printers/test_native.py`

**Interfaces:**
- Produces:
  - `NOT_LAN_LINKED = "not_lan_linked"`.
  - `def is_lan_linked(row: PrinterRow) -> bool`: `physical`, `lan_host`, `lan_cert_sha256` and `has_access_code` all set (Task 9 filters by it).
  - `async def lan_row(printers: PrinterStore | None, printer_id: str, *, feature: str) -> PrinterRow`: 404 for no row (or a non-UUID id); 409 `not_lan_linked` for a row that is not `physical`, or has no `lan_host`, no `lan_cert_sha256` or `has_access_code` false. Detail: `f"{row.name} is not linked over the LAN, so ScadBuddy cannot reach its {feature}. Link it in Settings > Printers."` (R-7276-4).
  - `async def live_status(reports: PrinterReportStore, row: PrinterRow, *, now: datetime) -> PrinterStatus`: the stored report's `report_status`; a missing report gives `connected=False` with no trays.
  - `async def external_count(reports: PrinterReportStore, row: PrinterRow) -> int`: the stored report's `vt_tray` length (1 when missing).
  - `LanCommands` (dataclass: `client_getter: Callable[[], Client]`, `task_queue: str`) with `async def run(self, printer_id: str, run: LanCommandRun, *, key: str | None = None) -> LanCommandOutcome` over `execute_lan_commands`; `LanCommandsDep` for routes.

- [ ] **Step 1: Write the failing tests**

```python
async def test_a_bambuddy_only_printer_is_not_lan_linked() -> None:  # 409, code not_lan_linked, "Settings > Printers" in detail
async def test_a_virtual_printer_is_not_lan_linked() -> None:
async def test_a_row_without_its_access_code_is_not_lan_linked() -> None:
async def test_an_unknown_printer_is_404() -> None:
async def test_no_report_reads_as_offline() -> None:
```

- [ ] **Step 2: Run, expect FAIL** — `cd backend && uv run --frozen pytest tests/printers/test_native.py -q`
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run, expect PASS**
- [ ] **Step 5: Commit** — `git commit -m "feat(printers): the LAN-link gate for printer commands (#7276)"`

---

### Task 7: K-profile routes on the LAN

**Files:**
- Create: `backend/scadbuddy/printers/kprofiles.py` (the bodies `KProfileSet`, `KProfileBatch`, `KProfileRemove`, `KProfileNoteSet`, `NoteKey`, `NozzleDiameter` moved from `bambuddy/kprofiles.py` unchanged, plus the LAN functions below)
- Modify: `backend/scadbuddy/api/printer_kprofiles.py`, `backend/scadbuddy/bambuddy/operations.py` (drop `kprofile_kinds`)
- Test: `backend/tests/api/test_printer_kprofiles.py` (rewritten onto the LAN path)

**Interfaces:**
- Consumes: Task 1 `extrusion_cali_get/_set/_del`, `KProfileRow`; Task 2 `LanCommandInput`, `LanCommandRun`; Task 5 `PrinterCommandData`; Task 6 `lan_row`, `live_status`, `LanCommandsDep`.
- Produces in `printers/kprofiles.py`:
  - `def table_of(reply: dict[str, Any], nozzle_diameter: str) -> KProfileTable`: each `filaments` entry to `KProfile` (`slot_id` = `cali_idx`), entries for another nozzle dropped (Bambuddy `_parse_kprofile_entries` L7392-L7438).
  - `def set_rows(rows: Sequence[KProfileSet]) -> list[KProfileRow]`: `cali_idx` -1 for `slot_id` 0, else `slot_id`; a new row with no `setting_id` gets `PF` + 17 random digits (L7718-L7720); `n_coef`, `ams_id`, `tray_id` as the body sends them.
  - `def write_result(reply: LanCommandReply) -> KProfileWriteResult`: `success=True`, `confirmed=reply.confirmed`, `message` empty or "the printer did not acknowledge it in time".
- Routes (paths, response models and tags unchanged):
  - `GET …/kprofiles?nozzle_diameter=` → one `read` command, `LAN_REPLY_WAIT_READ`; 502 `printer_no_answer` when none.
  - `GET …/kprofiles/notes` → `PrinterCommandData.notes`.
  - `POST …/kprofiles`, `…/batch`, `…/delete` → one `write` command each, `LAN_REPLY_WAIT_WRITE`, key = the `Idempotency-Key` header (R-7276-8). Before the delete, the dual-nozzle shape is chosen by `len(live_status(...).nozzles) > 1`. Each first refuses 409 `printer_offline` when the stored report is not fresh.
  - `PUT …/kprofiles/notes`, `DELETE …/notes/{setting_id}` → `PrinterCommandData` writes, answered `KProfileWriteResult(success=True)`; they still accept and ignore `Idempotency-Key` (they are upserts).
  - Every description drops "Bambuddy" and says it reaches the printer over ScadBuddy's LAN link; 409 `not_lan_linked` replaces the Bambuddy 409. The `OPERATION_RESPONSES` and `run_operation` imports go.

- [ ] **Step 1: Write the failing tests** (`tests/api`, Postgres + Temporal; the app's print worker runs in-process; `LanDeps.connect` is a `Scripted` printer through the in-process worker's deps; respx mounts no Bambuddy route)

```python
def test_the_list_is_the_printers_reply(client, scripted) -> None:
    scripted.reply("extrusion_cali_get", filaments=[ROW_04, ROW_06])
    body = client.get(f"/api/v1/printers/{PID}/kprofiles?nozzle_diameter=0.4").json()
    assert [p["slot_id"] for p in body["profiles"]] == [ROW_04["cali_idx"]]
def test_a_set_sends_one_extrusion_cali_set(client, scripted) -> None: ...  # payload rows equal set_rows(body)
def test_a_batch_sends_every_row_in_one_command(client, scripted) -> None: ...
def test_a_resent_key_publishes_once(client, scripted) -> None:
    for _ in range(2):
        client.post(f"/api/v1/printers/{PID}/kprofiles", json=SET, headers={"Idempotency-Key": "k1"})
    assert len(scripted.published) == 1
def test_an_unacknowledged_write_is_unconfirmed(client, scripted) -> None: ...  # 200, confirmed False
def test_a_refused_write_is_409_printer_refused(client, scripted) -> None: ...
def test_notes_live_in_scadbuddy(client) -> None: ...  # PUT, GET, DELETE; scripted.published empty
def test_a_bambuddy_only_printer_is_not_lan_linked(client, bb_only_row) -> None: ...
def test_a_printer_linked_both_ways_never_calls_bambuddy(client, both_row, respx_mock) -> None: ...
```

- [ ] **Step 2: Run, expect FAIL** — `cd backend && SCADBUDDY_TEST_DATABASE_URL=… uv run --frozen pytest tests/api/test_printer_kprofiles.py -q`
- [ ] **Step 3: Implement** the module and the routes; remove `kprofile_kinds` from `bambuddy/operations.py` (`OPERATION_KINDS`) and its import.
- [ ] **Step 4: Run, expect PASS**; also `uv run --frozen pytest tests/test_operation_kinds*.py tests/api/test_operations*.py -q` if they list kinds (grep `printer_kprofile_`) and update any listing test.
- [ ] **Step 5: Commit** — `git commit -m "feat(printers): K-profiles over ScadBuddy's LAN link, notes in ScadBuddy (#7276)"`

---

### Task 8: AMS slot routes on the LAN

**Files:**
- Create: `backend/scadbuddy/printers/ams_slots.py` (from `bambuddy/slot_control.py`: the bodies, views, `refused`, `invalid`, `known_slot`, `global_tray`, `filled`, `ams_busy`, `guard`, `slot_extruder`, `nozzle_of`, `configure_values`; drop `linked_row`, `read_row`, `preset_source_of`)
- Modify: `backend/scadbuddy/api/printer_slots.py`
- Test: `backend/tests/api/test_printer_slots.py` (rewritten onto the LAN path), `backend/tests/printers/test_ams_slots.py` (pure parts)

**Interfaces:**
- Consumes: Task 1 builders and `WireSlot`; Task 2; Task 4 status fields; Task 5 `PrinterCommandData`; Task 6 gate, `live_status`, `external_count`, `LanCommandsDep`.
- Produces in `printers/ams_slots.py`:
  - `guard(status, action, ams_id, tray_id)` as before; its `printer_offline` detail says "ScadBuddy has no fresh report from {name}" (R-7276-12).
  - `SlotIds` (frozen dataclass `tray_info_idx: str`, `setting_id: str | None`) and `def slot_ids(catalogue: Catalogue, slicer_filament: str) -> SlotIds | None`: the preset's Bambu `filament_id` (`PresetChoice.filament_id`) and its setting id (`Catalogue.native_setting_ids`), for a standard ref or an `sb-<id>` ref (`with_own`); `None` when the catalogue has no `filament_id` for it (R-7276-15). The catalogue is `catalogue_for(None, native=profiles)`, read off the event loop.
  - `configure_values(...) -> tuple[SlotParams, SlotConfigured]`: as before, but the spool's preset goes through `slot_ids` (never `for_bambuddy`), and `SlotConfigured.preset_source` is gone from the logic (field kept, always `None`, so the API schema stays).
  - `SlotParams` (moved `SlotConfigure`, renamed; same fields and `digest()`), and `def configure_commands(params: SlotParams, ams_id: int, tray_id: int, *, external_count: int) -> list[LanCommandInput]`: `ams_filament_setting`, then `extrusion_cali_sel` (`cali_idx` or -1), then `extrusion_cali_set_slot` only when `k_value > 0` and `cali_idx < 0`, all `kind="write"`, `LAN_REPLY_WAIT_AMS` (Bambuddy's configure order, `routes/printers.py` L3239-L3291 at the pinned commit).
- Routes (paths and response models unchanged; descriptions drop Bambuddy):
  - `GET /{id}/ams`: the stored report's status (Task 6), `saved_preset` from `PrinterCommandData.slot_presets`, unit `label` = the place's name when it differs from the default `<printer> <unit>` (R-7276-13), `place_id`/`place_name` as before. No command is sent.
  - `configure`, `reset`, `refresh`, `load`, `unload`: `guard` on the stored status, then one `LanCommandRun`. `reset` also clears the slot's preset row. `refresh` sends `ams_get_rfid` only (R-7276-11). `load` sends `extruder_id` only with the Filament Track Switch, else 409 `extruder_unknown` as before. `unload` refuses 409 `not_loaded` when `tray_now` is not that slot's global tray (the 409 Bambuddy used to give). Each answers its model with `message` "sent" or "sent; the printer did not acknowledge it in time".
  - `PUT`/`DELETE …/preset`: `PrinterCommandData`, no printer state read.
  - `PUT`/`DELETE /{id}/ams/{ams_id}/label`: only `filament.locations.name_ams_unit` (R-7276-3), with `place_problem` as before; no serial is read.
  - The routes keep `nudge_after_write` only on the label routes (they write a place); the rest stay in `api/filament.py` `NOT_NUDGED`.

- [ ] **Step 1: Write the failing tests**

```python
def test_the_view_reads_the_stored_report_and_sends_nothing(client, scripted, stored_report) -> None: ...
def test_a_stale_report_refuses_every_write_printer_offline(client, stale_report) -> None:
    for action in ("configure", "reset", "refresh", "load", "unload"):
        r = client.post(f"/api/v1/printers/{PID}/trays/0/1/{action}", json=BODY if action == "configure" else None)
        assert r.status_code == 409 and r.json()["code"] == "printer_offline"
def test_configure_sends_setting_then_cali_sel(client, scripted) -> None: ...  # names in order, ids per wire_slot
def test_configure_with_a_new_k_sends_cali_set_too(client, scripted) -> None: ...
def test_configure_from_a_spool_uses_the_presets_filament_id(client, scripted, spool) -> None: ...  # tray_info_idx == catalogue's filament_id, never the Bambuddy mirror id
def test_reset_clears_the_saved_preset(client, scripted) -> None: ...
def test_refresh_sends_ams_get_rfid_and_no_gcode(client, scripted) -> None: ...
def test_unload_of_a_slot_not_loaded_is_409(client, scripted) -> None: ...
def test_a_label_names_the_place_only(client, respx_mock) -> None: ...
def test_a_bambuddy_only_printer_is_not_lan_linked(client, bb_only_row) -> None: ...
def test_a_printer_linked_both_ways_never_calls_bambuddy(client, both_row, respx_mock) -> None: ...
```

Keep every 422 and state-check test the stopgap had (`known_slot`, `ams_busy`, `tray_empty`, `already_loaded`, `filament_loaded`, `no_nozzle`), now driven by a stored report.

- [ ] **Step 2: Run, expect FAIL** — `cd backend && SCADBUDDY_TEST_DATABASE_URL=… uv run --frozen pytest tests/api/test_printer_slots.py tests/printers/test_ams_slots.py -q`
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run, expect PASS**; also `uv run --frozen pytest tests/api/test_filament*.py -q -k "nudge"` (the `NOT_NUDGED` list).
- [ ] **Step 5: Commit** — `git commit -m "feat(printers): AMS slot control over ScadBuddy's LAN link (#7276)"`

---

### Task 9: The RFID override reconcile configures over the LAN

**Files:**
- Modify: `backend/scadbuddy/filament/rfid_slots.py`, `backend/scadbuddy/spools/activities.py`, `backend/scadbuddy/api/filament.py` (its `reconcile_slots_now` call)
- Test: `backend/tests/filament/test_rfid_slots.py`

**Interfaces:**
- Consumes: Task 6 `LanCommands`, `lan_row` rules; Task 8 `slot_ids`, `SlotParams`, `configure_commands`.
- Produces:
  - `reconcile_slots(store: Filament, commands: LanCommandSender, *, printers: PrinterStore, reports: PrinterReportStore, catalogue: Catalogue) -> m.SlotReport`. `LanCommandSender` is a Protocol with `run(printer_id, run, *, key=None) -> LanCommandOutcome` (`LanCommands` meets it). A printer counts when it passes the LAN-link rules (Task 6, as a predicate `is_lan_linked(row) -> bool` exported from `native.py`) and has a fresh report; `bambuddy_printer_id` no longer matters. `found.for_bambuddy()` is replaced by `slot_ids(catalogue, found.slicer_filament)`; `None` counts `no_preset`. A refused or unanswered command is `failed` with `error_label` (`printer_refused`, `printer_no_answer`, `printer_unreachable`).
  - `reconcile_slots_now(store, load, *, printers, reports, commands: LanCommandSender, profiles: Profiles) -> m.SlotReport | None`: no `bambuddy_url` gate; `None` only when another holds `SLOTS_LOCK`.
  - The docstrings drop Bambuddy's configure route.

- [ ] **Step 1: Write the failing tests** (rewrite the existing ones onto a fake `LanCommandSender` that records runs)

```python
async def test_an_override_slot_is_configured_over_the_lan(...) -> None:  # one run: ams_filament_setting, extrusion_cali_sel
async def test_a_printer_with_no_lan_link_is_skipped(...) -> None:
async def test_a_preset_with_no_filament_id_is_no_preset(...) -> None:
async def test_it_runs_with_no_bambuddy_configured(...) -> None:
async def test_a_refused_configure_is_failed_and_the_next_slot_goes_on(...) -> None:
```

Keep the existing `unchanged`, `no_nozzle`, `no_temps`, stale-row and `RECONFIGURE_WARNING` tests.

- [ ] **Step 2: Run, expect FAIL** — `cd backend && SCADBUDDY_TEST_DATABASE_URL=… uv run --frozen pytest tests/filament/test_rfid_slots.py -q`
- [ ] **Step 3: Implement**; `spools/activities.py` and `api/filament.py` pass a `LanCommands` (the activity's worker holds a Temporal client; use the client its `SpoolRefresh` activities already get, or `connect_lazily` as `api/deps.py` does) and the profiles.
- [ ] **Step 4: Run, expect PASS**; also `uv run --frozen pytest tests/spools tests/test_spool_refresh*.py -q`.
- [ ] **Step 5: Commit** — `git commit -m "feat(filament): an override tag's slot is configured over the LAN, not Bambuddy (#7276)"`

---

### Task 10: Agent tools on the port (#7162) and the slot tools' text

**Files:**
- Create: `agent/src/tools/printerKProfiles.ts`, `agent/test/printerKProfiles.test.ts`
- Modify: `agent/src/tools/printerSlots.ts` (`SOURCE`, descriptions), `agent/src/tools/coverage.ts` (drop the seven K-profile entries from `NOT_A_TOOL`), `agent/src/tools/index.ts`, `agent/src/sessions/touched.ts` (where `printerSlotTools` is listed), `agent/test/printerSlots.test.ts`

**Interfaces:**
- Consumes: the Task 7 routes (generated `schema.d.ts`).
- Produces `printerKProfileTools: Tool[]`, `SOURCE = "ScadBuddy's LAN link to the printer"`:
  - `printer_kprofiles_get` (`read`): `printer_id`, `nozzle_diameter` (enum `0.2`/`0.4`/`0.6`/`0.8`, default `0.4`).
  - `printer_kprofile_notes_get` (`read`).
  - `printer_kprofile_set`, `printer_kprofile_batch_set`, `printer_kprofile_delete` (`outward`): sent through `command()` (`agent/src/tools/command.ts`) with the call's idempotency key, so a lost answer is sent again under the same key and reaches the printer once (Task 7); the description says it is never sent again under a new key, and to read the list before a new try.
  - `printer_kprofile_note_set`, `printer_kprofile_note_delete` (`write`: ScadBuddy's table only).
  - Inputs mirror the backend bodies' bounds (`NOZZLE_ID`, `FILAMENT_ID`, `SETTING_ID`, `N_COEF` patterns, `k_value` a decimal string with at most 6 places, below 10, batch 1-16 of one nozzle).
- `printerSlots.ts`: every "Bambuddy" in text becomes the LAN link; the 409 list adds `not_lan_linked`; `printer_ams_label_set` and `_clear` become `write` (they write a place only, R-7276-13); `printer_slot_preset_set`/`_clear` stay at their tier unless they were `outward` only for Bambuddy, then `write`.

- [ ] **Step 1: Write the failing tests**

```ts
it('lists the K-profiles through the LAN route', async () => { /* GET /api/v1/printers/{id}/kprofiles called with nozzle_diameter */ })
it('sends a set once under the call key', async () => { /* Idempotency-Key header equals ctx key; a resend uses the same key */ })
it('tiers reads read, writes outward, notes write', () => { /* tool.risk per name */ })
it('refuses a k_value with seven places', () => { /* zod parse fails */ })
it('no slot or K-profile tool says Bambuddy', () => { /* description and source */ })
```

- [ ] **Step 2: Run, expect FAIL** — `cd agent && pnpm gen:api && pnpm exec vitest run test/printerKProfiles.test.ts test/printerSlots.test.ts test/coverage.test.ts`
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run, expect PASS**; then `pnpm lint && pnpm typecheck`
- [ ] **Step 5: Commit** — `git commit -m "feat(agent): K-profile tools on the LAN port, slot tools off Bambuddy (#7162, #7276)"`

---

### Task 11: The UI follows the LAN link

**Files:**
- Modify: `frontend/src/components/settings/Printers.tsx` (L234-L235 gate), `PrinterAmsSlots.tsx`, `PrinterKProfiles.tsx`, their `.test.tsx`, `frontend/src/mocks/features/printerAmsSlots.ts`, `printerKProfiles.ts`, `frontend/src/pages/filament/LabelsPage.tsx` if it says Bambuddy for these features
- Load `frontend-design` before the change (owner's rule).

**Interfaces:**
- Gate: `const lanLinked = row.kind === 'physical' && row.lan_host != null && row.has_access_code`; both panels show when `lanLinked`, whatever `bambuddy_printer_id` is.
- Copy: no "Bambuddy" in either panel. A write's `confirmed: false` shows "Sent; the printer did not confirm it." A 409 `not_lan_linked` shows its detail. The label editor says it names ScadBuddy's place.
- Mocks: the msw handlers answer as the new routes do (notes in a mock map; `confirmed` toggled by a mock flag).

- [ ] **Step 1: Write the failing tests**

```tsx
it('shows the panels on a LAN-linked printer with no Bambuddy link', ...)
it('hides them on a Bambuddy-only printer', ...)
it('says when the printer did not confirm a K-profile write', ...)
it('never says Bambuddy', ...)  // queryByText(/Bambuddy/) is null in both panels
```

- [ ] **Step 2: Run, expect FAIL** — `cd frontend && pnpm gen:api && pnpm exec vitest run src/components/settings/PrinterAmsSlots.test.tsx src/components/settings/PrinterKProfiles.test.tsx`
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run, expect PASS**; then `pnpm lint && pnpm typecheck`
- [ ] **Step 5: Commit** — `git commit -m "feat(frontend): K-profiles and AMS slots on a LAN-linked printer (#7276)"`

---

### Task 12: Delete what only the stopgaps used

**Files:**
- Delete: `backend/scadbuddy/bambuddy/kprofiles.py`, `backend/scadbuddy/bambuddy/slot_control.py`, `backend/tests/bambuddy/test_client_kprofiles.py`, `backend/tests/bambuddy/test_client_slots.py`
- Modify: `backend/scadbuddy/bambuddy/client.py` (delete `set_k_profile`, `set_k_profiles_batch`, `delete_k_profile`, `k_profile_notes`, `set_k_profile_note`, `delete_k_profile_note`, `configure_slot`, `reset_slot`, `refresh_slot`, `load_filament`, `unload_filament`, `slot_presets`, `set_slot_preset`, `delete_slot_preset`, `ams_labels`, `set_ams_label`, `delete_ams_label`), `bambuddy/models.py` (delete `SlotPreset`, `SlotControlAnswer`, `SlotConfigure`, the label models; keep `KProfile`, `KProfileTable`, `KProfileWriteResult` and `KProfileNotes`, which the routes and `filament/profile_fetch.py` still use, so the API's schema names do not move), `bambuddy/errors.py` (the scopes only they named), `AmsUnit.serial_number` if nothing else reads it.
- Keep: `BambuddyClient.k_profiles` (R-7276-14: `profile_fetch.py`'s read, out of scope).

- [ ] **Step 1: Write the failing check** — a test in `backend/tests/printers/test_native.py`:

```python
def test_no_filament_hardware_write_goes_through_bambuddy() -> None:
    src = (ROOT / "scadbuddy/bambuddy/client.py").read_text()
    for name in REMOVED:  # the list above
        assert f"def {name}(" not in src
```

- [ ] **Step 2: Run, expect FAIL** — `cd backend && uv run --frozen pytest tests/printers/test_native.py -q -k bambuddy`
- [ ] **Step 3: Delete**; `grep -rn` each name over `backend/ agent/ frontend/ docs/` and fix every reference.
- [ ] **Step 4: Run** `uv run --frozen ruff check . && uv run --frozen mypy && SCADBUDDY_TEST_DATABASE_URL=… uv run --frozen pytest -n 4 tests/bambuddy tests/printers tests/filament tests/api/test_printer_slots.py tests/api/test_printer_kprofiles.py -q` — expect PASS.
- [ ] **Step 5: Commit** — `git commit -m "refactor(bambuddy): drop the K-profile and AMS slot calls the LAN port replaced (#7276)"`

---

### Task 13: Docs

**Files:**
- Modify: `docs/features/filament-parity.md` (rows L19-L20, L108-L119: each cites ScadBuddy's own path, `printers/lan_commands.py`, the route, the LAN activity and the tests; "Via BB as a stopgap" goes), `docs/features/spools-and-filament.md` (the K-profile and AMS slot sections), `backend/CLAUDE.md` (layout: `printers/lan.py` line gains `request` and `lan_commands.py`; the `rfid_slots.py` line says it configures over the LAN; a line for `printers/native.py`, `kprofiles.py`, `ams_slots.py`, `command_data.py`, `workflows/lan_command.py` with its replay fixture and the `Recreate` note), `docs/architecture/` pages that list print-worker workflows or the Bambuddy client's routes (grep `slot_control`, `kprofiles`, `FilamentBbPull`), README's "A release that needs `Recreate`" list (`LanCommand`).
- No counts. Every measured fact says where and how (#259).

- [ ] **Step 1:** `grep -rn "slot_control\|bambuddy/kprofiles\|configure_slot\|through Bambuddy" docs backend/CLAUDE.md README.md` lists every stale line.
- [ ] **Step 2:** Fix each. The parity rows stay MATCH and name the LAN path.
- [ ] **Step 3:** Re-run the grep: no hit describes these features as going through Bambuddy.
- [ ] **Step 4: Commit** — `git commit -m "docs: K-profiles and AMS slots over ScadBuddy's LAN link (#7276)"`

---

### Task 14: Live H2C check (manual; only after the owner's OK)

This writes to a real printer. Do not start it without the owner's written OK on #7276 (R-7276-5). Never script it into CI.

- [ ] **Step 1:** Ask the owner on #7276 for the OK and a window when the H2C is idle with a spare slot. Wait for it.
- [ ] **Step 2:** With the owner's OK, on a deployed build: for each command, send it once through the UI and note the reply (`command`, `sequence_id` echo, `result`) from the print worker's log at debug, and the next report. Check: `n_coef`, `nozzle_id` (`HS00`/`HH00`), the dual-extruder `extruder_id`, `setting_id` on set and delete, reset's empty `ams_filament_setting`, and whether `ams_filament_setting` and `extrusion_cali_sel` echo a non-zero `sequence_id` (R-7276-10).
- [ ] **Step 3:** Record each finding on #7276 with the date, firmware and how it was read; fix any builder that differs in a follow-up PR with its own test; re-date the module docstring's "unverified" note.

---

## Rulings

- **R-7276-8, one path for every printer command.** Every command (K-profile reads and writes, slot commands, the RFID configure) is a `LanCommand` workflow that runs `lan_command` once per command, started and awaited by the API (`execute_lan_commands`). The K-profile writes leave the operations queue (`kprofile_kinds` is removed); their `Idempotency-Key` becomes the workflow id, so a resend attaches to, or reads back, the same execution and the printer gets the command once. — Why: an `Operation`'s run is itself an activity and cannot run `lan_command` (R-7276-1); one path is one set of refusals, timeouts and tests. — Cost if wrong: the K-profile writes lose their `operations` record and the 202 follow; a client that needs them gets a thin `OperationKind` wrapper later, and a resend after Temporal's retention has expired publishes again.
- **R-7276-9, no reply is "sent, unconfirmed", not a failure, for writes.** A write with no matching reply within its wait answers `confirmed: false`; a read with no reply after its tries is 502 `printer_no_answer`; `result: fail` is 409 `printer_refused` with the reason. — Why: Bambuddy reads silence the same way (`await_cali_ack` L7636-L7664, `_send_rfid_command` L8650-L8678); older firmware may not answer. — Cost if wrong: a write the printer dropped reads as sent; the UI says "not confirmed" and the K list or the next report shows the truth.
- **R-7276-10, a fresh `sequence_id` on every command and a strict match.** Every payload gets a fresh decimal `sequence_id`, `ams_filament_setting` and `extrusion_cali_sel` included (Bambuddy sends `"0"` there); a reply matches only on equal `command` and `sequence_id`. AMS commands wait 2 s, K writes 6 s, the K read 4 s twice, all inside a 13 s deadline (Envoy's route timeout is 15 s). — Why: R-7276-1 asks for it, and Bambuddy measured replies at 11 ms (RFID) and 70-150 ms (K writes). — Cost if wrong: if the H2C echoes `"0"` for those two, a configure always reads unconfirmed (Task 14 checks; the fix is a per-command match rule).
- **R-7276-11, refresh sends `ams_get_rfid` only.** No `M620 R` `gcode_line` fallback. — Why: the fallback is for older firmware (Bambuddy L8620-L8640), and a G-code channel is a broader power than this feature needs. — Cost if wrong: a printer on old firmware gets 409 `printer_refused` and cannot refresh from ScadBuddy.
- **R-7276-12, state checks read the stored report.** `guard` and the AMS view use the printer watch's latest report (`PrinterReportStore`, fresh per `is_fresh`); no probe per request. A stale or missing report is 409 `printer_offline`. — Why: the watch already holds the state; a probe per click costs a connection and seconds. — Cost if wrong: a printer whose watch lags reads offline until its next report.
- **R-7276-13, an AMS label is the unit's place name.** The label routes rename the place only (`name_ams_unit`); the view's `label` is the place's name when it differs from the default `<printer> <unit>`. The label tools become `write`. — Why: R-7276-3, and the LAN report has no AMS serial. — Cost if wrong: a person who wants a label different from the place's name cannot have both.
- **R-7276-14, the type profile fill's K read stays on Bambuddy for now.** `filament/profile_fetch.py`'s `client.k_profiles` (a GET of linked printers' K calibrations) is outside R-7276-6's list, so it and `BambuddyClient.k_profiles` stay; it is added to #7276's checklist as a found item for a follow-up on `LanCommands`. — Why: R-7276-6 fixes the scope; it is a read, not a write. — Cost if wrong: one more printer read through Bambuddy until that follow-up.
- **R-7276-15, a slot's `tray_info_idx` is the preset's Bambu filament id.** A spool's preset becomes `tray_info_idx` and `setting_id` through the native catalogue (`PresetChoice.filament_id`, `native_setting_ids`, ScadBuddy's own presets through `with_own`), never Bambuddy's mirror id (`for_bambuddy`). A preset with no filament id is `no_preset`. — Why: the printer knows Bambu's ids, not Bambuddy's local ids; Bambuddy converted them in its route (`routes/printers.py` L3045-L3225 at the pinned commit). — Cost if wrong: a ScadBuddy preset whose document has no `filament_id` cannot configure a slot until it gets one.
- **R-7276-16, nothing is copied from Bambuddy.** K-profile notes, slot presets and AMS labels start empty in ScadBuddy; no read of Bambuddy fills them. — Why: R-7276-3 says nothing is read from Bambuddy for them. — Cost if wrong: notes and labels kept in Bambuddy are lost to ScadBuddy unless the owner wants a one-time import (open question below).

## Open questions for the owner

1. Settled by the controller (2026-11-25, recorded on #7276): no import (R-7276-16 stands). It keeps the port free of new Bambuddy reads; the cost is that a person re-enters a few notes and labels.
2. Still open: the owner's written OK and a window for Task 14's live H2C check (R-7276-5).
3. Settled by the controller: yes, the profile fill's K read moves in a follow-up, #7604 (R-7276-14).

## Pre-flight rulings (controller, 2026-11-25)

The pre-flight scan (`.superpowers/sdd/2026-11-25-filament-hardware-off-bambuddy/preflight.md`) found 21 conflicts. Each ruling below overrides the task text it names. Format: what — why — cost if wrong.

- **P1 (T1, T6, T8):** an external holder's wire ids use `len(status.vt_tray)`, which already prefers `vir_slot`, never a `vt_tray` count from the raw report — the H2C report has no `vt_tray` and two `vir_slot` entries — a wrong count sends a single-holder id to a dual-holder printer.
- **P2 (T7):** a K set sends `tray_id 0`, `ams_id 0` and `n_coef "0.000000"` as Bambuddy does (its #2718), never the body's `-1` — firmware answers `-1` with `fail` while it applies the write, which R-7276-9 would turn into a false 409 — none.
- **P3 (T8):** configure ports the stopgap route's conversions: the `setting_id` back-fill (#2604), the generic `tray_info_idx` for an empty `preset_id`, and separate `preset_setting_id` and `k_setting_id` in `SlotParams` — the slot and the K selection take different setting ids — a slot shows the wrong preset.
- **P4 (T1, T8):** `extrusion_cali_set_slot`'s tray index is defined per unit: an AMS `ams*4+tray`, an HT unit `(ams-128)*4`, an external holder the raw `tray_id`; tests cover AMS 0-3, AMS 4, an HT unit and both external holders — the stopgap route's rule (Bambuddy routes L3281-3286) — a K selection lands on another slot.
- **P5 (T2):** `execute_lan_commands` maps the `_printer` refusals (`ApplicationError` wrapping `PrintRunError`, 409 and 503) to the route's problem with a command's own detail, never "print again" — the print path's text is wrong for a command — none.
- **P6 (T2):** register the activity and workflow in `bambuddy_worker` (`workflows/client.py`), the print worker's builder; there is no `print_worker`; `main.py` needs no change; update the docstring and `docs/architecture/workflows.md` — the plan named a function that does not exist — none.
- **P7 (T3):** the fake printer writes `ams_status` at the top level of `print`, as the H2C fixture has it, which T4 reads — consistency with the real report — none.
- **P8 (T7, T8):** API tests script the printer by monkeypatching `LanPrinter.request` and guard Bambuddy with `tests/support/no_bambuddy.py` `forbid_bambuddy` — no other seam exists; no new fixture module — none.
- **P9 (T2):** the workflow id is `lan-command-<printer>-<kind>-<key>` — a key reused for another command must not return the first command's result — none.
- **P10 (T9):** T9 also changes where `filament_rfid_activity` is built (`main.py` ~2160); `LanCommandSender` lives in `printers/native.py` — the plan left both open — none.
- **P11 (T12):** T12 also removes or rewrites `tests/bambuddy/test_client_rfid.py`'s uses of `SlotConfigure` and `configure_slot` — the suite would fail otherwise — none.
- **P12 (T8, T12):** T8 adds `SlotParams` (in `printers/lan_commands.py`) and re-points `command_data.py`'s imports; T12 deletes `SlotConfigure` — "moved" in T8 conflicted with T12's delete — none.
- **P13 (T10):** each new K-profile tool gets its `TOUCHES_NOTHING` entry in `agent/src/sessions/touched.ts` (or its touched rule), and T10 runs the touched test — the registry test fails otherwise — none.
- **P14 (T6, T11):** the printer view gains a backend-computed `lan_linked` boolean from `is_lan_linked` (host, pinned cert, sealed code), and the UI gate reads it; the frontend never re-derives the rule — one source of truth; T11's gate omitted `lan_cert_sha256` — none.
- **P15 (T12):** no test greps `client.py`'s source; the grep stays a verification step — such a test is scaffolding (repo rule) — none.
- **P16 (T4):** T4's test builds its own report rows in the test file or uses `test_status.py`'s existing helpers; `ROW`, `NOW` and `with_print` do not exist — none.
- **P17 (T5):** drop `preset_source` from `printer_slot_presets` — Bambuddy-only, nothing reads it (R-7276-16) — none.
- **P18 (T8):** T8 fixes the `NOT_NUDGED` comment in `api/filament.py` (~240): the preset routes now write a ScadBuddy row; decide per route whether it nudges (a slot preset is not synced to Bambuddy, so it stays un-nudged) and say so — the comment would become false — none.
- **P19 (T7):** before deploy, check that no `printer_kprofile_*` operation is open (a GET of `/api/v1/operations` or the activity list); the PR body names the check — a removed kind's open row is deferred forever (#4139) — one stuck operation row.
- **P20 (T2, R-7276-10 amended):** each `lan_command` activity's `start_to_close` is the MQTT connect timeout + its reply wait + 2 s; the K read retries as Bambuddy does (3 tries of 5 s; cite the exact lines); the route waits for the workflow up to the sum of its commands' timeouts (a configure's three commands included), not a fixed 13 s — 13 s could not fit three connections — a slow printer makes a route wait longer.
- **P21 (T13):** T13 adds the README note; the clusters PR that rolls `scadbuddy-print` with `Recreate` for the new `LanCommand` workflow type is opened by the controller in eh-homelab/clusters after #7276 merges, before deploy — it is another repo — a roll without it fails the new workflow's tasks until the new build is current.
- **P22 (T1, T8; amends P4):** `cali_set_tray` raises `UnsupportedUnitError` for a non-HT AMS id 4-15; Task 8 maps it to 422 `unsupported_unit`. AMS 0-3, HT units and the external holders keep P4's indexes — P4's formula and Bambuddy's rule disagree there, and Bambuddy's collides with AMS 0, so no safe index is known — cost if wrong: a printer with more than four AMS units cannot set a slot's K value until a live check settles it.
- **P23 (T7):** Task 7 makes a new K-profile's `setting_id` (`PF` + 17 digits, as Bambuddy makes it; cite the line) and passes it to the pure builder — the builder takes it, it does not make it — none.
- **P24 (T1, from the Task 1 review):** every slot builder (`wire_slot`, `extrusion_cali_sel`, the load and unload) raises `UnsupportedUnitError` for a unit outside AMS 0-3, AMS 4-15 (Bambuddy's one-slot branch), HT 128-135 and external 255, and for a `tray_id` outside its unit's range. The AMS Lite (16) and the AMS 3H (32) are refused, since ScadBuddy has no layout for them. `extrusion_cali_set` puts the command's common `nozzle_diameter` in every entry, as Bambuddy's batch does — a silent wrong id configures another slot — cost if wrong: an AMS Lite or AMS 3H owner gets 422 until a later issue adds those layouts.
- **P25 (T1; amends P24):** load and unload refuse a global tray past 15 (AMS 4-15 included) on purpose: Bambuddy's layout maps 16-63 as `(tray // 4, tray % 4)` except the AMS Lite's 24-27 and the AMS 3H's 28-36, so those trays are ambiguous. Load and unload take the printer's external holder count and refuse 255 (Ext-R) with one holder and 254/255 with none. `ams_filament_setting` and `ams_reset` check their `WireSlot` again — a caller-built `WireSlot` must not bypass P24 — cost if wrong: an AMS 4-15 load is refused until a live check settles it.
- **P26 (T8, from the Task 1 review):** Task 8 builds every slot only through `wire_slot` with the report's external holder count (`len(status.vt_tray)`), and passes that count to load and unload; it never constructs a `WireSlot` itself — a hand-built Ext-R form passes on a one-holder printer — none.
- **P27 (T2 report; binds T6-T11):** a command route never waits past 10 s (`LAN_ROUTE_WAIT_S`): `execute_lan_commands` waits up to that long; if the workflow is not done it answers 202 with the command's workflow id, and `GET /api/v1/printers/{id}/commands/{command_id}` (Task 6 adds it) returns the result when done (the same answer shape, or 202 while running). The frontend and the agent tools follow a 202 as they follow an operation's (`command()` in `frontend/src/api/client.ts`, `agent/src/tools/command.ts`) — P20's waits (up to 48 s for a K read) pass Envoy's 15 s route timeout, which would answer 504 before the route mapped the printer's answer — cost if wrong: one more route and a follow loop.
- **P28 (T3):** Task 3's fake-printer tests drive `LanPrinter.request` against the real broker (mosquitto), the only test of it on the wire — Task 2's tests inject the connection — none.
- **P29 (T13):** Task 13 fixes `backend/CLAUDE.md`'s mention of a `print_worker` (the builder is `bambuddy_worker`) — a stale name — none.
- **P30 (T2 review):** a write that may have reached the printer is never reported as a failure. (a) The activity bounds connect, subscribe and publish together by one `MQTT_TIMEOUT`, and its own reply deadline ends 1 s before its `start_to_close`, so it returns before Temporal times it out. (b) Once a write is published, silence, a timeout or a dropped connection answers `confirmed: false`; for a read, a drop after publish counts as silence and is retried. (c) While the run is open after `LAN_ROUTE_WAIT_S`, the route answers 202 with the workflow id (P27), never 502; a run that ended by timeout answers `confirmed: false` for a write and 502 `printer_no_answer` for a read. (d) A certificate mismatch is 409 `printer_cert_changed`, not `not_lan_linked`; a missing LAN key is 503 with code `lan_unavailable`. Task 14 watches the printer's concurrent-client limit — a reported failure for an applied write invites a resend, which writes twice — cost if wrong: a few more unconfirmed answers.
- **P31 (T3, carried from the Task 2 review after its third round):** Task 3 (a) runs the same suppressed force-close (`disconnect()`, then `_sock_close()`) when the `MQTT_TIMEOUT` block cuts a connect inside `aiomqtt.Client.__aenter__` (the client exists but nothing was entered), and (b) its mosquitto test drives a cut close and a cut connect against the real broker, so an aiomqtt bump that renames the private attributes the force-close uses fails a test instead of silently doing nothing — Task 2's tests use stubs only — cost if wrong: a cut connect can hold a printer client slot until aiomqtt cleans it up.
- **P32 (T3 review):** the fake printer's `/__silent` stays "record, apply nothing, answer nothing" (a lost command); the "applied but silent" mode (P30's `confirmed: false` path) is added by the first task whose e2e or API test needs it (Task 7 or 8), as `/__silent?apply=1`. The executor-phase connect leak is https://github.com/eh-homelab/ScadBuddy/issues/7616 — the brief said "answers nothing"; no test needs the other mode yet — none.
- **P33 (T8, from the Task 3 review):** Task 8's tests of a slot K set on an HT unit or an external holder assert the payload sent (the fake's `/__received`, its `tray_id`), never the fake's report: the fake reads the flat index as an AMS slot, as the wire gives no unit (P4), so HT-A or an external holder at index 0 changes AMS 0 slot 0 in the fake. Route and e2e tests make a K profile before they select it — the fake must not know more than a printer is told; the addressing stays open until Task 14 — none.
- **P34 (T5 review):** the migration keeps its `20261125T0700Z` stamp: it is unmerged, sorts after main's newest file, and the ledger applies any missing file in order — a later file stamped between the real time and 0700Z would only run after it — an audit-only oddity.
- **P35 (T5 review; binds T7, T8):** the note and slot-preset routes validate before the store: a note key of 1 to 256 characters with no `/`, a note of at most 2000, and `ams_id`/`tray_id` in the ranges the slot routes already accept, each refused 422, never a `CheckViolation` or `NumericValueOutOfRange` as a 500; a test per refusal — a 500 on bad input.
- **P36 (T6 review; binds T7, T8, T9):** every command route calls `lan_row` before `LanCommands.run` (`run` does not gate), and answers through `native.command_response` (200 with the replies, 202 with the workflow id). Task 9's `LanCommandSender` returns `LanCommandOutcome | LanCommandPending` and counts a pending run as `confirmed: false` (the write may be on its way), never success or failure. `live_status` gives a LAN-only printer with no report `id=0`, so no caller looks a status up by its `id` — a skipped gate or a pending run read as done.
- **P37 (T7 review; binds T7, T8):** the follow route `GET /printers/{id}/commands/{command_id}` answers the shape the route would have answered (P27): a generic view registry keyed by command name maps the started run and its outcome (`extrusion_cali_get` to `table_of`, `extrusion_cali_set`/`_del` to `write_result`; Task 8 registers the slot commands); an unregistered command answers `LanCommandOutcome` — a client never parses firmware JSON — cost: one small registry.
- **P38 (T7 review; binds T7, T8):** a write whose `Idempotency-Key` names a command already started skips the freshness gate (`lan_row` still runs first) and answers that command's result, so a resend after the printer drops off never reads as a failure of an applied write (P30) — cost: one lookup per keyed write.
- **P39 (T7 review):** the docs Task 7 made false (`docs/features/spools-and-filament.md` ~12, ~150-157, ~362-366; `docs/features/filament-parity.md` ~115-119) are fixed in Task 13, before the PR opens; `BambuddyClient.k_profile*` and their tests and the Grafana kind regexes (`deploy/grafana/scadbuddy-printing.json`, `scadbuddy-renders.json`) go in Task 12; one key reused across set and batch with another body is accepted (clients make a key per click) — cost: a doc missed, which the final review checks.
