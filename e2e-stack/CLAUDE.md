# Real-stack e2e — notes for agents

These notes cover `e2e-stack/`; paths are repo-relative; the root `CLAUDE.md` has the repo-wide rules.

## Commands
`e2e-stack/run.sh --auth [--keep] [--no-build]` is the login's (#5198: `auth.sh`,
`compose.auth.yaml`, `e2e/real-auth.spec.ts` in the Playwright image's three engines on
the stack's network; same job). `e2e-stack/run.sh [--mode recorder|unset|both] [--keep] [--no-build]` is the real-stack
e2e of the native hot path (docker with compose v2; it builds the runtime, slicer and test
images; weekly and on dispatch in `hot-path-e2e.yml`'s `stack` job, never on PRs);
`e2e/real-hot-path.spec.ts` skips without what it sets.

## Layout
- `e2e-stack/` — the real-stack e2e of the hot path (#4602): `compose.yaml` (ScadBuddy on
  moto's S3, the Temporal dev server, the slicer worker, a fake LAN printer and a
  Bambuddy recorder that fails every call), `run.sh` (builds, brings up, seeds with
  `seed.sh`, runs `frontend/e2e/real-hot-path.spec.ts`, tears down by compose project),
  `fake-printer/` (MQTT and FTPS) and `bambuddy-recorder/`; their tests are
  `backend/tests/printers/test_fake_printer.py` and `backend/tests/test_bambuddy_recorder.py`.
  `.github/workflows/hot-path-e2e.yml` runs the stack (its `stack` job) weekly and on
  dispatch, never on a PR. Its `fakes` job (those two tests, ruff and mypy over the fakes)
  runs on a PR that touches `e2e-stack/**`, `backend/**` or the workflow, and is not in
  `CI Summary`. The test image carries mosquitto (`SCADBUDDY_TEST_MQTT`) and both fakes
  (#4541), so the fake-printer tests (`tests/test_print_native_workflow.py`,
  `tests/api/test_hot_path_native.py` and the fakes' own) run in every backend-pytest shard;
  locally they skip without a `mosquitto` on `PATH` or `SCADBUDDY_TEST_MQTT`.

## See also

- Frontend specs it runs: see `frontend/CLAUDE.md`. Hot path code: see `backend/CLAUDE.md`.
