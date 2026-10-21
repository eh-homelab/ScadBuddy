# Printer Identity and the Native LAN Print Path Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One printer record per physical printer, keyed by serial, that links the Bambuddy printer and ScadBuddy's own LAN connection, so a print run reaches the printer by either path from the same request.

**Architecture:** A new `printers` feature (table, store, component, routes) owns the identity. Bambuddy's `GET /printers/` and `GET /printers/{id}/status` both carry `serial_number` (the recordings redact it), so linking a Bambuddy printer is a lookup, not a measurement. The LAN path is MQTT over TLS on 8883 (`aiomqtt`) for commands and state, and implicit FTPS on 990 (`ftplib.FTP_TLS` with the socket wrapped at connect) for the file. `PrintRunWorkflow` keeps every step up to and including the slice, then branches on the printer's path: Bambuddy's `print_enqueue`, or the new `lan_upload` and `lan_print` activities, all on the existing `bambuddy` queue served by the print worker (`run_print_worker`, `build_print_deps`); no queue is added or renamed. The print worker has no store and no S3 credentials, so `lan_upload` fetches the sliced 3MF through the API's internal blob route the slicer plan defines. One `printer_watch` workflow per LAN printer keeps the latest MQTT report in Postgres, which `/printers/now`, the print dialog and the native follow read in place of Bambuddy's status. This is tier 3a: the registry id reaches the request, the strip, activity attribution, the agent and the Settings page; the tables keyed by the Bambuddy integer stay as they are behind one resolver (see "Deferred to 3b").

**Tech Stack:** Python 3.12, FastAPI, pydantic, psycopg, Temporal (temporalio 1.33), `aiomqtt`, stdlib `ftplib`/`ssl`, React 19, msw, Playwright, the agent's `defineTool` registry.

**Spec:** `docs/superpowers/specs/2026-10-16-own-store-slicer-rust-workers-design.md` §6 (tier 3), §2 (measured facts), §1.3 (anti-goals).

## Global Constraints

- Schema changes are new files in `backend/scadbuddy/migrations/` named `$(date -u +%Y%m%dT%H%MZ)_<slug>.sql`; never edit a merged one (CLAUDE.md "Migrations").
- The access code is sealed with `scadbuddy.core.secrets.seal_secret(kek, plaintext, aad)` and opened with `open_secret`, AAD `printer:<id>:<serial>`; no route returns it, no log or span attribute carries it (spec §6.4).
- A new backend service is a `Component` in `scadbuddy/<feature>/component.py`, found by discovery, never an `AppState` field; routes read it through `component_dep` (CLAUDE.md "Layout").
- A new `scadbuddy.api` module exporting `router` is mounted by `main.py::_api_router` without an edit there; `tests/api/test_routes.py` checks no two routes collide.
- Every new `/api/v1` operation needs an agent tool or a `NOT_A_TOOL` entry in `agent/src/tools/coverage.ts`, or `agent/test/coverage.test.ts` fails.
- A new msw feature is `frontend/src/mocks/features/<feature>.ts` exporting `handlers` (and `reset`), picked up without editing `handlers.ts`.
- A `PrintRunWorkflow` change goes behind `workflow.patched(...)`; `backend/tests/test_print_replay.py` replays `tests/fixtures/print_run_histories/` and must still pass.
- Every LAN fact the plan relies on is cited: MQTT 8883, user `bblp`, password the access code, topics `device/<serial>/report` and `device/<serial>/request`, `pushing.pushall`, `print.project_file` (Doridian/OpenBambuAPI `mqtt.md`, read 2026-10-16); FTPS port 990, implicit TLS, user `bblp` (`ftp.md`, same repo, same date; ha-bambulab `pybambu/bambu_client.py` `ImplicitFTP_TLS`, `ftp.connect(..., port=990)`, `prot_p()`). The upload directory and the `url` the printer accepts for a LAN file are **not** documented there: Task 7 measures them on a printer before Task 9 depends on them.
- Payloads that cross a workflow history carry no secret: the access code is opened inside the activity that uses it, from the row, never passed in an input (as `QueuedPlate` keeps hotend serials out, `print_run.py:1420`).
- The printer's TLS certificate is self-signed, so verification is by pin, never off: the LAN link's probe records the leaf certificate's SHA-256 fingerprint (`lan_cert_sha256`) and every later MQTT and FTPS connection verifies the presented certificate against it (trust on first use, at the moment the user types the access code).
- The print worker reads a sliced blob only through `GET /api/v1/internal/blobs/{key}` with a per-call signed token, as `RemoteOutputs` reads outputs (`bambuddy/output_reader.py:141`); the route is the slicer plan's (`docs/superpowers/plans/2026-10-16-native-slicer-rust-worker.md`), consumed here, never defined here.
- Nothing new on the pod's disk; state is Postgres.
- Backend checks: `uv run --frozen ruff check . && uv run --frozen ruff format --check . && uv run --frozen mypy && uv run --frozen pytest -n auto` in `backend/`. Frontend: `pnpm lint && pnpm typecheck && pnpm test && pnpm build`. Agent: `pnpm lint && pnpm typecheck && pnpm test`.

## Review Focus

1. A Bambuddy printer whose `serial_number` is null or withheld (Bambuddy hides `access_code` from API-keyed callers, `models.py:109`; the serial may be hidden the same way). Expected: `/printers/discover` lists it as not linkable with a reason, and a run naming it prints through Bambuddy as today. Test in Task 1 (`test_printer_without_serial_is_not_linkable`).
2. Two Bambuddy printers reporting the same serial (a re-added printer). Expected: linking the second refuses with 409 naming the row already holding it. Test in Task 3 (`test_link_bambuddy_refuses_second_printer_with_same_serial`).
3. A `PrintRunRequest` naming both `printer` and `printer_id` that disagree. Expected: 422 before anything is planned. Test in Task 8 (`test_run_refuses_disagreeing_printer_fields`).
4. The print worker started without `SCADBUDDY_SECRET_KEY_FILE` while a printer's `preferred_path` is `native`. Expected: the run is refused at `print_check` with a problem saying LAN printing needs the key, never a crash inside `lan_print`. Test in Task 8 (`test_native_run_refused_without_kek`).
5. The MQTT report stops arriving while a native print is followed (printer off, Wi-Fi lost). Expected: `/printers/now` shows the printer `offline` once `seen_at` is older than 90 s, and the follow keeps publishing from the last report until `MAX_AGE`, then settles as `unknown`. Test in Task 6 (`test_stale_report_is_offline`) and Task 9 (`test_native_follow_settles_when_report_stops`).

---

### Task 1: Bambuddy's `Printer` carries its serial and address

**Files:**
- Modify: `backend/scadbuddy/bambuddy/models.py:106-118` (`Printer`), `:472-510` (`PrinterStatus`)
- Test: `backend/tests/bambuddy/test_models.py`

**Interfaces:**
- Produces: `Printer.serial_number: str | None`, `Printer.ip_address: str | None`, `PrinterStatus.serial_number: str | None` (each `None` when absent or empty). Both Bambuddy payloads carry the serial; the recordings redact it (`printers.json:4`, `printer.json:3`, `printer-status*.json`).

- [ ] **Step 1: Write the failing test**

```python
def test_printer_parses_serial_and_address() -> None:
    rows = json.loads(RECORDINGS.joinpath("printers.json").read_text())
    printer = Printer.model_validate(rows[0])
    assert printer.serial_number == "REDACTED-SERIAL"
    assert printer.ip_address == rows[0]["ip_address"]


def test_printer_status_parses_redacted_serial() -> None:
    status = PrinterStatus.model_validate(json.loads(RECORDINGS.joinpath("printer-status.json").read_text()))
    assert status.serial_number is not None


def test_printer_without_serial_is_not_linkable() -> None:
    printer = Printer.model_validate({"id": 3, "name": "x", "serial_number": ""})
    assert printer.serial_number is None
```

- [ ] **Step 2: Run it to make sure it fails**

Run: `cd backend && uv run --frozen pytest tests/bambuddy/test_models.py -k printer -v`
Expected: FAIL, `Printer` has no attribute `serial_number`.

- [ ] **Step 3: Add the fields**

In `Printer` (`models.py:106`), add `serial_number: str | None = None` and `ip_address: str | None = None`; in `PrinterStatus` (`:472`), `serial_number: str | None = None`; each with a `field_validator(..., mode="before")` that maps `""` to `None` (the pattern of `_serial_text` at `:347`). Extend `Printer`'s docstring: Bambuddy withholds `access_code` from API-keyed callers but not the serial, which the status payload carries too.

- [ ] **Step 4: Run the tests**

Run: the Step 2 command. Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/scadbuddy/bambuddy/models.py backend/tests/bambuddy/test_models.py
git commit -m "feat(bambuddy): Printer and PrinterStatus carry the serial"
```

### Task 2: The `printers` table and store

**Files:**
- Create: `backend/scadbuddy/migrations/<ts>_printers.sql`
- Create: `backend/scadbuddy/printers/__init__.py`, `backend/scadbuddy/printers/models.py`, `backend/scadbuddy/printers/store.py`
- Test: `backend/tests/printers/test_store.py` (`requires_postgres`, `pg_conninfo`)

**Interfaces:**
- Produces (`printers/models.py`):
  - `PrintPath = Literal["bambuddy", "native", "auto"]`
  - `class PrinterRow(BaseModel)`: `id: str` (uuid4 text), `serial: str`, `name: str`, `model: str | None`, `bambuddy_printer_id: int | None`, `lan_host: str | None`, `has_access_code: bool`, `preferred_path: PrintPath`, `created_at: datetime`, `updated_at: datetime`.
  - `class PrinterCreate(BaseModel)`: `serial: str = Field(min_length=8, max_length=32, pattern=r"^[0-9A-Z]+$")`, `name: str = Field(min_length=1, max_length=80)`, `model: str | None = None`, `preferred_path: PrintPath = "auto"`.
  - `class PrinterPatch(BaseModel)`: `name: str | None`, `preferred_path: PrintPath | None`.
  - `class LanLink(BaseModel)`: `host: str = Field(min_length=1, max_length=253)`, `access_code: str = Field(min_length=1, max_length=128)` (any format: the printer decides, #4434; first written as 8 digits).
  - `PrinterRow.lan_cert_sha256: str | None` (the pinned leaf certificate's fingerprint, hex).
- Produces (`printers/store.py`): `class PrinterStore` over `ConnectionPool[Connection[DictRow]]` and `Kek | None`:
  - `create(body: PrinterCreate) -> PrinterRow` (raises `SerialTakenError(existing_id)` on a duplicate serial)
  - `get(printer_id: str) -> PrinterRow | None`, `by_serial(serial: str) -> PrinterRow | None`, `by_bambuddy_id(printer_id: int) -> PrinterRow | None`, `list() -> list[PrinterRow]` (by name)
  - `patch(printer_id: str, body: PrinterPatch) -> PrinterRow`
  - `link_bambuddy(printer_id: str, bambuddy_printer_id: int) -> PrinterRow` (raises `BambuddyLinkTakenError(existing_id)` when another row holds that id)
  - `unlink_bambuddy(printer_id: str) -> PrinterRow`
  - `link_lan(printer_id: str, body: LanLink, *, cert_sha256: str) -> PrinterRow` (raises `KekMissingError` when `kek is None`)
  - `unlink_lan(printer_id: str) -> PrinterRow`
  - `open_access_code(printer_id: str) -> str` (raises `KekMissingError`, `NoAccessCodeError`; the only read of the sealed column)
  - `ensure_bambuddy(bambuddy_printer_id: int, *, serial: str | None, name: str, model: str | None) -> PrinterRow`: the row linking that id, else by serial (and links it), else a new row with `preferred_path="bambuddy"`; with `serial=None` a row keyed by the synthetic serial `bambuddy-<id>` (so an unlinked Bambuddy printer still gets exactly one row).
  - `delete(printer_id: str) -> None`
  - `resolve_bambuddy_id(printer_id: str | int | None) -> int | None`: the Bambuddy integer behind a registry id (or an integer passed through, or `None`). The one way the tables still keyed by the Bambuddy integer are reached from a registry id (see "Deferred to 3b").

- [ ] **Step 1: Write the migration**

`printers`: `id text primary key`, `serial text not null unique`, `name text not null`, `model text`, `bambuddy_printer_id integer unique`, `lan_host text`, `lan_cert_sha256 text`, `access_code_sealed bytea`, `access_code_dek_sealed bytea`, `access_code_kek_id text`, `preferred_path text not null default 'auto' check (preferred_path in ('bambuddy','native','auto'))`, `created_at timestamptz not null default now()`, `updated_at timestamptz not null default now()`.
`printer_reports`: `printer_id text primary key references printers(id) on delete cascade`, `report jsonb not null`, `seen_at timestamptz not null`.

- [ ] **Step 2: Write the failing tests**

```python
def test_create_and_lookup_by_serial(printers: PrinterStore) -> None: ...
def test_duplicate_serial_refused(printers: PrinterStore) -> None:
    printers.create(PrinterCreate(serial="01P00A000000001", name="A"))
    with pytest.raises(SerialTakenError): printers.create(PrinterCreate(serial="01P00A000000001", name="B"))
def test_lan_link_seals_and_opens(printers_with_kek: PrinterStore) -> None:
    row = printers_with_kek.link_lan(pid, LanLink(host="10.0.0.9", access_code="12345678"), cert_sha256="ab" * 32)
    assert row.has_access_code and "12345678" not in repr(row)
    assert printers_with_kek.open_access_code(pid) == "12345678"
def test_lan_link_without_kek_refused(printers: PrinterStore) -> None: ...
def test_opened_code_bound_to_row(printers_with_kek, pool) -> None:
    # copy the sealed columns onto another row; open_access_code there raises SealError
def test_ensure_bambuddy_is_idempotent_and_adopts_serial(printers) -> None:
    a = printers.ensure_bambuddy(4, serial=None, name="H2C", model="H2C")
    b = printers.ensure_bambuddy(4, serial="01P00A000000001", name="H2C", model="H2C")
    assert a.id == b.id and b.serial == "01P00A000000001"
```

- [ ] **Step 3: Run them to make sure they fail**

Run: `cd backend && uv run --frozen pytest tests/printers/test_store.py -v`
Expected: FAIL, `scadbuddy.printers` not found.

- [ ] **Step 4: Implement the store**

Sealing: `seal_secret(kek, code, f"printer:{id}:{serial}")` → the three columns from `Envelope`; opening builds the `Envelope` back and calls `open_secret` with the same AAD. `ensure_bambuddy` runs in one transaction with `SELECT … FOR UPDATE` on the id and serial lookups.

- [ ] **Step 5: Run the tests**

Run: the Step 3 command. Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add backend/scadbuddy/migrations backend/scadbuddy/printers backend/tests/printers
git commit -m "feat(printers): the printers registry table and store"
```

### Task 3: The component and the registry routes

**Files:**
- Create: `backend/scadbuddy/printers/component.py`, `backend/scadbuddy/api/printers.py`
- Test: `backend/tests/api/test_printers.py`

**Interfaces:**
- Consumes: `PrinterStore` (Task 2), `BambuddyClient.printers()` (`bambuddy/client.py:320`), `Printer.serial_number` (Task 1).
- Produces: `PRINTERS: Component[Printers]`, `PrintersDep = Annotated[Printers, Depends(component_dep(PRINTERS))]`, where `Printers` holds `store: PrinterStore` and `kek: Kek | None` (from `core.settings.secret_key_file` through `secrets.load_kek`).
- Routes (all under `/api/v1`, `response_model=PrinterRow` unless said):
  - `GET /printers` → `list[PrinterRow]`
  - `POST /printers` (`PrinterCreate`) → 201; 409 `SerialTakenError`
  - `GET /printers/{printer_id}`; 404
  - `PATCH /printers/{printer_id}` (`PrinterPatch`)
  - `DELETE /printers/{printer_id}` → 204
  - `PUT /printers/{printer_id}/bambuddy` body `{"bambuddy_printer_id": int}`; 409 on a taken id; 422 when the Bambuddy printer's serial is known and differs from the row's serial
  - `DELETE /printers/{printer_id}/bambuddy`
  - `PUT /printers/{printer_id}/lan` (`LanLink`) → verifies by `LanPrinter.probe` (Task 4) within 10 s, which also returns the certificate fingerprint the row then pins; 502 with the probe's reason on failure; 503 `KekMissingError` ("LAN printing needs SCADBUDDY_SECRET_KEY_FILE")
  - `DELETE /printers/{printer_id}/lan`
  - `GET /printers/discover` → `list[DiscoveredPrinter]`: every active Bambuddy printer with `bambuddy_printer_id`, `name`, `model`, `serial: str | None`, `linked_to: str | None` (the row's id), `linkable: bool`, `reason: str | None` ("Bambuddy did not report a serial" when `serial is None`). Empty list, not an error, when no Bambuddy is configured.

- [ ] **Step 1: Write the failing tests** (against the api `client` fixture; Bambuddy by the `respx`-style fake the other api tests use, e.g. `tests/api/test_print_choices.py`)

```python
def test_create_list_get_patch_delete(client) -> None: ...
def test_link_bambuddy_refuses_second_printer_with_same_serial(client, bambuddy) -> None: ...
def test_link_bambuddy_refuses_serial_mismatch(client, bambuddy) -> None: ...
def test_discover_marks_linked_and_unlinkable(client, bambuddy) -> None: ...
def test_lan_link_never_returns_code(client, lan_probe_ok) -> None:
    body = client.put(f"/api/v1/printers/{pid}/lan", json={"host": "10.0.0.9", "access_code": "12345678"}).json()
    assert body["has_access_code"] is True and "access_code" not in body
def test_lan_link_probe_failure_is_502(client, lan_probe_fails) -> None: ...
```

- [ ] **Step 2: Run them to make sure they fail**

Run: `cd backend && uv run --frozen pytest tests/api/test_printers.py -v`
Expected: FAIL, 404 on every route.

- [ ] **Step 3: Implement the component and routes**

The probe is injected on `Printers` as `probe: Callable[[str, str, str], Awaitable[tuple[LanReport, str]]]` (the report and the certificate's SHA-256) so the tests replace it on the state (`state.components[PRINTERS].probe = …`), as kinds read state when they run.

- [ ] **Step 4: Run the tests**

Run: the Step 2 command, then `uv run --frozen pytest tests/api/test_routes.py`. Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/scadbuddy/printers/component.py backend/scadbuddy/api/printers.py backend/tests/api/test_printers.py
git commit -m "feat(printers): registry routes, Bambuddy and LAN linking"
```

### Task 4: The LAN client

**Files:**
- Create: `backend/scadbuddy/printers/lan.py`, `backend/scadbuddy/printers/lan_models.py`
- Modify: `backend/pyproject.toml` (add `aiomqtt>=2.3,<3`), `backend/uv.lock` (with the image's uv version, see memory `uv-lock-with-image-uv`)
- Test: `backend/tests/printers/test_lan.py`, `backend/tests/printers/test_lan_live.py` (marker `requires_printer`)

**Interfaces:**
- Produces (`lan_models.py`):
  - `class LanReport(BaseModel)`: `serial: str`, `received_at: datetime`, `print: dict[str, Any]` (the `print` object of a report, merged over the previous one: P1-series send deltas, `mqtt.md`).
  - `class ProjectFile(BaseModel)`: `url: str`, `param: str`, `subtask_name: str`, `use_ams: bool`, `ams_mapping: list[int]`, `bed_type: str = "auto"`, `flow_cali: bool = False`, `timelapse: bool = False`, `layer_inspect: bool = True`, `md5: str`.
- Produces (`lan.py`):
  - `def pushall_command() -> dict[str, Any]` → `{"pushing": {"sequence_id": "0", "command": "pushall"}}`
  - `def project_file_command(file: ProjectFile, sequence_id: str) -> dict[str, Any]` → `{"print": {"command": "project_file", "sequence_id": …, "url", "param", "subtask_name", "use_ams", "ams_mapping", "bed_type", "flow_cali", "timelapse", "layer_inspect", "md5", "project_id": "0", "profile_id": "0", "task_id": "0", "subtask_id": "0"}}` (field names from `mqtt.md`).
  - `def merge_report(previous: dict[str, Any] | None, delta: dict[str, Any]) -> dict[str, Any]` (shallow-merge of `print`; `ams` replaced whole when present).
  - `def fetch_cert_sha256(host: str, port: int = 8883, timeout: float = 5.0) -> str`: the SHA-256 of the DER leaf certificate the printer presents (`ssl.get_server_certificate` with verification off for this one read, then `hashlib.sha256`). Used by the link probe only.
  - `def pinned_context(cert_sha256: str) -> ssl.SSLContext`: `CERT_NONE` and `check_hostname=False` at the TLS layer (the certificate is self-signed and names no host), with the fingerprint checked right after the handshake (`getpeercert(binary_form=True)`); a mismatch closes the connection and raises `CertificateMismatchError`. Every MQTT and FTPS connection of `LanPrinter` goes through it; nothing connects unverified.
  - `class LanPrinter`: `__init__(self, host: str, serial: str, access_code: str, cert_sha256: str)`.
    - `async probe(self, timeout: float = 10.0) -> LanReport`: connect, subscribe `device/<serial>/report`, publish `pushall_command()` to `device/<serial>/request`, return the first report. The link route calls `fetch_cert_sha256` first and builds the client with it, so the probe is pinned too.
    - `async reports(self) -> AsyncIterator[LanReport]`: connect, subscribe, publish pushall, yield each merged report; reconnects are the caller's (the activity retries).
    - `async publish(self, command: dict[str, Any]) -> None`.
    - `async upload(self, data: BinaryIO, remote_name: str) -> str`: implicit FTPS to `host:990` as `bblp`, `prot_p()`, `STOR <remote_name>` in the directory Task 7 records, run in `asyncio.to_thread`; returns the md5 hex of what was sent.
  - `class ImplicitFtpTls(ftplib.FTP_TLS)`: wraps the socket in the pinned context at connect (ha-bambulab's `ImplicitFTP_TLS` does the wrapping; the pin check is ours) and checks the fingerprint on the control and every data connection.
- `requires_printer`: skips unless `SCADBUDDY_TEST_PRINTER_HOST`, `_SERIAL` and `_ACCESS_CODE` are set (added to `backend/tests/conftest.py` beside `requires_openscad`).

- [ ] **Step 1: Write the failing unit tests**

```python
def test_project_file_command_fields() -> None:
    cmd = project_file_command(ProjectFile(url="file:///sdcard/x.3mf", param="Metadata/plate_1.gcode", subtask_name="x", use_ams=True, ams_mapping=[0, -1], md5="abc"), "7")
    assert cmd["print"]["command"] == "project_file" and cmd["print"]["ams_mapping"] == [0, -1]
    assert cmd["print"]["bed_type"] == "auto" and cmd["print"]["project_id"] == "0"
def test_merge_report_keeps_unmentioned_keys_and_replaces_ams() -> None: ...
def test_implicit_ftp_tls_wraps_socket_once(monkeypatch) -> None: ...
def test_pinned_context_refuses_other_cert(tls_server_pair) -> None:
    # a local TLS server with cert A; a context pinned to cert B's sha256 raises CertificateMismatchError
```

- [ ] **Step 2: Run them to make sure they fail**

Run: `cd backend && uv run --frozen pytest tests/printers/test_lan.py -v`
Expected: FAIL, module not found.

- [ ] **Step 3: Add `aiomqtt` and implement**

`uv add "aiomqtt>=2.3,<3"` with the Dockerfile's uv. MQTT: `aiomqtt.Client(host, 8883, username="bblp", password=access_code, tls_context=pinned_context(cert_sha256))`; the fingerprint check runs in a connect hook before the first publish.

- [ ] **Step 4: Write the live test** (skipped in CI)

```python
@pytest.mark.requires_printer
async def test_probe_returns_gcode_state(printer_env) -> None:
    report = await LanPrinter(**printer_env, cert_sha256=fetch_cert_sha256(printer_env["host"])).probe()
    assert "gcode_state" in report.print
```

- [ ] **Step 5: Run the unit tests, and the live one against a printer**

Run: `uv run --frozen pytest tests/printers -v`, then with the three variables set. Expected: unit PASS; live PASS, or a recorded reason in the PR.

- [ ] **Step 6: Commit**

```bash
git add backend/pyproject.toml backend/uv.lock backend/scadbuddy/printers/lan.py backend/scadbuddy/printers/lan_models.py backend/tests/printers backend/tests/conftest.py
git commit -m "feat(printers): LAN client over MQTT and implicit FTPS"
```

### Task 5: `PrinterStatus` from a LAN report

**Files:**
- Create: `backend/scadbuddy/printers/status.py`
- Create: `backend/tests/printers/fixtures/lan-report-h2c.json` (a report recorded from the Task 4 live probe, serial and access code redacted)
- Test: `backend/tests/printers/test_status.py`

**Interfaces:**
- Consumes: `PrinterStatus`, `AmsUnit`, `AmsTray`, `NozzleInfo`, `NozzleRackSlot`, `HmsError`, `FilaSwitch` (`bambuddy/models.py:389-510`).
- Produces: `def status_from_report(row: PrinterRow, report: LanReport, *, now: datetime) -> PrinterStatus`, with `id=0`-free semantics: `PrinterStatus.id` is `row.bambuddy_printer_id or 0`, `name=row.name`, `connected = now - report.received_at < STALE_AFTER` (`STALE_AFTER = timedelta(seconds=90)`), `state=print["gcode_state"]`, `ams` from `print["ams"]["ams"][]` (`id`, `humidity`, `temp`, `tray[]` with `id`, `tray_color`, `tray_type`, `tray_sub_brands`, `tray_id_name`, `tray_info_idx`, `remain`, `nozzle_temp_min/max`, `state`), `vt_tray` from `print["vt_tray"]`, `progress=print["mc_percent"]`, `remaining_time=print["mc_remaining_time"]`, `layer_num`, `total_layers=print["total_layer_num"]`, `subtask_name`, `hms` from `print["hms"][]` (`attr`, `code` → `HmsError(code=f"{attr:08X}{code:08X}", full_code=…, description=None, severity=(code >> 16) & 0xF)`), `nozzles` from `print["device"]["extruder"]["info"][]` when present, `nozzle_rack` from `print["device"]["nozzle"]` when present. Field names are the `mqtt.md` ones; a field the recorded report lacks is left at its default and listed in the module docstring as "not in the H2C report of 2026-MM-DD".

- [ ] **Step 1: Write the failing tests**

```python
def test_status_maps_trays_and_progress(report_h2c) -> None: ...
def test_stale_report_is_offline(report_h2c) -> None:
    status = status_from_report(row, report_h2c, now=report_h2c.received_at + timedelta(seconds=91))
    assert status.connected is False
def test_hms_code_formatting() -> None: ...
```

- [ ] **Step 2: Run them to make sure they fail**

Run: `cd backend && uv run --frozen pytest tests/printers/test_status.py -v`
Expected: FAIL, module not found.

- [ ] **Step 3: Implement `status_from_report`**

- [ ] **Step 4: Run the tests**

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/scadbuddy/printers/status.py backend/tests/printers
git commit -m "feat(printers): PrinterStatus from a LAN report"
```

### Task 6: `printer_watch` and the registry-backed `/printers/now`

**Files:**
- Create: `backend/scadbuddy/workflows/printer_watch.py`, `backend/scadbuddy/printers/watch.py` (the activity), `backend/scadbuddy/printers/reports.py` (`PrinterReportStore`)
- Modify: `backend/scadbuddy/core/events.py:156-170,320-341` (add `PrinterEvent`), `backend/scadbuddy/api/realtime.py:158-200` (`PrinterEvent` → `["printers", f"printer:{event.printer_id}"]`; `"printers"` into `COLLECTION_TOPICS`), `backend/scadbuddy/bambuddy/printers_now.py:95-160`, `backend/scadbuddy/api/printing.py:536-550`, `backend/scadbuddy/workflows/housekeeping.py:89-129` (a `PRINTER_WATCHES_SWEEP` in `SWEEPS`, behind `patched("housekeeping-printer-watches")`), `backend/scadbuddy/worker.py:765-875` (`PrintWorkerDeps` gains `printers: PrinterStore`, `reports: PrinterReportStore`, `kek: Kek | None`; the watch activity registered on the follow queue), `frontend/src/lib/realtime.ts:8`
- Test: `backend/tests/test_printer_watch_workflow.py` (`requires_temporal`), `backend/tests/api/test_printers_now.py`

**Interfaces:**
- Produces: `class PrinterReportStore`: `put(printer_id: str, report: dict[str, Any], seen_at: datetime) -> None`, `get(printer_id: str) -> tuple[dict[str, Any], datetime] | None`.
- `class PrinterEvent(BaseEvent)`: `kind: Literal["printer.report", "printer.changed"]`, `printer_id: str`. `printer.changed` is emitted by every Task 3 write.
- Workflow `PrinterWatch` (`PRINTER_WATCH_WORKFLOW = "PrinterWatch"`, id `printer-<serial>`), on the `bambuddy` queue, its activity on `follow_queue(task_queue)` as `FollowPrint`'s is (`workflows/follow.py:66-70`), both served by the print worker: one activity `printer_watch(PrinterWatchInput(printer_id: str))`, `heartbeat_timeout=30 s`, `start_to_close_timeout=24 h`, `retry_policy=FOLLOW_RETRY`; the activity opens the code, iterates `LanPrinter.reports()`, writes each merged report through `PrinterReportStore.put` (at most once a second) and `emit(events, PrinterEvent(kind="printer.report", printer_id=…))` only when `gcode_state`, `mc_percent`, `layer_num` or any tray changed. The workflow continues as new every 24 h or on `is_continue_as_new_suggested()`. A `stop` signal ends it (the LAN unlink route sends it). `ensure_watch(client, task_queue, printer_id, serial)` starts it with `WorkflowIDReusePolicy.ALLOW_DUPLICATE`, ignoring `WorkflowAlreadyStartedError`, as `follow()` does (`follow.py:134`).
- The sweep `housekeeping_printer_watches` calls `ensure_watch` for every row with `lan_host` set.
- `printers_now(client: BambuddyClient | None, links: PrintLinkStore, printers: PrinterStore, reports: PrinterReportStore, *, now: datetime) -> PrintersNow`: one card per registry row (plus, while unlinked Bambuddy printers exist, one per such printer through `ensure_bambuddy`, so the strip never loses a printer). A card's status is `status_from_report` when the row has a LAN link and a report fresher than `STALE_AFTER`, else Bambuddy's `printer_status` when the row links one and `client` is not `None`, else `activity="offline"`: the LAN report first, so a printer with both links costs no Bambuddy call (`docs/superpowers/plans/2026-10-18-hot-path-without-bambuddy.md` D4, the user's decision of 2026-10-18). `PrinterNow` gains `id: str`, `serial: str`, `path: Literal["bambuddy", "native", "none"]` (which link answered); `printer_id: int | None` keeps the Bambuddy id.

- [ ] **Step 1: Write the failing tests**

```python
async def test_watch_writes_reports_and_emits_on_change(temporal_env, fake_lan) -> None: ...
async def test_watch_stops_on_signal(temporal_env, fake_lan) -> None: ...
def test_now_prefers_the_lan_report_when_both_links_are_set(client, bambuddy, registry, reports) -> None: ...
def test_now_reads_bambuddy_for_a_bambuddy_only_printer(client, bambuddy, registry) -> None: ...
def test_now_uses_report_for_lan_only_printer(client, registry, reports) -> None: ...
def test_now_without_bambuddy_configured_still_lists_lan_printers(client, registry, reports) -> None: ...
```

`fake_lan` replaces `LanPrinter.reports` with an async generator over a list of deltas.

- [ ] **Step 2: Run them to make sure they fail**

Run: `cd backend && uv run --frozen pytest tests/test_printer_watch_workflow.py tests/api/test_printers_now.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement**

- [ ] **Step 4: Run the tests, then the existing `/printers/now` tests**

Run: Step 2's command, then `uv run --frozen pytest tests/api/test_print.py -k now -v`. Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/scadbuddy backend/tests frontend/src/lib/realtime.ts
git commit -m "feat(printers): printer_watch keeps each LAN printer's report; /printers/now reads the registry"
```

### Task 7: Measure the LAN upload directory and file URL

**Files:**
- Modify: `backend/tests/printers/test_lan_live.py`
- Modify: `docs/superpowers/specs/2026-10-16-own-store-slicer-rust-workers-design.md` §2 (the row on file delivery)

**Interfaces:**
- Produces two constants in `printers/lan.py`, set from the measurement: `UPLOAD_DIR` (the FTPS directory a 3MF goes in) and `def file_url(remote_name: str) -> str` (the `url` the printer accepts in `print.project_file`, e.g. `file:///sdcard/<name>` or `file:///mnt/sdcard/<name>`).

- [ ] **Step 1: Write the live test**

```python
@pytest.mark.requires_printer
async def test_upload_then_project_file_is_accepted(printer_env, tiny_sliced_3mf) -> None:
    lan = LanPrinter(**printer_env)
    md5 = await lan.upload(tiny_sliced_3mf, "scadbuddy-probe.3mf")
    await lan.publish(project_file_command(ProjectFile(url=file_url("scadbuddy-probe.3mf"), param="Metadata/plate_1.gcode", subtask_name="scadbuddy-probe", use_ams=False, ams_mapping=[], md5=md5), "1"))
    report = await anext(aiter(lan.reports()))
    assert report.print.get("print_error", 0) == 0
```

The test is run by hand against a printer with an empty bed and cancelled at once (the test publishes `{"print": {"command": "stop"}}` in its teardown).

- [ ] **Step 2: Run it against a printer with the candidates**

Try `UPLOAD_DIR="/"` with `file:///sdcard/<name>`, then `/cache/` with `file:///sdcard/cache/<name>`. Record which the printer accepted (and the firmware version) in the test's module docstring and the spec's §2 table.

- [ ] **Step 3: Set the constants and commit**

```bash
git add backend/scadbuddy/printers/lan.py backend/tests/printers/test_lan_live.py docs/superpowers/specs/2026-10-16-own-store-slicer-rust-workers-design.md
git commit -m "feat(printers): measured LAN upload directory and file url"
```

### Task 8: The run names a registry printer and chooses its path

**Files:**
- Modify: `backend/scadbuddy/bambuddy/print_run.py:169` (`PrintRunRequest`), `:1228-1340` (`prepare_run`), `:1399-1418` (`PlannedRun`), `:1552-1765` (`plan_run`) (line numbers re-read on main `7613580bf`, 2026-10-18)
- Modify: `backend/scadbuddy/workflows/print_activities.py:114` (`PrintDeps` gains `printers: PrinterStore | None`, `kek: Kek | None`), `:207-249` (`print_check`)
- Modify: `backend/scadbuddy/api/printing.py:766` (`accept_run`), `backend/scadbuddy/api/library_print.py:1173` (`post_library_run`)
- Test: `backend/tests/test_print_models.py`, `backend/tests/api/test_print.py`

**Interfaces:**
- `PrintRunRequest.printer: str | None = None` (registry id); `printer_id: int | None` stays. A `model_validator` refuses both set at once unless the registry row for `printer` links that `printer_id` (checked in `prepare_run`, 422 `disagreeing printer`); the request key (`run_key`) includes `printer` so a change of printer is a new run.
- `prepare_run` resolves the row: from `printer`, else `ensure_bambuddy(printer_id, serial=…, name=…, model=…)` for the Bambuddy printer it already reads (`_require_resolvable_printer`, `print_run.py:1907`, called from `prepare_run`), else the configured default Bambuddy printer the same way. It decides `path: Literal["bambuddy", "native"]`: `preferred_path` as set; `auto` → `native` when `lan_host` is set and the row's report is fresher than `STALE_AFTER`, else `bambuddy`. `native` with `kek is None` → `PrintRunError` detail "LAN printing needs SCADBUDDY_SECRET_KEY_FILE on the print worker" (Review Focus 4). `native` → `PrinterStatus` from the report (Task 5) feeds `with_trays`, `tray_colours`, the nozzle plan and the rack pick, whether or not the row also links a Bambuddy printer; Bambuddy's `printer_status` is read only on the `bambuddy` path (hot-path plan D4).
- `PlannedRun.printer: str` (registry id), `PlannedRun.path: Literal["bambuddy", "native"]`; `printer_id: int` stays, `0` when the row has no Bambuddy link. Every read of a table keyed by the Bambuddy integer (`settings.printer_id`, `rack_nozzle_*`, remembered bed types and choices, project print targets) goes through `PrinterStore.resolve_bambuddy_id` and is otherwise untouched.
- The route attributes the run with the registry id: `attribute_request(activity.index, f"printer:{row.id}")` beside today's source attribution, so the `activity` row names the printer by identity, not by path.

- [ ] **Step 1: Write the failing tests**

```python
def test_run_refuses_disagreeing_printer_fields(client, registry, bambuddy) -> None: ...
def test_run_with_bambuddy_id_creates_registry_row(client, bambuddy) -> None: ...
def test_auto_path_picks_native_when_report_fresh(registry, reports) -> None: ...
def test_native_run_refused_without_kek(client, registry_native, print_deps_without_kek) -> None: ...
```

- [ ] **Step 2: Run them to make sure they fail**

Run: `cd backend && uv run --frozen pytest tests/test_print_models.py tests/api/test_print.py -k "printer or path or kek" -v`
Expected: FAIL.

- [ ] **Step 3: Implement**

- [ ] **Step 4: Run the print test modules and the replay**

Run: `uv run --frozen pytest tests/api/test_print.py tests/api/test_print_library.py tests/test_print_replay.py -n auto`. Expected: PASS (nothing in the workflow changed yet; `PlannedRun`'s new fields have defaults so old histories still decode: `printer: str = ""`, `path = "bambuddy"`).

- [ ] **Step 5: Commit**

```bash
git add backend/scadbuddy backend/tests
git commit -m "feat(print): a run names a registry printer and decides its path"
```

### Task 9: The native branch of `PrintRunWorkflow` and its follow

**Files:**
- Modify: `backend/scadbuddy/workflows/printing.py:377-543` (`_print`), `backend/scadbuddy/workflows/print_models.py` (`QueueOutcome.lan: LanStarted | None = None`, new inputs), `backend/scadbuddy/workflows/print_activities.py` (two activities), `backend/scadbuddy/bambuddy/follow.py`, `backend/scadbuddy/bambuddy/progress.py:257-300,443-474`, `backend/scadbuddy/bambuddy/print_run.py:1420-1428` (`QueuedPlate`)
- Create: `backend/scadbuddy/printers/print_activities.py`
- Test: `backend/tests/test_print_native_workflow.py` (`requires_temporal`), `backend/tests/test_print_replay.py` (unchanged, must pass), `backend/tests/printers/test_native_progress.py`

**Interfaces:**
- `NATIVE_PATCH = "native-print"`.
- `class SlicedSource(BaseModel)`: `library_file_id: int | None = None`, `blob_key: str | None = None` (exactly one set; the blob form is the native slicer's, plan `2026-10-16-native-slicer-rust-worker.md`; until it lands every run sets `library_file_id`).
- Consumes: `GET /api/v1/internal/blobs/{key}` with a per-call signed token (the slicer plan's route) as `RemoteBlobs(api_internal_url)` in `bambuddy/output_reader.py`, the shape of `RemoteOutputs` (`:141`).
- Activity `lan_upload(LanUploadInput(run_id: str, printer: str, sliced: SlicedSource, remote_name: str)) -> LanUploaded(remote_name: str, md5: str, bytes: int)`: downloads the sliced 3MF (`client.download_library_file` streamed to a spooled temp file for `library_file_id`, or `RemoteBlobs.get(blob_key)` for `blob_key`; the print worker has no store) and `LanPrinter.upload`s it; heartbeats per chunk; `remote_name = f"scadbuddy-{run_id[:8]}-p{plate_id}.3mf"`.
- Activity `lan_print(LanPrintInput(printer: str, uploaded: LanUploaded, plate_id: int, subtask_name: str, ams_mapping: list[int], use_ams: bool, bed_type: str)) -> LanStarted(subtask_name: str, started_at: datetime)`: publishes `project_file_command` with `param=f"Metadata/plate_{plate_id}.gcode"`, `url=file_url(remote_name)`, then waits up to 30 s for a report whose `subtask_name` matches or whose `print_error` is non-zero (→ `ApplicationError` type `FAILED` with the code). `ams_mapping` is `printers/ams_mapping.py` `ams_mapping(plan, slots)` from `docs/superpowers/plans/2026-10-18-hot-path-without-bambuddy.md` Task 7. Nothing in ScadBuddy computes one today: Bambuddy's scheduler does, for a queue item that carries none (`bambuddy/filaments.py:1-8`, `:253`, `:786`; `print_run.py:1572` "no `ams_mapping` is sent"), and `print_run.py:491-520` is `resolve_project`/`read_status`, not a rack pick. `-1` for a slot the plate does not print with. A plate that prints from the H2C's hotend rack is not refused here: this task ships no refusal, and the hot-path plan's Task 8 (D7) adds `nozzle_mapping` to this input once the rack command is measured.
- In `_print`, after the slice: `if planned.path == "native" and workflow.patched(NATIVE_PATCH):` run `lan_upload` (`start_to_close_timeout=10 min`, `heartbeat_timeout=HEARTBEAT`, `retry_policy=BOUNDED_RETRY`) then `lan_print` (`SHORT`, `ONCE`), shielded as `print_enqueue` is; `QueueOutcome(lan=started, queue_item_ids=[], printer_id=planned.printer_id)`. Everything else (`print_start_enqueue`, `print_record`, `print_finish`, `print_succeed`, `_follow`) is unchanged.
- Follow: `progress_for` gains a `route="lan"` branch: when the subject's recorded outcome has `lan` set, progress is read from `PrinterReportStore.get(printer)`: `stage` from `gcode_state` (`RUNNING`/`PREPARE` → `printing`, `FINISH` → `completed`, `FAILED` → `failed`, `PAUSE` → `paused`), `copies_completed` 1 on `FINISH`, `settled` on `FINISH`/`FAILED` or when the report is older than `MAX_AGE`, `error_message` the `print_error` code. The same `print.progress`/`print.settled` events on `print:<subject>` (`progress.py:539-565`).
- `PrintRunResult.bambuddy_url` is `None` for a native print; the browser shows the printer's name instead (Task 10).

- [ ] **Step 1: Write the failing tests**

```python
async def test_native_run_uploads_and_starts(temporal_env, fake_lan, registry_native) -> None:
    # fake_lan records upload() and publish(); the run succeeds with outcome.lan set and queue_item_ids == []
async def test_native_run_records_print_error(temporal_env, fake_lan_error) -> None: ...
async def test_bambuddy_path_is_unchanged(temporal_env, bambuddy) -> None: ...
def test_native_follow_settles_when_report_stops(reports) -> None: ...
```

- [ ] **Step 2: Run them to make sure they fail**

Run: `cd backend && uv run --frozen pytest tests/test_print_native_workflow.py tests/printers/test_native_progress.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement**

- [ ] **Step 4: Run them, the replay fixtures and the follow tests**

Run: `uv run --frozen pytest tests/test_print_native_workflow.py tests/printers tests/test_print_replay.py tests/test_follow_workflow.py -n auto`. Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/scadbuddy backend/tests
git commit -m "feat(print): the native LAN branch of PrintRun, and its follow from the printer's report"
```

### Task 10: Frontend: printers in Settings, the picker and Now printing

**Files:**
- Modify: `frontend/src/api/types.ts`, `frontend/src/api/client.ts:1725` (add `listPrinters`, `createPrinter`, `patchPrinter`, `deletePrinter`, `linkPrinterBambuddy`, `unlinkPrinterBambuddy`, `linkPrinterLan`, `unlinkPrinterLan`, `discoverPrinters`)
- Create: `frontend/src/components/settings/Printers.tsx`, `frontend/src/components/settings/Printers.test.tsx`, `frontend/src/mocks/features/printers.ts`, `frontend/e2e/printers.spec.ts`
- Modify: `frontend/src/pages/SettingsPage.tsx` (a "Printers" section), `frontend/src/components/prints/NowPrinting.tsx` (follow the `printers` topic through `useRealtime`, keep `NOW_POLL_MS` as the fallback; show `path` as a small label "LAN" / "Bambuddy"), `frontend/src/components/PrintPicker.tsx` (the printer select lists registry rows; it sends `printer` and, when the row links a Bambuddy printer, `printer_id` too, so an older backend still reads it), `frontend/src/mocks/features/printersNow.ts` (the new fields)
- Test: `frontend/src/components/settings/Printers.test.tsx`, `frontend/src/components/prints/NowPrinting.test.tsx`, `frontend/e2e/printers.spec.ts`

**Interfaces:**
- Consumes: the Task 3 routes and the Task 6 `PrinterNow` fields.
- The Settings section lists printers (name, serial, model, Bambuddy link, LAN host, path select), a "Link a Bambuddy printer" list from `/printers/discover` with unlinkable rows disabled and their `reason` shown, a LAN form (host, 8-digit access code, "Test and link" → `PUT /lan`), and "Unlink" on each link. The form's layout is the builder's.

- [ ] **Step 1: Write the failing component tests**

```ts
it('lists printers with their links and path', ...)
it('links a discovered Bambuddy printer and disables one without a serial', ...)
it('links a LAN printer and never shows the code again', ...)
it('shows the probe failure from a 502', ...)
```

- [ ] **Step 2: Run them to make sure they fail**

Run: `cd frontend && pnpm test -- Printers`
Expected: FAIL, component not found.

- [ ] **Step 3: Implement the mocks, the API client methods and the components**

- [ ] **Step 4: Write and run the e2e spec**

`printers.spec.ts`: open Settings → Printers, link a discovered printer, link a LAN printer, see both in Now printing with their path labels. Run: `pnpm exec playwright test e2e/printers.spec.ts`. Expected: PASS.

- [ ] **Step 5: Run the full frontend checks**

Run: `pnpm lint && pnpm typecheck && pnpm test && pnpm build`. Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add frontend
git commit -m "feat(frontend): printers in Settings; the picker and Now printing know a printer's path"
```

### Task 11: Agent tools and coverage

**Files:**
- Create: `agent/src/tools/printers.ts`, `agent/test/printers.test.ts`
- Modify: `agent/src/tools/index.ts` (register), `agent/src/tools/coverage.ts` (nothing to allowlist if every route gets a tool; otherwise `GET /api/v1/printers/discover` with a reason)
- Modify: `plugins/scadbuddy/skills/print/SKILL.md` (a paragraph on printers and paths, with its sources; the hook bumps the plugin version)

**Interfaces:**
- Tools: `list_printers` (read), `get_printer` (read), `discover_printers` (read), `create_printer` (write), `update_printer` (write; `preferred_path` and `name`), `link_printer_bambuddy` (write), `unlink_printer_bambuddy` (write), `link_printer_lan` (outward: it stores a credential and connects to a device), `unlink_printer_lan` (write), `delete_printer` (outward). Each `defineTool` names its operation as `farm.ts:17-60` does.

> **As built (#4340 review, departing from the interface above):** there is no `link_printer_lan`. The access code is a device credential, so it is written only in Settings > Printers, as the Bambuddy key and the settings are. As a tool argument it would reach the model's context, the transcript, Hindsight and any flow script. `PUT /api/v1/printers/{printer_id}/lan` is a `coverage.ts` `NOT_A_TOOL` entry with that reason. `list_printers` and `get_printer` send the user to Settings > Printers, and the model can open the page with `browser_navigate` (the form there is `USER_ONLY`). `unlink_printer_lan` is **outward**, not write: it deletes the stored code and stops the printer's watch. Both SENSITIVE_KEY copies (agent `sdkEvents.ts`, agent-durable `session/events.py`) blank `access_code`, and both are pinned to `agent/test/fixtures/sensitive-keys.json`.

- [ ] **Step 1: Write the failing tests** (the fake backend pattern of `agent/test/farm.test.ts`)

```ts
it('link_printer_lan is outward and never echoes the access code', ...)
it('every /printers operation is covered', ...)   // coverage.test.ts does this; run it
```

- [ ] **Step 2: Run them to make sure they fail**

Run: `cd agent && pnpm test -- printers coverage`
Expected: FAIL (coverage names the uncovered operations).

- [ ] **Step 3: Implement the tools**

- [ ] **Step 4: Run the agent checks**

Run: `pnpm lint && pnpm typecheck && pnpm test`. Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add agent plugins/scadbuddy agent/plugins/scadbuddy
git commit -m "feat(agent): printer registry tools"
```

### Task 12: Deployment notes and the layout entry

**Files:**
- Modify: `README.md` ("Deploying": the print worker mounts `SCADBUDDY_SECRET_KEY_FILE` for LAN printing; the printer must be reachable from the print worker's network on 8883 and 990; the `requires_printer` variables), `CLAUDE.md` ("Layout": a `backend/scadbuddy/printers/` bullet naming the registry, the LAN client, `printer_watch`, the native branch and the tests' marker), `docs/superpowers/specs/2026-10-16-own-store-slicer-rust-workers-design.md` §2 (any fact Task 1, 5 or 7 corrected)

- [ ] **Step 1: Write the three edits**

- [ ] **Step 2: Run the lint job's checks that read them**

Run: `bash .github/scripts/lint-plugin.sh` (the skill's citations) and `markdownlint` if the repo runs it; otherwise `git diff --check`. Expected: clean.

- [ ] **Step 3: Commit**

```bash
git add README.md CLAUDE.md docs/superpowers/specs/2026-10-16-own-store-slicer-rust-workers-design.md
git commit -m "docs: printer registry and the native LAN path"
```

---

## Self-review

**Spec coverage (§6):** registry keyed by serial with both links (Tasks 2, 3); linking explicit and reversible, LAN link verified by a `pushall` probe (Tasks 3, 4); serial pre-filled from Bambuddy (Task 1 found it in `GET /printers/`, Task 3 `discover`); one request, two paths, Bambuddy id accepted and resolved or created on the fly, `auto` by reachability (Task 8); `lan_upload`, `lan_print`, the native follow with the same `print.*` events (Task 9); `printer_watch` as `printer-<serial>` with `printer_reports` and `printer:<id>` events (Task 6); `PrinterStatus` from either link, `/printers/now` from whichever answers (Tasks 5, 6); §6.3 Bambuddy-only features untouched; §6.4 credentials sealed under the KEK with AAD id+serial, never returned (Task 2, Review Focus). Every new activity registers on the existing `bambuddy` queue (and its follow queue), as the spec now says.

**Step scan:** Task 7 leaves two constants to a measurement, by design; every other step names its file and signature. Task 10's form layout is explicitly the builder's.

**Type consistency:** `PrinterRow.id: str` everywhere (`printer: str` on the request and `PlannedRun`); `bambuddy_printer_id: int | None`; `LanReport.print` is the merged `print` object in Tasks 4, 5, 6 and 9; `SlicedSource` is the seam the slicer plan fills.

**Review Focus:** each of the five lines names its test and task.

**Proportion:** twelve tasks over a §6 of about 70 lines; the plan is longer than the spec section because it pins the LAN protocol facts and the workflow seams the spec only names, and shorter than the code it describes.

## Deferred to 3b

The tables keyed by the Bambuddy printer integer stay keyed as they are in this plan and are reached from a registry id only through `PrinterStore.resolve_bambuddy_id`. Moving them to the registry uuid is a follow-up plan:

- `settings.printer_id` (the configured default printer)
- the `rack_nozzle_*` hotend usage tables (#836)
- `project_print_targets`
- the remembered bed types and remembered print choices per printer

## Open questions for the owner

- Whether a run on a printer with both links should fall back from `native` to `bambuddy` when `lan_print` fails (this plan fails the run, as a Bambuddy refusal does today).
- Whether a changed printer certificate (a firmware reset) should be re-pinned by re-entering the access code only (this plan: yes, `PUT /lan` again; a mismatch in between is a `CertificateMismatchError` the run reports).
