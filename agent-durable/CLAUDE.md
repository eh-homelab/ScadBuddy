# Python agent parts — notes for agents

These notes cover `agent-durable/`; paths are repo-relative; the root `CLAUDE.md` has the repo-wide rules.

## Commands
The Python agent parts (`agent-durable/`, Python 3.14; the `agent-durable` CI job, #1056):

```bash
cd agent-durable
uv run --frozen ruff check . && uv run --frozen ruff format --check .
uv run --frozen mypy
uv run --frozen pytest            # requires_postgres tests need SCADBUDDY_TEST_DATABASE_URL
docker build --target agent-durable -t scadbuddy-agent-durable:dev .
```

It needs uv 0.9.0 or later, which `[tool.uv] required-version` enforces. The job also runs
`.github/scripts/harness-pin.test.sh`: `uv-git-pin.sh` on the backend's temporal-agent-harness
pin, which must fail on a vanished commit (`uv lock --check` alone cannot tell).


## Layout
- `agent-durable/` — ScadBuddy's Python agent parts (#1056; spec 2026-10-13 §14.4), shipped
  as the Dockerfile's `agent-durable` target, trusted like `agent`. It ran the Python turn
  (DurableSession on the `agent` queue) until plan Step 8.1 retired it: it now depends on no
  Claude Agent SDK, and `python -m scadbuddy_durable` serves `/healthz` on 8082 and runs no
  worker (`worker.py` `NO_WORKER`), until a Python workload exists (decision Q9; clusters
  keeps its Deployment at zero). What stays, tested against the agent's vector files:
  `secrets.py`/`credentials.py` (open `ai_credentials` as the agent does; only the agent
  writes a secret), `codec.py` and `payload_store.py` (the payload codec's Python half),
  and `gate/` (the gate's Python half: ids, the `respond` validator, `build_entry`, and the
  `open_input` / `resolve_input` writers of `ai_pending_input`, `ai_input_responses`, the
  session's events and the approval audit row, which `agent/test/gateStore.pg.test.ts`
  runs against the TypeScript store).

## See also

- Vector fixtures are written by the agent's tests: see `agent/CLAUDE.md`.
