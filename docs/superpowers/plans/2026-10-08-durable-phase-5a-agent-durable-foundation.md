# Durable phase 5a: the `agent-durable` foundation — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the `agent-durable/` Python package with its plugin pin, the credential port that opens `ai_credentials` exactly as the agent does (proven by shared test vectors), its image stage with a bundled-CLI check, and the CI that guards all of it, before any session runs on it.

**Architecture:** A new uv project `agent-durable/` (Python 3.12, module `scadbuddy_durable`) depends on `temporalio-claude-agent-sdk` pinned by git SHA to `temporalio/ai-integrations`. `secrets.py` ports `agent/src/secrets.ts`'s `openSecret` (open only, never seal) and `credentials.py` ports `credentialAad`, `openCredential`, `credentialEnv` and the pool's usable-in-priority-order read. One JSON file of envelopes, written deterministically by the agent's TypeScript test suite, is opened by both suites, so neither side can change the format alone. The Dockerfile gains an `agent-durable` target that fails its build when the bundled Claude Code binary is not the version the SDK declares.

**Tech Stack:** Python 3.12, uv 0.12.23, `cryptography` (AES-256-GCM), `psycopg` 3 (async), pytest + pytest-asyncio, `temporalio` 1.33–1.34, `claude-agent-sdk` 0.2.164 (Python, bundles Claude Code 2.1.292), TypeScript/vitest on the agent side.

**Spec:** `docs/superpowers/specs/2026-10-01-durable-printing-agents-flows-design.md` §3.2, §6.2 ("The dependency", "The credential", "Test vectors"), §6.3a, §10 phase 5. Issue #1056, epic #1058.

## Phase 5 is split into five sub-plans

Each one ships on its own and is written when it starts (as phases 2 and 3 were):

| Sub-plan | Scope | Depends on |
|---|---|---|
| **5a (this plan)** | `agent-durable/` package, plugin pin and its monitor, credential port and shared vectors, the image stage, the CI job | — |
| 5b | The tool-call gate (§6.6), both modes: `ai_pending_input` / `ai_input_responses`, `pending_input` / `respond`, the classic `pending-input` and `respond` routes over `ai_approvals` and `ai_questions`, timers, the orphan sweep | 5a; and the `fix/prf-agent-approvals` and `fix/prf-assistant-panel` PRs (manage-pr-reviews-merges session), which land first |
| 5c | `DurableSession` (§6.2): the workflow on queue `agent`, `send_message`, the tool manifest as `activity_as_tool`s, limits, the event subscriber into `ai_session_events`, the payload codec (§6.5) and `forgetSubject`, image inputs by claim check (from #1868) | 5a, 5b |
| 5d | Mode (§6.1): `mode` on session create (chat socket, `POST /sessions`, `sessions_start`), `session_mode` setting and its routes, the composer's Advanced picker, the Durable badge, Settings → Assistant | 5c |
| 5e | Deploy: `build-image.yml` pushes `scadbuddy-agent-durable`, `deploy.reusable.yml` pins it, the clusters sidecar, the Temporal frontend's ingress `CiliumNetworkPolicy` | 5c |

## What changed since the spec (re-read 2026-10-08)

- **The §3.2 gate passes.** ai-integrations#33 is still a draft (head `b1cf3848b15ad5cd1f009bd19524e3f751140439`, committed 2026-10-03, PR updated 2026-10-06), and its description reports all three limitations solved: several durable calls per message (2c1fcb6), Bash and MCP calls as activities (39257db, 615e8ff), and an external session store no longer required (995bac6). None that this design relies on is still open. Recorded on #1056.
- **The CLI versions differ.** The package requires `claude-agent-sdk>=0.2.153,<0.3` (Python). No Python release bundles the TypeScript SDK's Claude Code 2.1.289 (`@anthropic-ai/claude-agent-sdk` 0.3.289). Measured: 0.2.159 → 2.1.281, 0.2.161 → 2.1.284, 0.2.162 → 2.1.285, 0.2.163 → 2.1.286, 0.2.164 → 2.1.292. The spec requires ≥ 2.1.273 and a build-time check that the binary is the declared version, not equal versions across the two SDKs. This plan pins 0.2.164 (2.1.292) exactly.
- **The previous key is not needed here.** The agent re-wraps every credential from `SCADBUDDY_SECRET_KEY_PREVIOUS_FILE` to the current key at start (`agent/src/main.ts:97-113`), and a credential sealed under another key is skipped as not openable (`opensWith`, `agent/src/credentials.ts:317`). So the port reads only `SCADBUDDY_SECRET_KEY_FILE`. 5c reads the previous key too, for `ai_payload_keys` re-wrapping.

## Global Constraints

- Python `requires-python = ">=3.12,<3.13"`, like `backend/pyproject.toml`.
- The dependency is exactly `temporalio-claude-agent-sdk @ git+https://github.com/temporalio/ai-integrations@b1cf3848b15ad5cd1f009bd19524e3f751140439#subdirectory=python/claude_agent_sdk`. Never the contributor's fork. Not vendored (the user's decision, spec §9).
- `claude-agent-sdk==0.2.164` is pinned exactly in `agent-durable/pyproject.toml`; `temporalio>=1.33.0,<1.35`, as the backend pins it.
- A pin change is its own PR that carries the diff of `python/claude_agent_sdk` between the old and new SHAs (spec §6.2, "Every bump is reviewed").
- Sealed format `version(1) | IV(12) | tag(16) | ciphertext`, AES-256-GCM; AAD `v2|<context>` for version 2 and the bare context for version 1; data-key context `dek:` + the secret's; KEK id = first 16 hex characters of the key's SHA-256; key file = exactly 32 bytes, standard base64 with padding, surrounding whitespace ignored.
- Credential AAD: `ai_credentials:<row id>:` + `JSON.stringify({kind, base_url})`, keys in that order, no spaces. A version-1 credential is refused with `LEGACY_FORMAT_MESSAGE`, never opened.
- Never log, return in an error, write to disk, or put in a `repr` a secret, a data key or a KEK.
- The image runs as `USER 10001:10001`, opens no port but its health check (8082), and reads only `SCADBUDDY_DATABASE_URL`, `SCADBUDDY_SECRET_KEY_FILE`, `SCADBUDDY_SECRET_KEY_PREVIOUS_FILE` and the Temporal variables.
- Every CI job runs on `ubuntu-latest`; a new `type=gha` cache scope uses `mode=min` (CLAUDE.md, "CI and caching rules").
- Shell scripts pass `shellcheck`; the Dockerfile passes `hadolint` with `.hadolint.yaml`.

## Review Focus

1. **A credential whose `base_url` holds a character `JSON.stringify` and Python's `json.dumps` encode differently** (non-ASCII; `/`): the AAD bytes differ, and the port fails authentication on a real row. Expect it to open. Pinned by a gateway vector whose base URL has a path and a non-ASCII host label (Task 2).
2. **A key file with a trailing newline, CRLF, or 33 bytes**: the agent trims whitespace and refuses a wrong length. Expect the same, with an error naming `SCADBUDDY_SECRET_KEY_FILE` and never the path's contents (Task 3).
3. **A cooling-down credential whose cooldown has passed**: the agent counts it active (`CASE WHEN … cooldown_until <= now()`). Expect it in the usable list, in priority order (Task 4).
4. **A row sealed under a different KEK, a v1 row, or a row whose `base_url` was edited** sits before a good one: expect it skipped (never raised), and the next row used (Task 4).
5. **The bundled binary missing for the build's platform, or printing an unexpected `--version`**: expect the image build to fail and name the declared version, never to pass with no check (Task 5).

---

## File structure

- Create `agent-durable/pyproject.toml`: the project, the pin, ruff/mypy/pytest settings.
- Create `agent-durable/uv.lock`: generated by `uv lock`, committed.
- Create `agent-durable/src/scadbuddy_durable/__init__.py`: empty.
- Create `agent-durable/src/scadbuddy_durable/secrets.py`: KEK loading and `open_secret`; one responsibility, the envelope format.
- Create `agent-durable/src/scadbuddy_durable/credentials.py`: the credential AAD, open, env, and the usable read; one responsibility, `ai_credentials`.
- Create `agent-durable/src/scadbuddy_durable/check_cli_version.py`: the build-time assertion.
- Create `agent-durable/src/scadbuddy_durable/health.py` and `__main__.py`: the process entry, `/healthz` only until 5c adds the worker.
- Create `agent-durable/tests/conftest.py`, `test_secrets.py`, `test_credentials.py`, `test_check_cli_version.py`, `test_health.py`.
- Modify `agent/src/secrets.ts`: an injectable random source for `sealSecret`, so vectors are deterministic.
- Create `agent/test/secretVectors.test.ts` and `agent/test/fixtures/secret-vectors.json`.
- Modify `Dockerfile`: the `agent-durable` stage.
- Modify `.github/workflows/ci.yml`: `layout` output, the `agent-durable` job, `summary`.
- Create `.github/workflows/agent-durable-pin.yml`: the weekly pin check.
- Modify `CLAUDE.md`: commands and layout for `agent-durable/`.

---

### Task 1: The project and its pin (the spec's first task, a stop gate)

**Files:**
- Create: `agent-durable/pyproject.toml`
- Create: `agent-durable/src/scadbuddy_durable/__init__.py`
- Create: `agent-durable/uv.lock` (generated)
- Create: `agent-durable/tests/test_import.py`

**Interfaces:**
- Produces: the importable package `scadbuddy_durable`; the locked `temporalio.claude_agent_sdk` and `claude_agent_sdk` modules every later task and sub-plan imports.

- [ ] **Step 1: Write the failing test**

`agent-durable/tests/test_import.py`:

```python
import importlib.metadata


def test_the_plugin_resolves_from_temporals_repository() -> None:
    dist = importlib.metadata.distribution("temporalio-claude-agent-sdk")
    direct_url = dist.read_text("direct_url.json") or ""
    assert "github.com/temporalio/ai-integrations" in direct_url
    assert "b1cf3848b15ad5cd1f009bd19524e3f751140439" in direct_url


def test_the_python_sdk_is_the_pinned_one() -> None:
    assert importlib.metadata.version("claude-agent-sdk") == "0.2.164"
```

- [ ] **Step 2: Write `pyproject.toml`**

```toml
[project]
name = "scadbuddy-durable"
version = "0.1.0"
description = "ScadBuddy's durable agent sessions on Temporal (spec 2026-10-01 §6)"
requires-python = ">=3.12,<3.13"
dependencies = [
    # ai-integrations#33, by full SHA in Temporal's own repository, never the
    # contributor's fork (spec §6.2). A bump is its own reviewed PR carrying the
    # diff of python/claude_agent_sdk between the two SHAs. Moves to PyPI once
    # the package is published.
    "temporalio-claude-agent-sdk @ git+https://github.com/temporalio/ai-integrations@b1cf3848b15ad5cd1f009bd19524e3f751140439#subdirectory=python/claude_agent_sdk",
    # Exact: its bundled Claude Code (2.1.292) is what check_cli_version asserts.
    "claude-agent-sdk==0.2.164",
    "temporalio>=1.33.0,<1.35",
    "cryptography>=44",
    "psycopg>=3.2",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/scadbuddy_durable"]

[tool.hatch.metadata]
allow-direct-references = true

[dependency-groups]
dev = [
    # The wheel with its own libpq, for tests without one on the system. The
    # image uses plain psycopg against apt's libpq5, as the backend does.
    "psycopg[binary]>=3.2",
    "mypy>=1.13",
    "pytest>=8.3",
    "pytest-asyncio>=0.24",
    "ruff>=0.8",
]

[tool.ruff]
line-length = 100
target-version = "py312"

[tool.ruff.lint]
select = ["E", "F", "I", "N", "UP", "B", "SIM", "RUF"]

[tool.mypy]
python_version = "3.12"
strict = true
files = ["src", "tests"]

[tool.pytest.ini_options]
testpaths = ["tests"]
asyncio_mode = "auto"
markers = [
    "requires_postgres: needs SCADBUDDY_TEST_DATABASE_URL pointing at a Postgres",
]
```

`agent-durable/src/scadbuddy_durable/__init__.py`: empty file.

- [ ] **Step 3: Lock — the stop gate**

Run: `cd agent-durable && uv lock`
Expected: `uv.lock` written, with `source = { git = "https://github.com/temporalio/ai-integrations?subdirectory=python%2Fclaude_agent_sdk&rev=b1cf…#b1cf3848b15ad5cd1f009bd19524e3f751140439" }`.

If it does not resolve from `temporalio/ai-integrations` by that SHA, **stop**: post on #1056 what failed and wait for the user (spec §6.2, §9). Do not point it at the fork, and do not vendor.

Check it: `grep -n "ai-integrations" uv.lock`, which should show the Temporal URL and the full SHA.

- [ ] **Step 4: Run the test**

Run: `cd agent-durable && uv sync --frozen && uv run --frozen pytest tests/test_import.py -v`
Expected: 2 passed.

- [ ] **Step 5: Lint and type-check**

Run: `uv run --frozen ruff check . && uv run --frozen ruff format --check . && uv run --frozen mypy`
Expected: no findings.

- [ ] **Step 6: Commit**

```bash
git add agent-durable/pyproject.toml agent-durable/uv.lock agent-durable/src agent-durable/tests/test_import.py
git commit -m "feat(agent-durable): the package and its ai-integrations#33 pin (#1056)"
```

---

### Task 2: Deterministic secret vectors from the agent's own `sealSecret`

**Files:**
- Modify: `agent/src/secrets.ts` (`seal`, `sealSecret`)
- Create: `agent/test/secretVectors.test.ts`
- Create: `agent/test/fixtures/secret-vectors.json` (generated)

**Interfaces:**
- Produces: `sealSecret(kek: Kek, plaintext: string, aad: string, random?: (n: number) => Buffer): Envelope`. `random` defaults to `randomBytes` and exists for the vectors only.
- Produces: `agent/test/fixtures/secret-vectors.json`, shaped as:

```json
{
  "kek_b64": "<44 chars>",
  "kek_id": "<16 hex>",
  "credentials": [
    {"name": "...", "id": "...", "priority": 0, "kind": "anthropic_api_key", "base_url": null,
     "secret_sealed_b64": "...", "dek_sealed_b64": "...", "kek_id": "...",
     "plaintext": "...", "opens": true}
  ],
  "secrets": [
    {"name": "...", "aad": "...", "version": 1, "secret_sealed_b64": "...", "dek_sealed_b64": "...",
     "kek_id": "...", "plaintext": "..."}
  ]
}
```

- [ ] **Step 1: Thread a random source through `seal`**

In `agent/src/secrets.ts`, change `seal` and `sealSecret` (the only two `randomBytes` callers on the seal path):

```ts
/** Where `seal` takes its IV and `sealSecret` its data key: `randomBytes`, except in the vectors. */
export type RandomSource = (size: number) => Buffer

function seal(key: Buffer, plaintext: Buffer, context: string, random: RandomSource = randomBytes): Buffer {
  const iv = random(IV_BYTES)
  const cipher = createCipheriv('aes-256-gcm', key, iv)
  cipher.setAAD(aadFor(SEAL_VERSION, context))
  const ciphertext = Buffer.concat([cipher.update(plaintext), cipher.final()])
  return Buffer.concat([Buffer.from([SEAL_VERSION]), iv, cipher.getAuthTag(), ciphertext])
}

/**
 * `random` exists for agent/test/secretVectors.test.ts, which seals with fixed bytes
 * so the vectors the Python port opens are reproducible. Never pass it elsewhere:
 * a repeated IV under one key breaks GCM.
 */
export function sealSecret(kek: Kek, plaintext: string, aad: string, random: RandomSource = randomBytes): Envelope {
  const dek = random(KEK_BYTES)
  const bytes = Buffer.from(plaintext, 'utf8')
  try {
    return {
      secretSealed: seal(dek, bytes, aad, random),
      dekSealed: seal(kek.key, dek, `dek:${aad}`, random),
      kekId: kek.id,
    }
  } finally {
    dek.fill(0)
    bytes.fill(0)
  }
}
```

`rewrap` keeps calling `seal(newKek.key, dek, …)` with the default.

- [ ] **Step 2: Write the vectors test**

`agent/test/secretVectors.test.ts`:

```ts
import { createCipheriv, createHash } from 'node:crypto'
import { readFileSync, writeFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'
import { credentialAad, LEGACY_FORMAT_MESSAGE, openCredential, type CredentialKind } from '../src/credentials.js'
import { kekFromBase64, openSecret, sealSecret, type RandomSource } from '../src/secrets.js'

// The one source of truth for the sealed format (spec 2026-10-01 §6.2, "Test
// vectors"). This test seals fixed plaintexts with fixed bytes and fails if the
// committed file differs, so secrets.ts's format or credentials.ts's AAD cannot
// change without new vectors. agent-durable/tests/test_secrets.py and
// test_credentials.py open every one, so new vectors cannot land without the
// Python port opening them. Regenerate: UPDATE_SECRET_VECTORS=1 pnpm vitest run test/secretVectors.test.ts
const FILE = new URL('./fixtures/secret-vectors.json', import.meta.url)

/** A counter, never random: reproducible bytes for the vectors only. */
function counter(seed: number): RandomSource {
  let n = seed
  return (size) => {
    const out = Buffer.alloc(size)
    for (let i = 0; i < size; i++) out[i] = (n++ * 131 + 7) & 0xff
    return out
  }
}

const KEK_B64 = Buffer.alloc(32, 0x5a).toString('base64')
const kek = kekFromBase64(KEK_B64)

const CREDENTIALS: { name: string; id: string; priority: number; kind: CredentialKind; base_url: string | null; plaintext: string }[] = [
  { name: 'api key, migrated row id', id: 'default', priority: 0, kind: 'anthropic_api_key', base_url: null, plaintext: 'sk-ant-api03-vector-0000000000000000' },
  { name: 'oauth token', id: 'c0ffee00-0000-4000-8000-000000000001', priority: 1, kind: 'claude_oauth_token', base_url: null, plaintext: 'sk-ant-oat01-vector-1111111111111111' },
  { name: 'gateway with a path', id: 'c0ffee00-0000-4000-8000-000000000002', priority: 2, kind: 'gateway', base_url: 'https://gw.example.com/anthropic/v1', plaintext: 'gw-token-vector-2222222222222222' },
  // Review Focus 1: JSON.stringify leaves non-ASCII as is; the port must too.
  { name: 'gateway, non-ASCII host', id: 'c0ffee00-0000-4000-8000-000000000003', priority: 3, kind: 'gateway', base_url: 'https://passerelle.exemple.fr/é/v1', plaintext: 'gw-token-vector-3333333333333333' },
]

/** A version-1 envelope as #354 wrote it (AAD = the bare context). Frozen: seal never writes v1. */
function sealV1(key: Buffer, plaintext: Buffer, context: string, random: RandomSource): Buffer {
  const iv = random(12)
  const cipher = createCipheriv('aes-256-gcm', key, iv)
  cipher.setAAD(Buffer.from(context, 'utf8'))
  const ct = Buffer.concat([cipher.update(plaintext), cipher.final()])
  return Buffer.concat([Buffer.from([0x01]), iv, cipher.getAuthTag(), ct])
}

function build() {
  const credentials = CREDENTIALS.map((c, i) => {
    const env = sealSecret(kek, c.plaintext, credentialAad(c.id, c.kind, c.base_url), counter(1000 * (i + 1)))
    return {
      name: c.name, id: c.id, priority: c.priority, kind: c.kind, base_url: c.base_url,
      secret_sealed_b64: env.secretSealed.toString('base64'), dek_sealed_b64: env.dekSealed.toString('base64'),
      kek_id: env.kekId, plaintext: c.plaintext, opens: true,
    }
  })
  // A v1 credential: openCredential refuses it with LEGACY_FORMAT_MESSAGE.
  const r = counter(9000)
  const legacyAad = credentialAad('c0ffee00-0000-4000-8000-000000000009', 'anthropic_api_key', null)
  const dek = r(32)
  credentials.push({
    name: 'v1 credential (refused)', id: 'c0ffee00-0000-4000-8000-000000000009', priority: 9, kind: 'anthropic_api_key', base_url: null,
    secret_sealed_b64: sealV1(dek, Buffer.from('sk-ant-legacy'), legacyAad, r).toString('base64'),
    dek_sealed_b64: sealV1(kek.key, dek, `dek:${legacyAad}`, r).toString('base64'),
    kek_id: kek.id, plaintext: 'sk-ant-legacy', opens: false,
  })
  const r2 = counter(7000)
  const dek2 = r2(32)
  const secrets = [{
    name: 'v1 secret', aad: 'ai_credentials:default', version: 1,
    secret_sealed_b64: sealV1(dek2, Buffer.from('v1-plain'), 'ai_credentials:default', r2).toString('base64'),
    dek_sealed_b64: sealV1(kek.key, dek2, 'dek:ai_credentials:default', r2).toString('base64'),
    kek_id: kek.id, plaintext: 'v1-plain',
  }]
  return { kek_b64: KEK_B64, kek_id: createHash('sha256').update(kek.key).digest('hex').slice(0, 16), credentials, secrets }
}

describe('secret vectors', () => {
  const built = build()

  it('match the committed file', () => {
    if (process.env.UPDATE_SECRET_VECTORS === '1') writeFileSync(FILE, `${JSON.stringify(built, null, 2)}\n`)
    expect(JSON.parse(readFileSync(FILE, 'utf8'))).toEqual(built)
  })

  it('open with the agent itself', () => {
    for (const v of built.credentials) {
      const row = {
        id: v.id, kind: v.kind, base_url: v.base_url,
        envelope: { secretSealed: Buffer.from(v.secret_sealed_b64, 'base64'), dekSealed: Buffer.from(v.dek_sealed_b64, 'base64'), kekId: v.kek_id },
      }
      if (v.opens) expect(openCredential(kek, row).secret).toBe(v.plaintext)
      else expect(() => openCredential(kek, row)).toThrow(LEGACY_FORMAT_MESSAGE)
    }
    for (const v of built.secrets) {
      const env = { secretSealed: Buffer.from(v.secret_sealed_b64, 'base64'), dekSealed: Buffer.from(v.dek_sealed_b64, 'base64'), kekId: v.kek_id }
      expect(openSecret(kek, env, v.aad)).toBe(v.plaintext)
    }
  })
})
```

- [ ] **Step 3: Run it to see it fail**

Run: `cd agent && pnpm vitest run test/secretVectors.test.ts`
Expected: FAIL, "match the committed file" (ENOENT: no fixture yet).

- [ ] **Step 4: Generate the file, then run without the flag**

Run: `UPDATE_SECRET_VECTORS=1 pnpm vitest run test/secretVectors.test.ts && pnpm vitest run test/secretVectors.test.ts`
Expected: both PASS. A third run still passes, which proves the file is reproducible.

- [ ] **Step 5: Run the existing secrets and credentials tests**

Run: `pnpm vitest run test/secrets.test.ts test/credentials.test.ts && pnpm lint && pnpm typecheck`
Expected: PASS. The default `random` leaves every existing caller unchanged.

- [ ] **Step 6: Commit**

```bash
git add agent/src/secrets.ts agent/test/secretVectors.test.ts agent/test/fixtures/secret-vectors.json
git commit -m "test(agent): reproducible secret vectors, the port's source of truth (#1056)"
```

---

### Task 3: The envelope port, `secrets.py`

**Files:**
- Create: `agent-durable/src/scadbuddy_durable/secrets.py`
- Create: `agent-durable/tests/conftest.py`
- Test: `agent-durable/tests/test_secrets.py`

**Interfaces:**
- Consumes: `agent/test/fixtures/secret-vectors.json` (Task 2).
- Produces:
  - `class SecretKeyError(Exception)`, `class SealError(Exception)`;
  - `@dataclass(frozen=True) class Kek: id: str; key: bytes` (`key` has `repr=False`);
  - `def kek_from_base64(text: str) -> Kek`;
  - `def load_kek(path: str | None, variable: str = "SCADBUDDY_SECRET_KEY_FILE") -> Kek` (raises `SecretKeyError`);
  - `@dataclass(frozen=True) class Envelope: secret_sealed: bytes; dek_sealed: bytes; kek_id: str`;
  - `SEAL_V1 = 1`, `SEAL_V2 = 2`, `def sealed_version(sealed: bytes) -> int | None`;
  - `def open_secret(kek: Kek, envelope: Envelope, aad: str) -> str`.

- [ ] **Step 1: The vectors fixture**

`agent-durable/tests/conftest.py`:

```python
import json
from pathlib import Path
from typing import Any

import pytest

VECTORS = Path(__file__).resolve().parents[2] / "agent" / "test" / "fixtures" / "secret-vectors.json"


@pytest.fixture(scope="session")
def vectors() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(VECTORS.read_text())
    return data
```

- [ ] **Step 2: Write the failing tests**

`agent-durable/tests/test_secrets.py`:

```python
import base64
from pathlib import Path
from typing import Any

import pytest

from scadbuddy_durable.secrets import (
    Envelope,
    SealError,
    SecretKeyError,
    kek_from_base64,
    load_kek,
    open_secret,
)


def _env(v: dict[str, Any]) -> Envelope:
    return Envelope(
        secret_sealed=base64.b64decode(v["secret_sealed_b64"]),
        dek_sealed=base64.b64decode(v["dek_sealed_b64"]),
        kek_id=v["kek_id"],
    )


def test_kek_id_is_the_agents(vectors: dict[str, Any]) -> None:
    assert kek_from_base64(vectors["kek_b64"]).id == vectors["kek_id"]


def test_opens_every_v1_secret_vector(vectors: dict[str, Any]) -> None:
    kek = kek_from_base64(vectors["kek_b64"])
    for v in vectors["secrets"]:
        assert open_secret(kek, _env(v), v["aad"]) == v["plaintext"], v["name"]


def test_a_wrong_aad_fails_authentication(vectors: dict[str, Any]) -> None:
    kek = kek_from_base64(vectors["kek_b64"])
    v = vectors["secrets"][0]
    with pytest.raises(SealError, match="failed authentication"):
        open_secret(kek, _env(v), v["aad"] + "x")


def test_another_keys_envelope_is_named_not_tried(vectors: dict[str, Any]) -> None:
    kek = kek_from_base64(base64.b64encode(b"\x01" * 32).decode())
    v = vectors["secrets"][0]
    with pytest.raises(SealError, match=f"sealed with key {v['kek_id']}"):
        open_secret(kek, _env(v), v["aad"])


def test_a_truncated_envelope_is_malformed(vectors: dict[str, Any]) -> None:
    kek = kek_from_base64(vectors["kek_b64"])
    v = vectors["secrets"][0]
    env = _env(v)
    short = Envelope(secret_sealed=env.secret_sealed, dek_sealed=env.dek_sealed[:20], kek_id=env.kek_id)
    with pytest.raises(SealError, match="malformed"):
        open_secret(kek, short, v["aad"])


@pytest.mark.parametrize("suffix", ["\n", "\r\n", "  \n"])
def test_key_file_whitespace_is_ignored(tmp_path: Path, vectors: dict[str, Any], suffix: str) -> None:
    f = tmp_path / "k"
    f.write_text(vectors["kek_b64"] + suffix)
    assert load_kek(str(f)).id == vectors["kek_id"]


@pytest.mark.parametrize(
    ("content", "message"),
    [
        (base64.b64encode(b"\x00" * 33).decode(), "decodes to 33 bytes, not 32"),
        ("not base64!!", "is not base64"),
        ("00" * 32, "decodes to 48 bytes, not 32"),
    ],
)
def test_a_bad_key_file_is_refused_by_name(tmp_path: Path, content: str, message: str) -> None:
    f = tmp_path / "k"
    f.write_text(content)
    with pytest.raises(SecretKeyError) as raised:
        load_kek(str(f))
    assert message in str(raised.value)
    assert "SCADBUDDY_SECRET_KEY_FILE" in str(raised.value)
    assert content not in str(raised.value)


def test_unset_and_unreadable_key_files_name_the_variable(tmp_path: Path) -> None:
    with pytest.raises(SecretKeyError, match="SCADBUDDY_SECRET_KEY_FILE is not set"):
        load_kek(None)
    with pytest.raises(SecretKeyError, match="SCADBUDDY_SECRET_KEY_FILE cannot be read"):
        load_kek(str(tmp_path / "missing"))


def test_the_key_never_appears_in_repr(vectors: dict[str, Any]) -> None:
    kek = kek_from_base64(vectors["kek_b64"])
    assert repr(kek.key) not in repr(kek)
```

- [ ] **Step 3: Run to see them fail**

Run: `cd agent-durable && uv run --frozen pytest tests/test_secrets.py -v`
Expected: FAIL, `ModuleNotFoundError: scadbuddy_durable.secrets`.

- [ ] **Step 4: Implement**

`agent-durable/src/scadbuddy_durable/secrets.py`:

```python
"""Opening envelopes sealed by agent/src/secrets.ts (spec 2026-10-01 §6.2).

A port of `openSecret` only: this process never seals a credential. The format and
its versions are documented in secrets.ts; agent/test/fixtures/secret-vectors.json
(written by the agent's tests) is what both sides must open.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import re
from dataclasses import dataclass, field

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

KEK_BYTES = 32
_IV_BYTES = 12
_TAG_BYTES = 16
SEAL_V1 = 0x01
SEAL_V2 = 0x02
_KNOWN_VERSIONS = frozenset({SEAL_V1, SEAL_V2})
_BASE64 = re.compile(r"^[A-Za-z0-9+/]+={0,2}$")


class SecretKeyError(Exception):
    """The key file is unset, unreadable or not a key. Never carries its contents."""


class SealError(Exception):
    """Decryption failed: wrong key, or altered bytes. Never carries plaintext."""


@dataclass(frozen=True)
class Kek:
    id: str
    key: bytes = field(repr=False)


@dataclass(frozen=True)
class Envelope:
    secret_sealed: bytes = field(repr=False)
    dek_sealed: bytes = field(repr=False)
    kek_id: str


def kek_from_base64(text: str, variable: str = "SCADBUDDY_SECRET_KEY_FILE") -> Kek:
    trimmed = text.strip()
    if not _BASE64.match(trimmed) or len(trimmed) % 4 != 0:
        raise SecretKeyError(
            f"{variable} is not base64; it must hold 32 random bytes, base64-encoded"
            " (openssl rand -base64 32)"
        )
    try:
        key = base64.b64decode(trimmed, validate=True)
    except binascii.Error as err:
        raise SecretKeyError(f"{variable} is not base64") from err
    if len(key) != KEK_BYTES:
        raise SecretKeyError(
            f"{variable} decodes to {len(key)} bytes, not {KEK_BYTES} (openssl rand -base64 32)"
        )
    return Kek(id=hashlib.sha256(key).hexdigest()[:16], key=key)


def load_kek(path: str | None, variable: str = "SCADBUDDY_SECRET_KEY_FILE") -> Kek:
    if path is None:
        raise SecretKeyError(f"{variable} is not set")
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except OSError as err:
        raise SecretKeyError(f"{variable} cannot be read ({err.errno})") from None
    return kek_from_base64(text, variable)


def sealed_version(sealed: bytes) -> int | None:
    return sealed[0] if sealed else None


def _aad(version: int, context: str) -> bytes:
    # v1 did not authenticate its version byte (secrets.ts `aadFor`).
    return (context if version == SEAL_V1 else f"v{version}|{context}").encode()


def _open(key: bytes, sealed: bytes, context: str) -> bytes:
    version = sealed_version(sealed)
    if len(sealed) < 1 + _IV_BYTES + _TAG_BYTES or version not in _KNOWN_VERSIONS:
        raise SealError("sealed value is malformed or of an unknown version")
    assert version is not None
    iv = sealed[1 : 1 + _IV_BYTES]
    tag = sealed[1 + _IV_BYTES : 1 + _IV_BYTES + _TAG_BYTES]
    ciphertext = sealed[1 + _IV_BYTES + _TAG_BYTES :]
    try:
        return AESGCM(key).decrypt(iv, ciphertext + tag, _aad(version, context))
    except InvalidTag:
        raise SealError("sealed value failed authentication (wrong key, or altered)") from None


def open_secret(kek: Kek, envelope: Envelope, aad: str) -> str:
    if envelope.kek_id != kek.id:
        raise SealError(
            f"secret was sealed with key {envelope.kek_id}, but SCADBUDDY_SECRET_KEY_FILE"
            f" holds key {kek.id}"
        )
    dek = _open(kek.key, envelope.dek_sealed, f"dek:{aad}")
    return _open(dek, envelope.secret_sealed, aad).decode()
```

The `"00" * 32` case (64 hex characters) is valid base64 that decodes to 48 bytes, so it gets the length message, as in the agent.

- [ ] **Step 5: Run the tests**

Run: `uv run --frozen pytest tests/test_secrets.py -v && uv run --frozen mypy && uv run --frozen ruff check .`
Expected: all pass, no findings.

- [ ] **Step 6: Commit**

```bash
git add agent-durable/src/scadbuddy_durable/secrets.py agent-durable/tests/conftest.py agent-durable/tests/test_secrets.py
git commit -m "feat(agent-durable): open the agent's sealed envelopes (#1056)"
```

---

### Task 4: The credential port, `credentials.py`

**Files:**
- Create: `agent-durable/src/scadbuddy_durable/credentials.py`
- Modify: `agent-durable/tests/conftest.py` (the Postgres fixture)
- Test: `agent-durable/tests/test_credentials.py`

**Interfaces:**
- Consumes: `Kek`, `Envelope`, `SealError`, `SEAL_V1`, `sealed_version`, `open_secret` (Task 3).
- Produces (5c's worker calls these per model segment):
  - `CredentialKind = Literal["anthropic_api_key", "claude_oauth_token", "gateway"]`;
  - `LEGACY_FORMAT_MESSAGE: str` (the agent's, verbatim);
  - `@dataclass(frozen=True) class Credential: id: str; priority: int; kind: CredentialKind; base_url: str | None; secret: str` (`secret` has `repr=False`);
  - `def credential_aad(id: str, kind: str, base_url: str | None) -> str`;
  - `def open_credential(kek: Kek, *, id: str, priority: int, kind: CredentialKind, base_url: str | None, envelope: Envelope) -> Credential`;
  - `def credential_env(c: Credential) -> dict[str, str]`;
  - `async def usable_credentials(conn: psycopg.AsyncConnection[Any], kek: Kek) -> list[Credential]`.

- [ ] **Step 1: The Postgres fixture, applying the agent's real migrations**

Append to `agent-durable/tests/conftest.py`:

```python
import os
import uuid
from collections.abc import AsyncIterator

import psycopg

MIGRATIONS = Path(__file__).resolve().parents[2] / "agent" / "src" / "db" / "migrations"


@pytest.fixture
async def agent_db() -> AsyncIterator[psycopg.AsyncConnection[Any]]:
    """A throwaway schema holding the agent's tables, from its own migration files."""
    url = os.environ.get("SCADBUDDY_TEST_DATABASE_URL")
    if not url:
        pytest.skip("SCADBUDDY_TEST_DATABASE_URL is not set")
    schema = f"durable_{uuid.uuid4().hex[:12]}"
    conn = await psycopg.AsyncConnection.connect(url, autocommit=True)
    try:
        await conn.execute(f'CREATE SCHEMA "{schema}"')
        await conn.execute(f'SET search_path TO "{schema}"')
        for sql in sorted(MIGRATIONS.glob("*.sql")):
            await conn.execute(sql.read_text())
        yield conn
    finally:
        await conn.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await conn.close()
```

If a migration file fails here because it needs an earlier ledger step (the agent's `migrations.ts` applies plain files in order under one transaction each), stop and report which. The plan assumes every file is plain SQL, as `migrations.ts:180` (`tx.unsafe(migration.sql)`) runs them.

- [ ] **Step 2: Write the failing tests**

`agent-durable/tests/test_credentials.py`:

```python
import base64
from typing import Any

import psycopg
import pytest

from scadbuddy_durable.credentials import (
    LEGACY_FORMAT_MESSAGE,
    credential_aad,
    credential_env,
    open_credential,
    usable_credentials,
)
from scadbuddy_durable.secrets import Envelope, SealError, kek_from_base64


def _envelope(v: dict[str, Any]) -> Envelope:
    return Envelope(
        secret_sealed=base64.b64decode(v["secret_sealed_b64"]),
        dek_sealed=base64.b64decode(v["dek_sealed_b64"]),
        kek_id=v["kek_id"],
    )


def test_aad_matches_json_stringify() -> None:
    assert (
        credential_aad("default", "anthropic_api_key", None)
        == 'ai_credentials:default:{"kind":"anthropic_api_key","base_url":null}'
    )
    assert credential_aad("x", "gateway", "https://h/é") == (
        'ai_credentials:x:{"kind":"gateway","base_url":"https://h/é"}'
    )


def test_opens_every_credential_vector(vectors: dict[str, Any]) -> None:
    kek = kek_from_base64(vectors["kek_b64"])
    for v in vectors["credentials"]:
        args = dict(id=v["id"], priority=v["priority"], kind=v["kind"], base_url=v["base_url"])
        if v["opens"]:
            assert open_credential(kek, **args, envelope=_envelope(v)).secret == v["plaintext"]
        else:
            with pytest.raises(SealError, match=LEGACY_FORMAT_MESSAGE):
                open_credential(kek, **args, envelope=_envelope(v))


def test_an_edited_base_url_fails_authentication(vectors: dict[str, Any]) -> None:
    kek = kek_from_base64(vectors["kek_b64"])
    v = next(c for c in vectors["credentials"] if c["kind"] == "gateway")
    with pytest.raises(SealError, match="failed authentication"):
        open_credential(
            kek, id=v["id"], priority=v["priority"], kind="gateway",
            base_url="https://attacker.example", envelope=_envelope(v),
        )


def test_env_per_kind(vectors: dict[str, Any]) -> None:
    kek = kek_from_base64(vectors["kek_b64"])
    got = {}
    for v in vectors["credentials"]:
        if v["opens"]:
            c = open_credential(
                kek, id=v["id"], priority=v["priority"], kind=v["kind"],
                base_url=v["base_url"], envelope=_envelope(v),
            )
            got[v["name"]] = credential_env(c)
    assert got["api key, migrated row id"] == {"ANTHROPIC_API_KEY": "sk-ant-api03-vector-0000000000000000"}
    assert got["oauth token"] == {"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-vector-1111111111111111"}
    assert got["gateway with a path"] == {
        "ANTHROPIC_BASE_URL": "https://gw.example.com/anthropic/v1",
        "ANTHROPIC_AUTH_TOKEN": "gw-token-vector-2222222222222222",
    }


def test_the_secret_never_appears_in_repr(vectors: dict[str, Any]) -> None:
    kek = kek_from_base64(vectors["kek_b64"])
    v = vectors["credentials"][0]
    c = open_credential(
        kek, id=v["id"], priority=v["priority"], kind=v["kind"],
        base_url=v["base_url"], envelope=_envelope(v),
    )
    assert v["plaintext"] not in repr(c)


async def _insert(conn: psycopg.AsyncConnection[Any], v: dict[str, Any], **over: Any) -> None:
    row = {
        "id": v["id"], "priority": v["priority"], "kind": v["kind"], "base_url": v["base_url"],
        "secret_sealed": base64.b64decode(v["secret_sealed_b64"]),
        "dek_sealed": base64.b64decode(v["dek_sealed_b64"]),
        "kek_id": v["kek_id"], "status": "active", "cooldown_until": None, **over,
    }
    await conn.execute(
        "INSERT INTO ai_credentials (id, priority, kind, base_url, secret_sealed, dek_sealed,"
        " kek_id, last4, status, cooldown_until)"
        " VALUES (%(id)s, %(priority)s, %(kind)s, %(base_url)s, %(secret_sealed)s,"
        " %(dek_sealed)s, %(kek_id)s, '', %(status)s, %(cooldown_until)s)",
        row,
    )


@pytest.mark.requires_postgres
async def test_usable_in_priority_order_skipping_what_cannot_be_used(
    agent_db: psycopg.AsyncConnection[Any], vectors: dict[str, Any]
) -> None:
    kek = kek_from_base64(vectors["kek_b64"])
    by = {v["name"]: v for v in vectors["credentials"]}
    # priority 0: disabled; 1: cooling down until the future; 2: cooled down already
    # (counts as active, Review Focus 3); 3: sealed under another key; 9: v1.
    await _insert(agent_db, by["api key, migrated row id"], status="disabled")
    await _insert(
        agent_db, by["oauth token"], status="cooling_down",
        cooldown_until="2999-01-01T00:00:00Z",
    )
    await _insert(
        agent_db, by["gateway with a path"], status="cooling_down",
        cooldown_until="2000-01-01T00:00:00Z",
    )
    await _insert(agent_db, by["gateway, non-ASCII host"], kek_id="0000000000000000")
    await _insert(agent_db, by["v1 credential (refused)"])
    usable = await usable_credentials(agent_db, kek)
    assert [c.id for c in usable] == [by["gateway with a path"]["id"]]


@pytest.mark.requires_postgres
async def test_an_edited_row_is_skipped_and_the_next_is_used(
    agent_db: psycopg.AsyncConnection[Any], vectors: dict[str, Any]
) -> None:
    kek = kek_from_base64(vectors["kek_b64"])
    by = {v["name"]: v for v in vectors["credentials"]}
    await _insert(agent_db, by["gateway with a path"], priority=0, base_url="https://attacker.example")
    await _insert(agent_db, by["oauth token"], priority=1)
    usable = await usable_credentials(agent_db, kek)
    assert [c.kind for c in usable] == ["claude_oauth_token"]
```

- [ ] **Step 3: Run to see them fail**

Run: `uv run --frozen pytest tests/test_credentials.py -v`
Expected: FAIL, `ModuleNotFoundError: scadbuddy_durable.credentials`.

- [ ] **Step 4: Implement**

`agent-durable/src/scadbuddy_durable/credentials.py`:

```python
"""The Claude credential, read as the agent reads it (spec 2026-10-01 §6.2).

Ports agent/src/credentials.ts `credentialAad` and `openCredential`, the pool's
"usable, in priority order" (`CredentialPool`, agent/src/harness/fallback.ts) and
agent/src/harness/run.ts `credentialEnv`. The agent alone writes `ai_credentials`:
health verdicts (cooldowns, disables) stay its job until 5c decides how a durable
segment reports one.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Literal, cast

import psycopg

from scadbuddy_durable.secrets import SEAL_V1, Envelope, Kek, SealError, open_secret, sealed_version

log = logging.getLogger(__name__)

CredentialKind = Literal["anthropic_api_key", "claude_oauth_token", "gateway"]

LEGACY_FORMAT_MESSAGE = (
    "the stored credential was saved in an older format that did not bind its kind and"
    " base URL; save it again"
)


@dataclass(frozen=True)
class Credential:
    id: str
    priority: int
    kind: CredentialKind
    base_url: str | None
    secret: str = field(repr=False)

    @property
    def label(self) -> str:
        """For logs: which credential, never its secret (credentials.ts `credentialLabel`)."""
        what = {"gateway": f"gateway {self.base_url or ''}", "claude_oauth_token": "OAuth token"}
        return f"credential {self.priority + 1} ({what.get(self.kind, 'API key')})"


def credential_aad(id: str, kind: str, base_url: str | None) -> str:
    # JSON.stringify: keys in this order, no spaces, non-ASCII left as is.
    bound = json.dumps({"kind": kind, "base_url": base_url}, separators=(",", ":"), ensure_ascii=False)
    return f"ai_credentials:{id}:{bound}"


def open_credential(
    kek: Kek,
    *,
    id: str,
    priority: int,
    kind: CredentialKind,
    base_url: str | None,
    envelope: Envelope,
) -> Credential:
    if sealed_version(envelope.secret_sealed) == SEAL_V1:
        raise SealError(LEGACY_FORMAT_MESSAGE)
    secret = open_secret(kek, envelope, credential_aad(id, kind, base_url))
    if kind == "gateway" and base_url is None:
        raise SealError("gateway credential has no base_url")
    return Credential(id=id, priority=priority, kind=kind, base_url=base_url, secret=secret)


def credential_env(c: Credential) -> dict[str, str]:
    if c.kind == "anthropic_api_key":
        return {"ANTHROPIC_API_KEY": c.secret}
    if c.kind == "claude_oauth_token":
        return {"CLAUDE_CODE_OAUTH_TOKEN": c.secret}
    assert c.base_url is not None
    return {"ANTHROPIC_BASE_URL": c.base_url, "ANTHROPIC_AUTH_TOKEN": c.secret}


async def usable_credentials(conn: psycopg.AsyncConnection[Any], kek: Kek) -> list[Credential]:
    """Every credential a query may use now, in priority order.

    Usable is the agent's rule: active, or cooling down with its cooldown passed;
    sealed under the mounted key; in the current format; and authenticating against
    its row. A row that fails any of those is skipped and logged by label.
    """
    cur = await conn.execute(
        "SELECT id, priority, kind, base_url, secret_sealed, dek_sealed, kek_id"
        " FROM ai_credentials"
        " WHERE status = 'active' OR (status = 'cooling_down' AND cooldown_until <= now())"
        " ORDER BY priority"
    )
    usable: list[Credential] = []
    for id, priority, kind, base_url, secret_sealed, dek_sealed, kek_id in await cur.fetchall():
        envelope = Envelope(secret_sealed=bytes(secret_sealed), dek_sealed=bytes(dek_sealed), kek_id=kek_id)
        if kek_id != kek.id:
            log.info("credential %d is sealed under another key; skipped", priority + 1)
            continue
        try:
            usable.append(
                open_credential(
                    kek, id=id, priority=priority, kind=cast(CredentialKind, kind),
                    base_url=base_url, envelope=envelope,
                )
            )
        except SealError as err:
            log.warning("credential %d cannot be opened (%s); skipped", priority + 1, err)
    return usable
```

`SealError` messages never carry plaintext (Task 3), so logging one is safe.

- [ ] **Step 5: Run the tests, with a Postgres**

Run:
```bash
docker run -d --rm --name pg-durable -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=scadbuddy_test -p 5433:5432 postgres:17
SCADBUDDY_TEST_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:5433/scadbuddy_test uv run --frozen pytest tests/test_credentials.py -v
docker rm -f pg-durable
```
Expected: all pass, the two `requires_postgres` tests included (not skipped).

- [ ] **Step 6: Lint, type-check, commit**

Run: `uv run --frozen ruff check . && uv run --frozen ruff format --check . && uv run --frozen mypy`

```bash
git add agent-durable/src/scadbuddy_durable/credentials.py agent-durable/tests/conftest.py agent-durable/tests/test_credentials.py
git commit -m "feat(agent-durable): read the Claude credential as the agent's pool does (#1056)"
```

---

### Task 5: The bundled-CLI check and the image stage

**Files:**
- Create: `agent-durable/src/scadbuddy_durable/check_cli_version.py`
- Create: `agent-durable/src/scadbuddy_durable/health.py`
- Create: `agent-durable/src/scadbuddy_durable/__main__.py`
- Test: `agent-durable/tests/test_check_cli_version.py`, `agent-durable/tests/test_health.py`
- Modify: `Dockerfile` (a new `agent-durable` stage after the `agent` stage, before `openscad-lsp`)

**Interfaces:**
- Produces: `def declared_cli_version() -> str`, `def bundled_cli_path() -> Path`, `def parse_cli_version(stdout: str) -> str`, `def main() -> int` in `check_cli_version`.
- Produces: `async def serve_health(host: str, port: int, status: Callable[[], dict[str, str]]) -> asyncio.Server` in `health`, which 5c reuses with the worker's status.
- Produces: `python -m scadbuddy_durable`, which serves `GET /healthz` on 8082 answering `{"status": "ok", "worker": "not started"}`.

- [ ] **Step 1: Write the failing tests**

`agent-durable/tests/test_check_cli_version.py`:

```python
import pytest

from scadbuddy_durable.check_cli_version import (
    MINIMUM,
    bundled_cli_path,
    declared_cli_version,
    parse_cli_version,
)


def test_parses_claude_codes_version_line() -> None:
    assert parse_cli_version("2.1.292 (Claude Code)\n") == "2.1.292"


@pytest.mark.parametrize("out", ["", "claude 2.1.292", "2.1 (Claude Code)"])
def test_refuses_anything_else(out: str) -> None:
    with pytest.raises(ValueError, match="unrecognised"):
        parse_cli_version(out)


def test_the_pinned_sdk_declares_its_cli_and_meets_the_plugins_floor() -> None:
    declared = declared_cli_version()
    assert declared == "2.1.292"
    assert tuple(map(int, declared.split("."))) >= MINIMUM


def test_the_bundled_binary_is_where_the_sdk_keeps_it() -> None:
    path = bundled_cli_path()
    assert path.name == "claude"
    assert path.parent.name == "_bundled"
```

`agent-durable/tests/test_health.py`:

```python
import asyncio
import json

from scadbuddy_durable.health import serve_health


async def _get(port: int, path: str) -> tuple[str, str]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(f"GET {path} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
    await writer.drain()
    raw = (await reader.read()).decode()
    writer.close()
    head, _, body = raw.partition("\r\n\r\n")
    return head.splitlines()[0], body


async def test_healthz_answers_the_status() -> None:
    server = await serve_health("127.0.0.1", 0, lambda: {"status": "ok", "worker": "not started"})
    port = server.sockets[0].getsockname()[1]
    try:
        line, body = await _get(port, "/healthz")
        assert line == "HTTP/1.1 200 OK"
        assert json.loads(body) == {"status": "ok", "worker": "not started"}
        line, _ = await _get(port, "/other")
        assert line == "HTTP/1.1 404 Not Found"
    finally:
        server.close()
        await server.wait_closed()
```

- [ ] **Step 2: Run to see them fail**

Run: `uv run --frozen pytest tests/test_check_cli_version.py tests/test_health.py -v`
Expected: FAIL, `ModuleNotFoundError`.

- [ ] **Step 3: Implement**

`agent-durable/src/scadbuddy_durable/check_cli_version.py`:

```python
"""Build-time assertion, run by the Dockerfile's `agent-durable` stage.

    python -m scadbuddy_durable.check_cli_version

Fails when the Python claude-agent-sdk's bundled Claude Code binary is missing for
this platform, prints an unrecognised --version, differs from the version the SDK
declares, or is older than the plugin's floor (ai-integrations#33 README: 2.1.273).
The version is pinned once, by `claude-agent-sdk==` in pyproject.toml, as the agent
pins its own (agent/src/check-cli-version.ts, #1540).
"""

from __future__ import annotations

import re
import subprocess
import sys
import tempfile
from pathlib import Path

import claude_agent_sdk
from claude_agent_sdk._cli_version import __cli_version__

MINIMUM = (2, 1, 273)
_LINE = re.compile(r"^(\d+\.\d+\.\d+\S*) \(Claude Code\)$", re.MULTILINE)


def declared_cli_version() -> str:
    return str(__cli_version__)


def bundled_cli_path() -> Path:
    return Path(claude_agent_sdk.__file__).parent / "_bundled" / "claude"


def parse_cli_version(stdout: str) -> str:
    match = _LINE.search(stdout.strip())
    if not match:
        raise ValueError(f"unrecognised claude --version output: {stdout!r}")
    return match.group(1)


def main() -> int:
    declared = declared_cli_version()
    path = bundled_cli_path()
    if not path.is_file():
        print(f"ERROR: no bundled Claude Code at {path} (declared {declared}).", file=sys.stderr)
        return 1
    with tempfile.TemporaryDirectory(prefix="claude-version-") as config_dir:
        out = subprocess.run(
            [str(path), "--version"],
            capture_output=True, text=True, timeout=60, check=False,
            env={"CLAUDE_CONFIG_DIR": config_dir, "HOME": config_dir, "PATH": "/usr/bin:/bin"},
        )
    try:
        actual = parse_cli_version(out.stdout)
    except ValueError as err:
        print(f"ERROR: {err} (declared {declared}).", file=sys.stderr)
        return 1
    if actual != declared:
        print(
            f"ERROR: claude-agent-sdk declares Claude Code {declared!r} but its binary reports"
            f" {actual!r}. Reinstall from agent-durable/uv.lock for this platform.",
            file=sys.stderr,
        )
        return 1
    if tuple(int(p) for p in actual.split(".")[:3]) < MINIMUM:
        print(f"ERROR: Claude Code {actual} is older than the plugin's floor {MINIMUM}.", file=sys.stderr)
        return 1
    print(f"Claude Code {actual} (bundled by claude-agent-sdk) is the version the SDK declares.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

`agent-durable/src/scadbuddy_durable/health.py`:

```python
"""GET /healthz, the one port this container opens (spec 2026-10-01 §6.3a)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable


async def serve_health(
    host: str, port: int, status: Callable[[], dict[str, str]]
) -> asyncio.Server:
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request = await asyncio.wait_for(reader.readline(), timeout=5)
            parts = request.decode("latin-1").split()
            if len(parts) >= 2 and parts[0] == "GET" and parts[1] == "/healthz":
                body = json.dumps(status()).encode()
                head = "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
            else:
                body = b'{"error":"not found"}'
                head = "HTTP/1.1 404 Not Found\r\nContent-Type: application/json\r\n"
            writer.write(f"{head}Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode() + body)
            await writer.drain()
        except (TimeoutError, ConnectionError):
            pass
        finally:
            writer.close()

    return await asyncio.start_server(handle, host, port)
```

`agent-durable/src/scadbuddy_durable/__main__.py`:

```python
"""python -m scadbuddy_durable: the agent-durable sidecar.

Phase 5a serves /healthz only. 5c starts the `agent` queue's worker here.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal

from scadbuddy_durable.health import serve_health

PORT = int(os.environ.get("SCADBUDDY_DURABLE_HEALTH_PORT", "8082"))


async def run() -> None:
    server = await serve_health("0.0.0.0", PORT, lambda: {"status": "ok", "worker": "not started"})
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    await stop.wait()
    server.close()
    await server.wait_closed()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run())
```

- [ ] **Step 4: Run the tests**

Run: `uv run --frozen pytest -v && uv run --frozen mypy && uv run --frozen ruff check . && uv run --frozen ruff format --check .`
Expected: all pass. If mypy reports `claude_agent_sdk._cli_version` as untyped, add `[[tool.mypy.overrides]] module = ["claude_agent_sdk.*"] ignore_missing_imports = false` only if the package ships `py.typed`; otherwise read the value through `importlib.import_module("claude_agent_sdk._cli_version").__cli_version__` typed as `str`.

- [ ] **Step 5: The Dockerfile stage**

Insert after the `agent` stage's `HEALTHCHECK` (before `# ── openscad-lsp`):

```dockerfile
# ── agent-durable: durable agent sessions on Temporal (spec 2026-10-01 §6.3a) ──
# A sidecar in the ScadBuddy pod beside `agent`, trusted like it (#1030). Python
# because the Claude Agent SDK's Temporal plugin is Python (ai-integrations#33,
# git-pinned in agent-durable/uv.lock). Plain psycopg against apt's libpq5, as
# the backend image does, so libpq fixes arrive with the OS.
FROM python:3.12-slim-bookworm AS agent-durable

# hadolint ignore=DL3008
RUN apt-get update \
    && apt-get install -y --no-install-recommends tini libpq5 git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY --from=uv /uv /usr/local/bin/

RUN groupadd --gid 10001 scadbuddy \
    && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /srv/agent --shell /usr/sbin/nologin scadbuddy \
    && install -d -o 10001 -g 10001 /srv/agent

WORKDIR /app/agent-durable
ENV UV_PROJECT_ENVIRONMENT=/app/agent-durable/.venv \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never
COPY agent-durable/pyproject.toml agent-durable/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY agent-durable/src ./src
RUN uv sync --frozen --no-dev

# ScadBuddy's own skills only, as the agent-build stage copies them (spec §6.3b).
COPY plugins/scadbuddy/skills /app/plugins/scadbuddy/skills

# Fails the build when the bundled Claude Code is missing for this platform or
# is not the version claude-agent-sdk declares (agent/src/check-cli-version.ts's twin).
RUN .venv/bin/python -m scadbuddy_durable.check_cli_version

ARG SCADBUDDY_REVISION=unknown
ARG SCADBUDDY_VERSION=dev
ENV PATH=/app/agent-durable/.venv/bin:$PATH \
    HOME=/srv/agent \
    CLAUDE_CONFIG_DIR=/srv/agent/claude \
    SCADBUDDY_REVISION=${SCADBUDDY_REVISION} \
    SCADBUDDY_VERSION=${SCADBUDDY_VERSION}

USER 10001:10001
EXPOSE 8082
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "scadbuddy_durable"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8082/healthz', timeout=4).status == 200 else 1)"]
```

`git` is needed because `uv sync` builds the git-pinned package. The repository has no `.dockerignore` rule for `agent-durable/`; check with `grep -n agent-durable .dockerignore` that nothing excludes it.

- [ ] **Step 6: Build and run it**

Run:
```bash
docker build --target agent-durable -t scadbuddy-agent-durable:dev .
docker run -d --rm --name ad --read-only --tmpfs /tmp --tmpfs /srv/agent:uid=10001,gid=10001 -p 18082:8082 scadbuddy-agent-durable:dev
sleep 3; curl -fsS http://127.0.0.1:18082/healthz; docker rm -f ad
hadolint --config .hadolint.yaml Dockerfile
```
Expected: the build log shows `Claude Code 2.1.292 (bundled by claude-agent-sdk) is the version the SDK declares.`; `{"status": "ok", "worker": "not started"}`; hadolint clean.

- [ ] **Step 7: Commit**

```bash
git add agent-durable/src/scadbuddy_durable agent-durable/tests Dockerfile
git commit -m "feat(agent-durable): image stage with a bundled Claude Code check, /healthz (#1056)"
```

---

### Task 6: CI — the `agent-durable` job, the summary, and the weekly pin check

**Files:**
- Modify: `.github/workflows/ci.yml` (`layout` outputs and detect step; new job `agent-durable`; `summary.needs` and its assertions)
- Create: `.github/workflows/agent-durable-pin.yml`

**Interfaces:**
- Consumes: Tasks 1–5.
- Produces: the job `agent-durable` (display name `Agent-durable lint, test, image`), gated by `needs.layout.outputs.agent_durable`.

- [ ] **Step 1: `layout` detects the tree**

In the `layout` job, add the output `agent_durable: ${{ steps.detect.outputs.agent_durable }}`, and in the detect script:

```bash
          agent_durable=false
          if [ -f agent-durable/pyproject.toml ]; then agent_durable=true; fi
```

and `echo "agent_durable=$agent_durable"` inside the `{ … } | tee -a "$GITHUB_OUTPUT"` block.

- [ ] **Step 2: The job**

After the `agent` job:

```yaml
  agent-durable:
    name: Agent-durable lint, test, image
    needs: layout
    if: ${{ needs.layout.outputs.agent_durable == 'true' }}
    runs-on: ubuntu-latest
    timeout-minutes: 20
    services:
      postgres:
        image: postgres:17
        env:
          POSTGRES_PASSWORD: postgres
          POSTGRES_DB: scadbuddy_test
        ports:
          - 5432:5432
        options: >-
          --health-cmd "pg_isready -U postgres"
          --health-interval 5s
          --health-timeout 5s
          --health-retries 12
    defaults:
      run:
        working-directory: agent-durable
    steps:
      - uses: actions/checkout@v7
        with:
          fetch-depth: 1

      - name: Set up uv
        uses: astral-sh/setup-uv@v10.2.0
        with:
          enable-cache: true
          cache-dependency-glob: agent-durable/uv.lock

      # The pin still resolves from temporalio/ai-integrations (spec §6.2): a
      # commit only a draft PR references can vanish. Runs on every PR, so a
      # change to uv.lock is checked where it is made.
      - name: Check the lock still resolves
        run: uv lock --check

      - name: Install
        run: uv sync --frozen

      - name: Lint
        run: |
          uv run --frozen ruff check .
          uv run --frozen ruff format --check .

      - name: Typecheck
        run: uv run --frozen mypy

      # Opens agent/test/fixtures/secret-vectors.json, which the agent job
      # regenerates and compares: a format change in agent/src/secrets.ts or
      # credentials.ts fails one job or the other.
      - name: Tests
        env:
          SCADBUDDY_TEST_DATABASE_URL: postgresql://postgres:postgres@127.0.0.1:5432/scadbuddy_test
        run: uv run --frozen pytest -v

      - uses: docker/setup-buildx-action@v4

      - name: Build the agent-durable image
        uses: docker/build-push-action@v7
        with:
          context: .
          target: agent-durable
          load: true
          tags: scadbuddy-agent-durable:ci
          cache-from: type=gha,scope=agent-durable
          cache-to: type=gha,scope=agent-durable,mode=min

      - name: Start it and check /healthz
        working-directory: .
        run: |
          set -euo pipefail
          docker run -d --name scadbuddy-agent-durable-ci --read-only \
            --tmpfs /tmp --tmpfs /srv/agent:uid=10001,gid=10001 \
            -p 18082:8082 scadbuddy-agent-durable:ci
          for _ in $(seq 1 30); do
            if curl --fail --silent http://127.0.0.1:18082/healthz >/dev/null; then break; fi
            sleep 1
          done
          body="$(curl --fail --silent --show-error http://127.0.0.1:18082/healthz)"
          echo "$body"
          jq -e '.status == "ok"' <<<"$body" >/dev/null

      - name: Tear down
        if: always()
        working-directory: .
        run: docker rm -f scadbuddy-agent-durable-ci || true
```

- [ ] **Step 3: The summary requires it**

Add `agent-durable` to `summary.needs` and `AGENT_DURABLE_RESULT: ${{ needs.agent-durable.result }}` to the assertion step's `env`. Next to `if [ -f agent/package.json ]; then has_agent=true; fi` (the step re-derives the layout from the tree, ci.yml ~1522), add:

```bash
          has_agent_durable=false
          if [ -f agent-durable/pyproject.toml ]; then has_agent_durable=true; fi
```

and after the `assert "Agent lint, typecheck, test, build, image" …` line:

```bash
          assert "Agent-durable lint, test, image"        "$AGENT_DURABLE_RESULT" "$has_agent_durable"
```

- [ ] **Step 4: The weekly pin check**

`.github/workflows/agent-durable-pin.yml`:

```yaml
# The agent-durable plugin pin is a commit that only an open draft PR
# (temporalio/ai-integrations#33) references, so a force-push or close can make
# it unreachable and GitHub may collect it (spec 2026-10-01 §6.2). Images already
# built keep the package; a vanished commit stops rebuilds. Remedy: a reviewed
# bump to the PR's new head, or to the PyPI release.
name: agent-durable pin

on:
  schedule:
    - cron: "17 6 * * 1"
  workflow_dispatch:

permissions:
  contents: read
  issues: write

jobs:
  check:
    runs-on: ubuntu-latest
    timeout-minutes: 10
    steps:
      - uses: actions/checkout@v7
        with:
          fetch-depth: 1
      - uses: astral-sh/setup-uv@v10.2.0
      - name: uv lock --check
        id: lock
        working-directory: agent-durable
        run: uv lock --check
      - name: Open an issue
        if: failure() && steps.lock.outcome == 'failure'
        env:
          GH_TOKEN: ${{ github.token }}
          RUN_URL: ${{ github.server_url }}/${{ github.repository }}/actions/runs/${{ github.run_id }}
        run: |
          set -euo pipefail
          title="agent-durable: the ai-integrations pin no longer resolves"
          if gh issue list --state open --search "in:title \"$title\"" --json number --jq '.[0].number' | grep -q .; then
            echo "already open"; exit 0
          fi
          gh issue create --title "$title" --label dependencies --body \
            "The weekly \`uv lock --check\` in \`agent-durable/\` failed: $RUN_URL. The pinned commit of temporalio/ai-integrations#33 may have been force-pushed away. Running images are unaffected; rebuilds fail. Bump the pin to the PR's current head (or the PyPI release) in a reviewed PR that carries the diff of \`python/claude_agent_sdk\` (spec 2026-10-01 §6.2)."
```

- [ ] **Step 5: Lint the workflows**

Run: `actionlint .github/workflows/ci.yml .github/workflows/agent-durable-pin.yml`
Expected: no findings.

- [ ] **Step 6: Commit**

```bash
git add .github/workflows/ci.yml .github/workflows/agent-durable-pin.yml
git commit -m "ci: the agent-durable job, its summary gate and the weekly pin check (#1056)"
```

---

### Task 7: CLAUDE.md, then the PR

**Files:**
- Modify: `CLAUDE.md` (Commands; Layout)

- [ ] **Step 1: Document the commands**

Under "## Commands (what CI runs)", after the agent block:

````markdown
Durable sessions (`agent-durable/`, Python 3.12, uv; the `agent-durable` CI job, #1056):

```bash
cd agent-durable
uv lock --check                   # the ai-integrations#33 pin still resolves
uv run --frozen ruff check . && uv run --frozen ruff format --check .
uv run --frozen mypy
uv run --frozen pytest            # requires_postgres tests need SCADBUDDY_TEST_DATABASE_URL
docker build --target agent-durable -t scadbuddy-agent-durable:dev .   # checks the bundled Claude Code
```

`agent/test/fixtures/secret-vectors.json` is written by `agent/test/secretVectors.test.ts`
(`UPDATE_SECRET_VECTORS=1`) and opened by `agent-durable/tests`: a change to
`agent/src/secrets.ts`'s format or `credentials.ts`'s AAD needs new vectors, and the port must open them.
The plugin pin moves only in its own PR, which carries the diff of `python/claude_agent_sdk`
between the two SHAs; `agent-durable-pin.yml` checks weekly that it still resolves.
````

- [ ] **Step 2: Document the layout**

In "## Layout", after the `agent/` entry:

```markdown
- `agent-durable/` — durable agent sessions (#1056, spec 2026-10-01 §6), Python on
  `temporalio-claude-agent-sdk` (ai-integrations#33, git-pinned by SHA in `uv.lock`, never
  vendored), shipped as the Dockerfile's `agent-durable` target, a sidecar trusted like
  `agent`. `secrets.py`/`credentials.py` open `ai_credentials` as the agent does (open
  only; the agent alone writes it); `check_cli_version.py` is the build's bundled-CLI
  check (Python `claude-agent-sdk` pinned exactly; it bundles a Claude Code of its own,
  not the TypeScript SDK's). Phase 5a serves `/healthz` on 8082 only; the worker is 5c.
```

- [ ] **Step 3: Run every suite this plan touched**

Run:
```bash
cd agent && pnpm lint && pnpm typecheck && pnpm test
cd ../agent-durable && uv lock --check && uv run --frozen ruff check . && uv run --frozen mypy && uv run --frozen pytest
cd .. && actionlint && hadolint --config .hadolint.yaml Dockerfile
```
Expected: all green. `requires_postgres` tests run when `SCADBUDDY_TEST_DATABASE_URL` is set.

- [ ] **Step 4: Commit and open the PR**

```bash
git add CLAUDE.md
git commit -m "docs: agent-durable commands and layout (#1056)"
git push -u origin HEAD
gh pr create --title "feat(agent-durable): the package, its pin, the credential port and image (#1056, phase 5a)" --body-file <body>
```

The body links #1056 ("Part of #1056", not "Fixes": 5b–5e follow), names the pin SHA, states the gate result and the CLI-version note from "What changed since the spec", and ends with the session's attribution lines.
