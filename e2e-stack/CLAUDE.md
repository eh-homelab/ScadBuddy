# Real-stack e2e — notes for agents

These notes cover `e2e-stack/`; paths are repo-relative; the root `CLAUDE.md` has the repo-wide rules.

## Commands
`e2e-stack/run.sh --auth [--keep] [--no-build]` is the login's (#5198: `auth.sh`,
`compose.auth.yaml`, `e2e/real-auth.spec.ts` in the Playwright image's three engines on
the stack's network; same job). `e2e-stack/run.sh [--mode recorder|unset|both] [--keep] [--no-build]` is the real-stack
e2e of the native hot path (docker with compose v2; it builds the runtime, slicer, test and core
images; weekly and on dispatch in `hot-path-e2e.yml`'s `stack` job, never on PRs);
`e2e/real-hot-path.spec.ts` skips without what it sets. On the first mode's stack, `run.sh`
also runs `e2e/inventory-core.spec.ts` (#6949 K1, `E2E_INVENTORY_CORE=1`; it skips without
it): a kind, an item, a lot received and 1 used, all writes through Temporal to
scadbuddy-core's worker; the item reads 1 less, and the Activities tab lists
`inventory.ReceiveLot` and `inventory.Take` as succeeded. `run.sh` fails that run when its
JSON report (`frontend/test-results/inventory-core.json`) counts no passed test, or a skipped
or failed one. Its logs go to `frontend/test-results/inventory-core/stack.log`.

## Layout
- `e2e-stack/` — the real-stack e2e of the hot path (#4602): `compose.yaml` (ScadBuddy on
  moto's S3, the Temporal dev server, the slicer worker, a fake LAN printer, a
  Bambuddy recorder that fails every call, and `core`: scadbuddy-core from
  `tools/go/core/Dockerfile` with areas `inventory,inventory:read`, its CRUD port on 8082
  (the app's `SCADBUDDY_CORE_CRUD_URL`) and its inventory worker on the Temporal queue
  `inventory`; it has no healthcheck, since its image has no curl), `run.sh` (builds, brings up, seeds with
  `seed.sh`, runs `frontend/e2e/real-hot-path.spec.ts` and, on the first stack,
  `frontend/e2e/inventory-core.spec.ts`, tears down by compose project),
  `fake-printer/` (MQTT and FTPS) and `bambuddy-recorder/`; `seed.sh` makes a spool
  (`new-spool`) for each untagged filled tray in `fake-printer/report.json`, as a set-up
  printer has, or the print is refused with `unknown_spool_loaded` (#7143); their tests are
  `backend/tests/printers/test_fake_printer.py` and `backend/tests/test_bambuddy_recorder.py`.
  `.github/workflows/hot-path-e2e.yml` runs the stack (its `stack` job) weekly and on
  dispatch, never on a PR. Its `fakes` job (those two tests, ruff and mypy over the fakes)
  runs on a PR that touches `e2e-stack/**`, `backend/**` or the workflow, and is not in
  `CI Summary`. The test image carries mosquitto (`SCADBUDDY_TEST_MQTT`) and both fakes
  (#4541), so the fake-printer tests (`tests/test_print_native_workflow.py`,
  `tests/api/test_hot_path_native.py` and the fakes' own) run in every backend-pytest shard;
  locally they skip without a `mosquitto` on `PATH` or `SCADBUDDY_TEST_MQTT`.
  `backend/tests/printers/test_lan_command_fake.py` (the `lan_command` activity against the
  fake, #7276) needs Postgres too, so it runs in the backend shards, not the `fakes` job.

## See also

- Frontend specs it runs: see `frontend/CLAUDE.md`. Hot path code: see `backend/CLAUDE.md`.
