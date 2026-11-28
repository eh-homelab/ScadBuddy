# Filament Presets View Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A Presets view under Filament, with a route and an agent tool for each action, over ScadBuddy's own filament presets (#7204).

**Architecture:** Two phases in one plan (R1). **Phase A (buildable now):** Go core (`tools/go/core`) serves three read routes in a new package `internal/filamentpresets`, area `filament-presets` (N1). It reads `filament_presets`, `filament_preset_versions` and each document's blob through `blobstore.OpenKey` of #8129 (N3). It strips print-host credential keys as Python does, and shared vectors hold the two equal (N4). The frontend gets a Presets tab and a read-only preset page. The agent gets three read tools. **Phase B (after `internal/temporalx` merges):** three write routes in the same package. Each checks its body and caller, then runs a Python Temporal activity on queue `library` (copy, edit, send again). The activity writes with the helpers of `sb_presets.py` and runs the Python mirror (R2). The frontend and the agent get the three writes. Python serves none of these paths. Go serves them only after eh-homelab/clusters names the area (N6).

**Tech Stack:** Go (`tools/go/core`: pgx, `internal/auth`, `internal/blobstore`, `internal/jsonv`, `testdb`; Phase B: `internal/temporalx`); Python 3.12, psycopg 3, temporalio (the Phase B activities, the vector writer); TypeScript, zod, `defineTool` (agent); React 19, msw, vitest (frontend).

**Spec:** issue #7204 (`gh issue view 7204`), and the presets design it follows:
`docs/superpowers/plans/2026-11-23-scadbuddy-filament-presets.md` (Decisions 2, 3, 4 and 11).
The Go rulings rest on `docs/superpowers/specs/2026-11-25-go-service-migration-design.md`
(§1.4, §4 "Switch", §7) and on the owner's policy of #7667. The worked pattern is the #7143
plan on PR #8123 (`docs/superpowers/plans/2026-11-23-unknown-spool-flow.md`, rulings G1 to
G17 and R4) and its package `tools/go/core/internal/unknowntrays`.

**PRs:**

- **Phase A** is one PR now: `feat(filament): a read-only Presets view, served by Go core (#7204)`, Tasks A1 to A7. Its body says `Refs #7204`. Task A8 is a PR in eh-homelab/clusters after the image is deployed.
- **Phase B** is one PR after `internal/temporalx` (inventory K1 layer 4) merges: `feat(filament): copy, edit and send again a preset, through library activities (#7204)`, Tasks B1 to B6. Its body says `Fixes #7204`. Its Python half (B1, B2) may go first in a PR of its own (N7).

## Global Constraints

- **Only the API process and `tools/go/core` touch the database** (root `CLAUDE.md` "Database access", #5295 and the #7667 exception). No new database client, no cross grant. The Phase B activities run in the API process, on the `library` worker that it serves (`backend/scadbuddy/workflows/housekeeping.py:662` `library_worker`; the activity list in `backend/scadbuddy/main.py` near :2365).
- **New routes go in Go** (owner, #7667). Python changes only as Temporal activities or as edits to existing Python code. Python has no handler for a route of this plan.
- **Routes**, all in `internal/filamentpresets`, all public (`auth.Public`, the caller from `auth.CallerOf(ctx)`):
  - Phase A: `GET /api/v1/filament/presets`, `GET /api/v1/filament/presets/{preset_id}`, `GET /api/v1/filament/presets/{preset_id}/diff`.
  - Phase B: `POST /api/v1/filament/presets/{preset_id}/copy`, `PATCH /api/v1/filament/presets/{preset_id}`, `POST /api/v1/filament/presets/{preset_id}/mirror`.
  - `POST /api/v1/filament/presets/bambuddy-import` stays Python's (`backend/scadbuddy/api/filament.py:3397`). The Go patterns name a method, so that POST still goes on to Python.
- **Problem codes** go in the extension `code`, as the Python filament routes write it (`api/filament.py:3360`, `code="instance_changed"`): `preset_not_found` (404), `version_not_found` (404), `document_missing` (409), `document_too_large` (409), `document_unreadable` (409), `store_not_configured` (503), `store_unavailable` (503). Phase B adds `not_manual` (409), `version_moved` (409, extension `current_version`), `key_refused` (422, extension `key`), `mirror_off`, `not_configured`, `instance_changed`, `not_mirrored` (409), `preset_write_pending` (503), `writes_not_configured` (503: core has no Temporal address, P22), `idempotency_key_reused` (422, problem `type` `https://scadbuddy.dev/problems/crud/idempotency-key-reused`, Python's `crud/idempotency.py` `KEY_PROBLEM`: the key came again with another body), and `invalid_input` (422: an activity input that is not a copy or an edit, for a reason other than a key; `filament/preset_writes.py` `INVALID_INPUT`). Use these strings everywhere: Go, mocks, frontend, agent.
- **A preset's document is a blob.** `filament_presets.blob_key` is `attachment-<sha256 of the document>` (`orders/attachments.py:120` `blob_key`). The blob is a zip of a directory, and its member `file` is the JSON (`orders/attachments.py:42` `BLOB_FILE`, `store/archive.py:31` `pack_dir`, `store/cache.py:66` `_member`). Never bytea, never base64. Never a second S3 client: the read is the one of `internal/blobstore` (N3).
- **A document that the API shows has no print-host credential keys**: `core/credentials.py:28` `is_credential` (`CREDENTIAL_KEYS` and `_CREDENTIAL_PARTS`, :24-25) and `:34` `strip_credentials` (nested objects and lists too, order kept), as `FilamentPresetStore.read` does (`filament/sb_presets.py:422`). A document that is not a JSON object reads as `{}` in Python; Go answers `document_unreadable` (superseded by P8, see "Decisions made while building").
- **Phase B writes only through Python**: `sb_presets.keep_document` (:162), `lock_presets_on` (:177), `create_on` (:186) and `version_on` (:245), in the transaction of the activity. Go writes no row of `filament_presets`, `filament_preset_versions` or `blob_refs`, and Go never calls Bambuddy.
- No allowlist of slicer keys (#2998). Any Bambu Studio key may be set.
- A mirror failure never fails the write that asked for it (Decision 11).
- Auth: every route goes through `gate.Wrap` (#8053; `tools/go/core/internal/auth/check.go:160` `Public`, `:196` `CallerOf`, `:205` `HasScope`), never a check of its own. Prod never runs `SCADBUDDY_AUTH_MODE=enforce`.
- Go-served routes have no OpenAPI entry, so `pnpm gen:api` writes no types for them. The frontend and agent types are hand-written (N8). An agent tool over them has `routes: []`, as `filament_unknown_trays` of #7143 has: the test "has no stale entries" of `agent/test/coverage.test.ts` fails on a route that is not in `backend/openapi.json`. Never commit `backend/openapi.json` or a `schema.d.ts`.
- UI tasks load the `frontend-design:frontend-design` skill first.
- **No local test runs** beyond the touched frontend files (the owner's note; there is no local Postgres). Locally, run only format, lint, vet, build, typecheck and the vitest of touched frontend files. CI runs every other test. Write each test before its code. A "run" step type-checks or builds the test, and the CI run of the PR shows it green.
  - Go is not on PATH here. Use the pinned image (`tools/go/core/Dockerfile:7`), from the repo root:
    `docker run --rm -u "$(id -u):$(id -g)" -e GOTOOLCHAIN=local -e GOCACHE=/tmp/gocache -e GOFLAGS=-buildvcs=false -v "$PWD":/src -w /src/tools/go/core golang:1.26.9-bookworm@sha256:d9c68c2c51161e12fd77e4c6320687c9cd86e1af1e3ad6e6cd63ff970641453c sh -c 'test -z "$(gofmt -l .)" && go vet ./... && go build ./...'`
  - backend: `cd backend && uvx uv@0.13.10 run --frozen ruff check . && uvx uv@0.13.10 run --frozen ruff format --check . && uvx uv@0.13.10 run --frozen mypy` (uv as `Dockerfile:38` pins it).
  - agent: `cd agent && pnpm gen:api && pnpm lint && pnpm typecheck`.
  - frontend: `cd frontend && pnpm gen:api && pnpm lint && pnpm typecheck && pnpm exec vitest run <touched test files>` (root `CLAUDE.md` "Generated API files": `gen:api` first).
- Commit titles are conventional and end with `(#7204)`. Agent-authored docs cite their sources (#259). No counts in docs.

## Rulings

Each reads `Ruling: <what> — <why> — <cost if wrong>`.

### Changed by the Go policy

- **R1.** Ruling: every route of this plan is a Go route in `tools/go/core/internal/filamentpresets`. Phase A (the reads) ships now, and Phase B (the writes) ships after `internal/temporalx` — the owner's policy of #7667 puts new routes in Go. Go core has public routes (#8053) and a blob store (#7961) on main, so the reads need no Python. The writes need `sb_presets.lock_presets_on`, `version_on`, `create_on`, `keep_document` and the Python mirror, so they run as Python activities. Go has no Temporal client on main (no `internal/temporalx`, and no `go.temporal.io` in `tools/go/core/go.mod` at 3b53cf43c). The old R1 (Python routes) is withdrawn, as the issue's comment of 2026-11-26 18:04 says. That comment also said that Go writes the rows under the same lock. This plan replaces that with Python activities: a Go copy of `create_on` and `version_on` beside the Python one is the anti-goal of spec §1.4, "two writers with different invariants on one table". Thus Go also needs no in-process blob put (the comment of 18:07, item b) — cost if wrong: the writes wait for K1 layer 4, and a person sees a read-only view until then.
- **R2.** Ruling: the mirror stays Python's. It runs in process, in the Phase B activity, after the commit of the write (`preset_mirror.py:362` `mirror_pending`, `:284` `mirror_with`) — it runs that way today after a profile fetch (`profile_fetch.py:936`) and after the pull (`preset_mirror.py:384` `mirror_after_pull`), under one session try-lock (`preset_mirror.py:73` `LOCK`). The activity runs in the API process, so the lock keeps one set of users — a slow Bambuddy holds the activity for at most the bounds of R11.
- **R10.** Ruling: `NOT_NUDGED` (`api/filament.py:245`) does not change — `nudge_after_write` (`api/filament.py:284`) is a dependency of the Python filament router and never runs for a Go route. The activities nudge nothing, because the filament sync pushes bindings, not preset documents — none.
- **R11.** Ruling: the copy and edit activities bound the mirror by `WRITE_MIRROR_TIMEOUT = 30.0` s, and the send-again activity bounds it by `RESEND_TIMEOUT = 60.0` s. A timeout answers the outcome `pending`. The start-to-close of each activity is its bound plus 30 s, with no retry. The Go route waits for the result for at most that plus 10 s, then answers 503 `preset_write_pending` ("send the same Idempotency-Key again") — the profile fetch already bounds its mirror (`IMPORT_MIRROR_TIMEOUT`, `profile_fetch.py:121`), and the next pull sends what is left (`_pending` takes each preset whose `mirrored_sha256` is not its `sha256`, `preset_mirror.py:97`) — a cancelled create leaves `mirror_create_floor`, which the next send already handles.
- **R12.** Ruling: copy and edit need an `Idempotency-Key`. Go refuses a request without one with 428 and the type `https://scadbuddy.dev/problems/crud/idempotency-key-required` before it starts anything. The activity runs its write through `crud/idempotency.py:152` `idempotent_write(conn, "filament", f"{actor}\x1f{key}\x1f{preset_id}", ROUTE, asked, run)` in the transaction of the write, as `POST /filament/spools/bulk` does (`api/filament.py:1342`). The workflow id is `filament-preset-<op>-<sha256 of the actor, the key, the preset id and the JSON body that Go sends the activity>` (P24), so two sends of one key and one body at the same time join one run, and one key with another body starts its own run. Send again needs no key. The old plan said 422, but `required_key` answers 428 (`crud/idempotency.py:65-73`) — a lost reply must not make a second copy or a second version, and a repeated send is harmless — cost if wrong: a client that sends no key gets 428; a stored reply lasts `RETENTION` (24 h, `crud/idempotency.py:49`).

### Kept from the old plan

- **R3.** Ruling: only a `manual` preset can be edited; a `3dfp` or `bambuddy` preset is copied first — a re-fetch versions a `3dfp` preset over an edit (Decision 3), and a Bambuddy refresh versions a `bambuddy` one (Decision 4) — the person needs one more click.
- **R4.** Ruling: a copy can come from any source. It keeps `type_id`, `printer_model` and `nozzle_diameter`, and leaves out `tdfp_id`, `fetch_run_id`, `bb_*` and the mirror columns — the copy is a new preset that the mirror creates in Bambuddy as its own — none (the partial unique index `filament_presets_fetched` covers only `3dfp`, `migrations/20261123T0711Z_filament_presets.sql`).
- **R5.** Ruling: an edit is a patch (`name`, `set`, `unset`) with a required `expected_version`, not a whole document — the agent and the UI change few of about 150 keys, and a stale writer gets 409 `version_moved` — a full replace needs every key sent.
- **R6.** Ruling: copy and edit write the row's name into the document's `name` key — a rename is then always a new document and a new version, and Bambuddy, the native slicer and the row agree — a document whose `name` key differed from its row changes on its first edit.
- **R7.** Ruling: a value in `set` is a string or a list of strings. A key matches `^[A-Za-z0-9_.-]{1,128}$`. `name` (use the `name` field) and credential keys (`is_credential`) are refused with 422 `key_refused`. At most 500 keys go in `set` plus `unset`. Go checks these before it starts the activity, and the pydantic model of the activity checks them again — Bambu Studio's config values are strings or string lists — a non-string value is refused.
- **R8.** Ruling: the copy, edit and send-again tools are `outward`; the three reads are `read` — copy and edit start a mirror write to Bambuddy, as `filament_type_fetch_profiles` does, which is `outward` (`agent/src/tools/filament.ts:567`) — each such call needs an approval.
- **R9.** Ruling: the mirror state is computed in Go, not stored: `bambuddy_owned` (source `bambuddy`); `failed` (`mirror_error` set and `mirrored_sha256` is not `sha256`); `pending` (`mirrored_sha256` is not `sha256`, no error); `sent` (else; a `mirror_error` then is the lost-create note, shown as a note) — the columns already hold it — none.
- **R13.** Ruling: the list is the tab `/filament/presets` (after Types). Each preset has its page `/filament/presets/:id`. Agent answers carry its `ui_path` (link kind `preset`) — the Types tab and the type page are the pattern (#6869) — none.
- **R14.** Ruling: the list has no paging. It filters by `source` and `type_id`, and it is in the order `lower(name)`, then `id` — there is about one preset per type, printer model and nozzle — a very large site gets one long response.
- **R15.** Ruling: the detail holds the document of the current version. An older version is seen through the diff only — the edit form needs the current one, and the diff covers the rest — no route shows an old document whole.
- **R16.** Ruling: `docs/features/filament-parity.md` does not change — its rows are the parity list of spec §11, which has no preset-management item, so no row becomes false — none.

### New

- **N1.** Ruling: the area has one name, `filament-presets`, for every route of both phases. It has no `filament-presets:read` form — Python serves none of these paths, and the core README says to name a part of an area "only where Python still runs" (`tools/go/core/README.md`, "An area Go answers in part…"). The README's own example already names `filament-presets` ("Public routes"). The issue's comment of 2026-11-26 18:04 named both forms; one name replaces it. The Phase B writes go live when their image deploys, with no second switch — cost if wrong: the writes cannot be turned off apart from the reads; to drop the name turns all six off, and the window of N5 comes back.
- **N2.** Ruling: a `case filamentpresets.Area:` in `newRouter` of `main.go` adds the public routes, beside `case blobstore.Area:` (`main.go:342`). They do not go through `publicPackages` (`main.go:262`). The case builds `blobstore.New(pool, keys, lookupEnv, tel)`. It stops the start when `keys` is nil, because the store's secret key is sealed (`blobstore/settings.go:48`). It puts each route on the same `public` mux, behind the same `gate.Wrap`, and sets `served = true` — `publicAreas` passes only `(pool, keys)` (`main.go:259`), and the blob read needs `lookupEnv` and `tel`, as the blobs area does. It is no second mount — cost if wrong: a reviewer who wants `publicAreas` to carry them moves the lines, and `unknowntrays.Public` of #7143 then needs the same signature.
- **N3.** Ruling: the blob read is the shared one of #8129 (PR #8130): `blobstore.New(pool, keys, lookupEnv, tel)` and `(*Blobs).OpenKey(ctx, key) (io.ReadCloser, Ref, error)`, with `ErrBlobNotFound`, `ErrNotConfigured` and `ErrBlobMismatch` (at the end of the stream; size and SHA-256 checked as it reads). This plan adds nothing to `internal/blobstore`. The unzip of member `file` and the 1 MiB cap (N10) are in `internal/filamentpresets` (`document.go`): a `Ref.Size` over the cap is `document_too_large` before any GET; else the stream is read whole (it is at most the cap), then `archive/zip` gives member `file`. A missing row or object changes no row; Python's read forgets the row (`store/content.py:560`) — 18fe662e's two ports read through the same `OpenKey` (fleet chat epic-7667-go-core #366-#372), so core has one read, and a read must not write — cost if wrong: the row of a lost object stays until the next Python read or sweep; the detail still answers `document: null`.
- **N4.** Ruling: `backend/tests/filament/test_filament_preset_vectors.py` writes `backend/tests/fixtures/filament-preset-vectors.json` with `UPDATE_FILAMENT_PRESET_VECTORS=1`, as `auth-vectors.json` is written. It uses Python's own `is_credential` and `strip_credentials`, and the rule of `_changed_keys` (`profile_fetch.py:1118`, `sorted(k for k in {*old, *doc} if old.get(k) != doc.get(k))`). The Go tests read the file. `.github/scripts/select-go.sh:8` and `select-go.test.sh` name it — two languages stay equal only through shared vectors, and `select-go.sh` names each vector file, so the `go` job skips a change to an unnamed one — cost if wrong: a hand-edited vector passes both sides; the Python test fails on a stale file when the variable is not set.
- **N5.** Ruling: before the switch-on (N6), every path of this plan goes on to Python, which has no handler: a 404 with no `code`. The frontend tells that apart from a 404 with a `code`. The Presets tab and the preset page show "Presets are not on yet on this server" and nothing else. A `sb-<id>` link in `PresetTable` still shows. The agent tools answer a `ToolError` that names the area. This is R4 of #7143 — the image with the routes deploys before the clusters change names the area — cost if wrong: one branch of copy to remove after the switch.
- **N6.** Ruling: the switch-on is Task A8, in eh-homelab/clusters (`scadbuddy.yaml`, `SCADBUDDY_CORE_AREAS` on the `scadbuddy-core` container). It merges only after the image with the area is deployed, and it names `filament-presets` only — core stops its start on an area name that it does not know (`newRouter`'s default case in `main.go`: "which this build does not have"), as G16 of #7143 says — cost if wrong: the API pod crash-loops on the old image.
- **N7.** Ruling: the Python activities of Phase B (B1, B2) may merge before `temporalx`, with their tests. The Go routes (B3) and the UI and agent writes (B4, B5) wait for it — the activities are plain Python on the `library` worker, and nothing reaches them until Go starts them — cost if wrong: unused activity code on main for a time.
- **N8.** Ruling: the types for the Go routes are hand-written: `frontend/src/api/filamentPresets.ts` and `agent/src/api/filamentPresets.ts` (a `paths` fragment over the same `backend` client, as `agent/src/api/unknownTrays.ts` on PR #8123). `test-fixtures/filament/preset-wire.json` holds one list row, one detail and one diff as Go writes them. The Go, frontend-mock and agent tests check their key sets against it — `pnpm gen:api` exports only Python's OpenAPI — cost if wrong: types drift from Go; the tests of the fixture catch a changed key set, not a changed value type.
- **N9.** Ruling: the diff compares two values by their JSON text (`jsonv.Dumps`), not by Python's `!=` — Go has no Python equality. Bambu Studio's values are strings and string lists, where the two agree, and the vectors use only such values — cost if wrong: `1` against `1.0` shows as a change in Go; the README's "Where it differs from Python" says so.
- **N10.** Ruling: the document read is capped at 1 MiB (`DocumentLimit = 1 << 20`). A larger blob is `document_too_large`. A JSON that does not parse is `document_unreadable` — a 3DFP preset document is tens of KiB, and Python reads with no cap — cost if wrong: a larger document shows no document; the cap is one constant.

## Review Focus

1. A version whose blob the store lost, or a store that is not `s3`: the detail answers `document: null` with `document_error` set. The diff answers 409 `document_missing` or 503 `store_not_configured`. The page shows a message, not a crash. Tests: `TestDetailOfALostBlobHasNoDocument` and `TestDiffRefusesAnUnknownVersionAndALostBlob` (Task A3), `shows a message when a version's document is missing` (Task A6).
2. A document with `printhost_apikey`, `my_token` or a nested `password` key: the detail and the diff never show it, in Go exactly as in Python. Tests: `test_the_vectors_are_pythons` (Task A1), `TestStripCredentialsMatchesPython` (Task A3).
3. The area is not switched on: every path is a 404 with no `code`. The tab, the page and the agent say that the view is not on, and nothing crashes. Tests: `TestAnUnswitchedAreaForwardsToNext` (Task A3), `says presets are not on when the area is off` (Task A5), `says the presets routes are off on a bare 404` (Task A4).
4. Two edits of one preset at the same time (two tabs, or a person and the agent): the second gets 409 `version_moved` with `current_version`, and no edit is lost. Tests: `test_a_stale_edit_is_refused` (Task B1), `TestEditMapsEachFailureType` (Task B3).
5. A copy sent again with the same `Idempotency-Key` after a lost reply, or after a 503 `preset_write_pending`: one preset, the same id. Tests: `test_a_repeated_copy_key_makes_one_preset` (Task B1), `TestCopyNeedsAKey` and `TestCopyJoinsARunWithTheSameKey` (Task B3).

---

## Phase A (buildable now; one PR)

### Task A1: The shared vectors, and the rulings on the issue

**Files:**

- Create: `backend/tests/filament/test_filament_preset_vectors.py`
- Create: `backend/tests/fixtures/filament-preset-vectors.json` (written by the module)
- Modify: `.github/scripts/select-go.sh` (the pattern at line 8: `filament-preset` beside `event-stream|auth|plugin-seal|blob-upload`, and `test-fixtures/filament/preset-wire\.json$`), `.github/scripts/select-go.test.sh` (one case for each path)

**Interfaces:**

- `build() -> dict[str, Any]` (the cases, with no database) and `main()` (writes the file), so `python -m tests.filament.test_filament_preset_vectors` writes it with no test run.
- The file: `{"credential_keys": [{"key": str, "credential": bool}], "documents": [{"in": object, "out": object}], "diffs": [{"old": object, "new": object, "changed": [str]}]}`. `out` is `strip_credentials(in)`. `changed` is the rule of `_changed_keys` over `strip_credentials` of both documents.

- [ ] **Step 1: Post the rulings on the issue.** Comment R1, R12 and N1 on #7204: what changed against the comments of 2026-11-26 18:04 and 18:07, and why. Add the session's owner marker. Issues are the durable log.
- [ ] **Step 2: Write the failing test** `test_the_vectors_are_pythons`. The cases: the keys `printhost_apikey`, `PrintHost_Password`, `api_token`, `secret_sauce`, `nozzle_temperature`, `print_host` and `""`; documents with nested objects, lists of objects and a credential key at depth 3; diffs with a changed, an added and a removed key, a list value, and a credential key that differs only in the old document. With `UPDATE_FILAMENT_PRESET_VECTORS=1` the test writes the file (`json.dumps(…, indent=2, ensure_ascii=False)` plus a newline). Without it, the test asserts that the file equals what `build()` makes now.
- [ ] **Step 3: Write the file.** Run `cd backend && uvx uv@0.13.10 run --frozen python -m tests.filament.test_filament_preset_vectors`. Expected: the file exists; `git diff --stat` shows it.
- [ ] **Step 4: Add the paths to `select-go.sh` and its test.** Run `bash .github/scripts/select-go.test.sh`. Expected: pass.
- [ ] **Step 5: Lint** (the backend line of Global Constraints). Expected: clean.
- [ ] **Step 6: Commit** — `test(filament): shared vectors for a preset's credential keys and diff (#7204)`.

### Task A2: (folded into A3) The blob read is #8129

PR #8130 (#8129) adds `blobstore.New` and `(*Blobs).OpenKey` (N3). Phase A starts after it merges; cut the branch from main then. No task here: A3's `document.go` unzips and caps the document.

### Task A3: The read routes (`internal/filamentpresets`)

**Files:**

- Create: `tools/go/core/internal/filamentpresets/filamentpresets.go` (`Area`, the paths, `Public`, the problem map), `list.go`, `detail.go`, `diff.go`, `credentials.go`, `mirror.go`, `document.go` (N3: the cap and the unzip of member `file`)
- Modify: `tools/go/core/main.go` (`newRouter`: `case filamentpresets.Area:`, N2), `tools/go/core/README.md` (the area, its routes, the blob read, and two lines under "Where it differs from Python": N9 and N10)
- Create: `test-fixtures/filament/preset-wire.json` (N8)
- Test: `tools/go/core/internal/filamentpresets/vectors_test.go`, `list_test.go`, `detail_test.go`, `diff_test.go`; `tools/go/core/filament_presets_test.go` (through `newRouter`)

**Interfaces:**

- `const Area = "filament-presets"`. Paths: `listPath = "/api/v1/filament/presets"`, `detailPath = listPath + "/{preset_id}"`, `diffPath = detailPath + "/diff"`.
- `type Blobs interface { OpenKey(ctx context.Context, key string) (io.ReadCloser, blobstore.Ref, error) }` (superseded by P7: the built interface also has `Size`) (met by `*blobstore.Blobs`); `const DocumentLimit = 1 << 20`.
- `func readDocument(ctx context.Context, blobs Blobs, key string) (jsonv.Object, string, error)`: the document and an empty `document_error`, or none and one of `document_missing` (`ErrBlobNotFound`, or no member `file`), `document_too_large` (`Ref.Size` over the cap), `document_unreadable` (`ErrBlobMismatch`, a bad zip, or JSON that is not an object), `store_not_configured` (`ErrNotConfigured`) or `store_unavailable` (any other error). Tests: `TestReadDocumentGivesMemberFile`, `TestReadDocumentMapsEachError` (a table over a fake `Blobs`), `TestReadDocumentOverTheCapSendsNoGet`.
- `func Public(pool *pgxpool.Pool, blobs Blobs) []auth.Public`: each is `auth.Public{Pattern: "GET " + path, Route: auth.Route{Class: "api", Template: path}, Handler: …}`. The answers carry `Cache-Control: no-store`.
- `func IsCredential(key string) bool` and `func StripCredentials(doc jsonv.Object) jsonv.Object` (over the values of `jsonv.Parse`, order kept).
- `func MirrorState(source, sha256 string, mirroredSHA256, mirrorError *string) string` (R9).
- `func ChangedKeys(old, new jsonv.Object) []KeyChange` (sorted by key; N9).
- Wire shapes (the JSON keys; `test-fixtures/filament/preset-wire.json`):
  - `Row`: `id` (a number), `ref` (`sb-<id>`), `name`, `source`, `version`, `type_id` (a UUID or null), `printer_model`, `nozzle_diameter`, `filament_type`, `filament_colour`, `compatible_printers` (a list), `sha256`, `bb_preset_id` (a number or null), `bb_instance`, `mirror` (R9), `mirror_error`, `mirror_error_at`, `used_by_types`, `used_by_spools`, `created_at`, `updated_at`. No `blob_key`.
  - `Detail`: `Row`, plus `versions` (newest first: `version`, `sha256`, `made_by`, `made_at`, `fetch_run_id`, `bb_updated_at`), `document` (an object or null) and `document_error` (null, `document_missing`, `document_too_large`, `document_unreadable`, `store_not_configured` or `store_unavailable`).
  - `Diff`: `preset_id`, `from_version`, `to_version`, `changes` (`key`, `before`, `after`; null is absent).
- `GET …/presets?source=&type_id=`: `source` is one of `3dfp`, `bambuddy` and `manual`, else 422 (`httpx.Invalid`, `loc` `["query", "source"]`). `type_id` is a UUID (`crud.IsUUID`), else 422. `used_by_types` and `used_by_spools` count the bindings by their generated `preset_id` columns (`filament_spool_type_presets`, `filament_spool_presets`; `sb_presets.py:75` `BINDINGS`), with one `LEFT JOIN LATERAL` each.
- `GET …/{preset_id}`: a `preset_id` that is no positive int64 is 404 `preset_not_found`. Thus `GET …/presets/bambuddy-import` is a 404 too.
- `GET …/{preset_id}/diff?from_version=&to_version=`: `to_version` is the current version by default, and `from_version` is `max(to_version - 1, 1)`. Each is a positive integer, else 422. An unknown one is 404 `version_not_found`. A version whose blob is lost is 409 `document_missing`. Each document goes through `StripCredentials` first.

- [ ] **Step 1: Write the failing tests** (`testdb.New`; rows inserted with SQL as `create_on` writes them; a `Blobs` fake in the package tests; the fake S3 of `blobstore` in `filament_presets_test.go`):
  - `TestIsCredentialMatchesPython`, `TestStripCredentialsMatchesPython`, `TestChangedKeysMatchPython`: every case of `backend/tests/fixtures/filament-preset-vectors.json`.
  - `TestMirrorStateCoversEachCase` (a table): `("bambuddy", s, s, nil) → "bambuddy_owned"`; `("manual", s, nil, nil) → "pending"`; `("manual", s, t, "Bambuddy: 500") → "failed"`; `("3dfp", s, s, nil) → "sent"`; `("3dfp", s, s, "Bambuddy also holds…") → "sent"`.
  - `TestListCountsBindingsAndFilters`: three presets (`3dfp`, `bambuddy`, `manual`); one bound to a type and to two spools. `used_by_types == 1`, `used_by_spools == 2`, `ref == "sb-<id>"`. `?source=manual` holds only the manual one. `?source=x` and `?type_id=x` are 422.
  - `TestDetailListsVersionsNewestFirstWithTheCurrentDocument`: three versions; `versions` are 3, 2, 1; `document` is the v3 document without `printhost_apikey`; the key order is the blob's.
  - `TestDetailOfALostBlobHasNoDocument`: the fake has no blob. `document` is null, `document_error` is `document_missing`, and the row still answers. The same for `document_too_large`, `document_unreadable` and `store_not_configured` from the errors of the fake.
  - `TestDiffDefaultsToPreviousAndCurrent`: with no query, it compares v2 to v3. With one version only, it compares v1 to v1, with no changes.
  - `TestDiffRefusesAnUnknownVersionAndALostBlob`: `to_version=9` → 404 `version_not_found`; the v1 blob gone → 409 `document_missing`; an unknown id → 404 `preset_not_found`.
  - `TestTheAnswersHaveTheWireKeys`: the key sets of a list row, a detail and a diff equal those of `test-fixtures/filament/preset-wire.json`.
  - `TestPublicRoutesUseTheGate` (`filament_presets_test.go`): in `enforce` with no cookie, 401 signed-out; in `observe`, served and counted.
  - `TestAnUnswitchedAreaForwardsToNext`: `newRouter` without the area sends `GET /api/v1/filament/presets` to `next`.
  - `TestTheAreaNeedsTheKEK`: `newRouter` with `keys` nil and the area named stops the start.
  - `TestTheImportPostStillReachesPython`: with the area on, `POST /api/v1/filament/presets/bambuddy-import` reaches `next`.
- [ ] **Step 2: Vet and build.** Expected: the only errors name the missing package.
- [ ] **Step 3: Implement the package, the `newRouter` case (N2) and the README rows.** The README's area table gets `filament-presets` with its routes. It says that the routes use `internal/auth`, and that the area needs the KEK and an `s3` store for documents. It names `OpenKey` (#8129) as the read the area uses.
- [ ] **Step 4: Vet and build.** Expected: clean.
- [ ] **Step 5: Commit** — `feat(core): list, read and diff ScadBuddy's filament presets in Go (#7204)`.

### Task A4: The agent's read tools

**Files:**

- Create: `agent/src/api/filamentPresets.ts` (the `paths` fragment and `filamentPresetClient(backend)`, N8)
- Modify: `agent/src/tools/filament.ts` (three tools after `filament_bambuddy_presets_import`, :2251; the `linkingTools` map, :2416), `agent/src/tools/filamentLinks.ts` (`filamentPaths.preset`, `LinkKind` `'preset'`, and an `ownPath` case before the `str(row.id)` check, because a preset id is a number), `test-fixtures/filament/ui-paths.json` (one `preset` vector), `frontend/src/lib/filamentLinks.ts` (`presets`, `preset(id)`), so that the vector tests of both packages pass in this commit
- Test: `agent/test/filament.test.ts`, `agent/test/filamentLinks.test.ts`, `frontend/src/lib/filamentLinks.test.ts`

**Interfaces:**

- `filament_preset_list`, risk `read`, `routes: []`, `GET /api/v1/filament/presets`. Input: `{ source?: '3dfp' | 'bambuddy' | 'manual', type_id?: uuid }`.
- `filament_preset_get`, risk `read`, `routes: []`, `GET /api/v1/filament/presets/{preset_id}`. Input: `{ preset_id: positive int }`.
- `filament_preset_diff`, risk `read`, `routes: []`, `GET /api/v1/filament/presets/{preset_id}/diff`. Input: `{ preset_id, from_version?: positive int, to_version?: positive int }`.
- `filamentPaths.preset(id: string | number) => '/filament/presets/<id>'` (agent); `filamentLinks.presets` and `filamentLinks.preset(id: number | string)` (frontend).
- A 404 with no `code` is `ToolError('the presets routes are not on yet on this server (core area filament-presets)')` (N5).

- [ ] **Step 1: Write the failing tests:**
  - `it.each` over the three: `%s is read tier, never gated, with no OpenAPI route`.
  - `lists presets with the filters it was given`: msw asserts `?source=manual&type_id=<uuid>`; the rows carry `ui_path: '/filament/presets/7'`; the msw answer is the row of `preset-wire.json`.
  - `reads a preset and diffs two versions`.
  - `says the presets routes are off on a bare 404`: a 404 with no `code` is that `ToolError`; a 404 `preset_not_found` is the problem as it is.
  - `the preset tools run in durable mode`: `toolActivities(ALL_TOOLS, deps)[name]` and `toolCallActivities(...)[toolCallActivity(name)]` are functions for all three.
  - `filamentLinks.test.ts` (both packages): the vector `{ "kind": "preset", "key": "12", "path": "/filament/presets/12" }`.
- [ ] **Step 2: Type-check** (the agent line of Global Constraints). Expected: the only errors name the missing tools.
- [ ] **Step 3: Implement.** The descriptions say what `source` and `mirror` mean (R9), that only a `manual` preset can be edited (R3), and that the copy, edit and send-again tools come later.
- [ ] **Step 4: Lint and type-check.** Expected: clean.
- [ ] **Step 5: Commit** — `feat(agent): filament preset tools: list, get, diff (#7204)`.

### Task A5: Frontend client, mocks and the Presets tab

Load `frontend-design:frontend-design` first.

**Files:**

- Create: `frontend/src/api/filamentPresets.ts` (hand-written types, `listPresets`, `getPreset`, `diffPreset`, `routeMissing`; N8)
- Create: `frontend/src/mocks/features/filament/presets.ts` (exports `handlers` and `reset`; `mocks/features.ts` loads every feature by glob)
- Modify: `frontend/src/pages/filament/tabs.ts` (`'presets'` after `'types'`, :32), `frontend/src/pages/filament/FilamentPage.tsx` (`INTRO.presets`, :27, and the tab body), `frontend/src/App.tsx` (the literal route `filament/presets`, beside :139)
- Create: `frontend/src/pages/filament/PresetsTab.tsx`
- Test: `frontend/src/pages/filament/PresetsTab.test.tsx`, `frontend/src/pages/filament/tabs.test.ts`, `frontend/src/mocks/features/filament/presets.test.ts`

**Interfaces:**

- `listPresets(query?: { source?: PresetSource; typeId?: string }): Promise<PresetList>`, where `PresetList = { rows: PresetRow[]; served: boolean }` (`served` is false on a 404 with no `code`); `getPreset(id: number): Promise<PresetDetail>`; `diffPreset(id: number, from?: number, to?: number): Promise<PresetDiff>`; `routeMissing(cause: unknown): boolean`.
- The seed of the mock: a `3dfp` preset (sent), a `bambuddy` preset, a `manual` preset with `mirror: 'failed'` and two versions, and one version whose document is missing. The mock switch `presetsOff()` answers a bare 404 on every preset path.

- [ ] **Step 1: Write the failing tests:**
  - `presets.test.ts`: the row, detail and diff of the mock have the keys of `preset-wire.json`; the diff of the lost version is 409 `document_missing`; `presetsOff()` gives a 404 with no `code`.
  - `PresetsTab.test.tsx`: `lists each preset with its source, version and mirror state` (a "Failed" state with the error text in its title); `filters by source`; `opens a preset's page from its name` (`href` is `/filament/presets/<id>`); `shows the Bambuddy id, or none`; `says presets are not on when the area is off` (no table, the N5 text).
  - `tabs.test.ts`: the route test still passes with the new literal route; add `puts Presets after Types`.
- [ ] **Step 2: Type-check and run the touched tests** (the frontend line of Global Constraints). Expected: the new tests fail on the missing code.
- [ ] **Step 3: Implement.** The table follows `TypesTab.tsx` (a search box, source chips, the type name as a link to the type page). Phase A has no write button.
- [ ] **Step 4: Lint, type-check and run the touched tests.** Expected: clean and green.
- [ ] **Step 5: Commit** — `feat(frontend): the Filament Presets tab (#7204)`.

### Task A6: The preset page (read only)

Load `frontend-design:frontend-design` first.

**Files:**

- Create: `frontend/src/pages/filament/PresetPage.tsx`
- Modify: `frontend/src/App.tsx` (the route `filament/presets/:id`, lazy as `TypePage`, :134), `frontend/src/components/filament/PresetTable.tsx` (an `sb-<id>` ref, :110, is a link to its page), `frontend/e2e/page-titles.spec.ts` (`/filament/presets` and the page of one seeded preset, beside :53)
- Test: `frontend/src/pages/filament/PresetPage.test.tsx`, `frontend/src/components/filament/PresetTable.test.tsx`

**Interfaces:**

- Consumes: the calls, mock and `filamentLinks.preset` of Task A5.
- Produces: `PresetPage` (a route component that reads `:id`). The page title is the name of the preset.

- [ ] **Step 1: Write the failing tests:**
  - `shows the versions newest first and the keys that changed from the previous version`.
  - `compares any two versions the person picks`.
  - `shows the current document's keys with a filter box`.
  - `shows a message when a version's document is missing` (409 `document_missing`), and one for `document_error` `store_not_configured` on the detail.
  - `says presets are not on when the area is off`.
  - `PresetTable.test.tsx`: `links a ScadBuddy preset ref to its page` (`sb-7` → `/filament/presets/7`); a Bambuddy id stays text.
- [ ] **Step 2: Type-check and run the touched tests.** Expected: the new tests fail on the missing page.
- [ ] **Step 3: Implement.** The document view lists keys with a filter box (about 150 keys). A list value shows one line for each item. Phase A has no write control.
- [ ] **Step 4: Lint, type-check and run the touched tests.** Expected: clean and green.
- [ ] **Step 5: Commit** — `feat(frontend): a preset's page: versions, diff and document (#7204)`.

### Task A7: Docs

**Files:**

- Modify: `docs/features/spools-and-filament.md` (the presets paragraph at :104: the view, its Go routes, the area and its switch, the tools, R3, R9 and N5)
- Modify: `frontend/CLAUDE.md` (:282: the tab list gets Presets; the preset page; the 404 text of N5)
- Modify: `backend/CLAUDE.md` (the `sb_presets.py` entry near :948: Go core reads the presets, `internal/filamentpresets`; the vectors of N4)
- Modify: `agent/CLAUDE.md` (beside the filament tools: three tools over Go routes, `routes: []`)
- Modify: `docs/architecture/processes.md` (the public routes of core: the area `filament-presets`)
- Modify: `tools/go/core/README.md` (if Task A3 did not finish it)

- [ ] **Step 1: Edit each file.** Each statement names its source file. No counts.
- [ ] **Step 2: Check.** `grep -rn "Shopping list and Ledger\|tabs are Spools" frontend/CLAUDE.md docs/` shows no stale list. `docs/features/filament-parity.md` stays as it is (R16). `npx markdownlint-cli2@0.20.0` on the changed files is clean.
- [ ] **Step 3: Commit** — `docs(filament): the read-only Presets view and its Go routes (#7204)`.

### Task A8: Switch the area on (outside this repo; eh-homelab/clusters)

This is an explicit step of the issue, not code in this repo (N6).

- [ ] **Step 1: Wait for the image.** The `scadbuddy-core` image on prod must hold the area. Read the pod images (`kubectl -n bambuddy get pods -o jsonpath`, as `deploy/CLAUDE.md` "Prod checks" says). Never merge Step 3 first.
- [ ] **Step 2: Check what the area needs.** The `scadbuddy-core` container has `SCADBUDDY_SECRET_KEY_FILE` (N2), and the stored `store_backend` is `s3` (Settings → Blob store, or the `settings` row, read as `deploy/CLAUDE.md` "Prod checks" says). If one is missing, stop and say so on the issue.
- [ ] **Step 3: Open the clusters PR.** In `scadbuddy.yaml`, add `filament-presets` (that one name) to `SCADBUDDY_CORE_AREAS` on the `scadbuddy-core` container. Title: `feat(scadbuddy): serve the filament-presets area from core (eh-homelab/ScadBuddy#7204)`.
- [ ] **Step 4: Check prod after the sync** (`status.history[-1].deployedAt` of the `scadbuddy` Argo CD app, root `CLAUDE.md` "Prod gotchas"). Expected: `GET /api/v1/filament/presets` from the signed-in page answers 200, and the detail of one preset has a `document`. Post both on #7204.
- [ ] **Step 5: Rollback** is to drop the name. The paths 404 again, and the UI and the agent say that the view is not on (N5).

---

## Phase B (after `internal/temporalx` merges; one PR)

Blocked by inventory K1 layer 4 (`tools/go/core/internal/temporalx`; session eee8e050, no PR at 3b53cf43c). Tasks B1 and B2 may go first (N7).

### Task B1: Copy and edit as library activities (Python)

**Files:**

- Create: `backend/scadbuddy/filament/preset_writes.py` (the models, the write helpers and the two activities)
- Modify: `backend/scadbuddy/main.py` (the activity list of the `library` worker, near :2365)
- Test: `backend/tests/filament/test_preset_writes.py`

**Interfaces:**

- Consumes: `sb_presets.keep_document`, `document_bytes`, `create_on`, `version_on`, `lock_presets_on`, `FilamentPresetStore.read`, `Conn`; `idempotent_write` and `Replay` of `crud/idempotency.py`; `preset_mirror.mirror_pending`.
- Produces:
  - `class PresetCopyInput(BaseModel)`: `preset_id: int`, `name: str` (1 to 200 characters, stripped), `actor: str`, `idempotency_key: str`.
  - `class PresetEditInput(BaseModel)`: `preset_id: int`, `expected_version: int` (> 0), `name: str | None = None`, `set: dict[str, str | list[str]] = {}`, `unset: list[str] = []`, `actor: str`, `idempotency_key: str`. The validators apply R7: the `KEY` pattern; `name` and `is_credential` refused; at most `MAX_EDIT_KEYS = 500`; a key in both `set` and `unset` refused; an edit with no `name`, `set` or `unset` refused.
  - `class PresetWritten(BaseModel)`: `preset_id: int`, `version: int`, `changed: bool`, `mirror: Literal["sent", "failed", "pending", "off"]`.
  - The activities `filament_preset_copy` and `filament_preset_edit`, named by the constants `COPY_ACTIVITY` and `EDIT_ACTIVITY`. Each takes one JSON object (the fields of `PresetCopyInput` or `PresetEditInput`) and validates it in its body with `_parsed` (P16). Each returns the dump of a `PresetWritten`. Each failure is a non-retryable `ApplicationError` whose `type` is the problem code: `invalid_input` (details: the `loc` and `msg` of each fault, with no input value), `key_refused` (details: the key, cut to 64 characters), `preset_not_found`, `not_manual`, `version_moved` (details: the current version), `document_missing`, `idempotency_key_reused`.
  - `WRITE_MIRROR_TIMEOUT: Final = 30.0` (R11).

The order in each activity (superseded by P17, P18, see "Decisions made while building"): read the current document (`FilamentPresetStore.read`), apply the change and R6, and `keep_document`. Then, in one transaction, `idempotent_write(conn, "filament", f"{actor}\x1f{key}\x1f{preset_id}", ROUTE, asked, run)`. `run` takes `lock_presets_on`, reads the row `FOR UPDATE`, refuses `not_manual` (edit) or `version_moved` (when `version` is not `expected_version`), and calls `create_on` (copy, R4) or `version_on` (edit). After the commit: `asyncio.wait_for(mirror_pending(pool, store, settings, [id]), WRITE_MIRROR_TIMEOUT)`. A `TimeoutError` is `mirror: "pending"`. A blob kept for a refused write stays unheld, and the blob sweep reclaims it (the docstring of `keep_document`).

- [ ] **Step 1: Write the failing tests** (the activity functions called directly, with the `pg_pool` fixture and `LocalBlobStore`; respx for Bambuddy, as `test_preset_mirror.py` does):
  - `test_a_copy_is_a_manual_preset_with_the_source_document_and_the_new_name` (R4, R6; `blob_refs` holds its key under `filament_preset`, `f"{id}:1"`).
  - `test_an_edit_sets_and_unsets_keys_as_a_new_version`; `test_a_rename_alone_is_a_new_version`; `test_an_edit_that_changes_nothing_writes_no_version` (`changed` is false).
  - `test_a_stale_edit_is_refused`: two edits with `expected_version=1`. The second fails with the type `version_moved` and the current version 2. The row holds the first edit only.
  - `test_only_a_manual_preset_is_edited`: `3dfp` and `bambuddy` fail with `not_manual`.
  - `test_refused_keys` (parametrized over `name`, `printhost_apikey`, `my_token`, `bad key`, `""`, a key in both lists, 501 keys, an empty edit): `ValidationError`.
  - `test_a_repeated_copy_key_makes_one_preset`: the same input two times gives the same `preset_id` and one new row.
  - `test_a_copy_and_an_edit_are_sent_to_bambuddy_after_the_commit`: respx `POST /local-presets/` answers id 41, then `PUT /local-presets/41`; `mirror` is `sent`.
  - `test_a_failed_send_never_fails_the_edit`: Bambuddy answers 500; the activity returns `mirror: "failed"`.
  - `test_the_library_worker_serves_the_preset_activities`: the activity list of the API names both.
- [ ] **Step 2: Lint** (the backend line). Expected: the only errors name the missing module.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Lint.** Expected: clean.
- [ ] **Step 5: Commit** — `feat(filament): copy and edit a preset as library activities (#7204)`.

### Task B2: Send one preset again (Python)

**Files:**

- Modify: `backend/scadbuddy/filament/preset_mirror.py` (`mirror_again`; the module docstring names the Go route), `backend/scadbuddy/filament/preset_writes.py` (the activity `filament_preset_mirror_again`), `backend/scadbuddy/main.py` (the activity list)
- Test: `backend/tests/filament/test_preset_mirror.py`

**Interfaces:**

- `MirrorAgain = Literal["sent", "failed", "busy", "in_sync", "pending"]`; `RESEND_TIMEOUT: Final = 60.0`.
- `async mirror_again(pool, store, settings: StoredSettings, preset_id: int, timeout: float = RESEND_TIMEOUT) -> MirrorAgain`. The refusals are `ApplicationError` types `preset_not_found`, `not_mirrored` (a `bambuddy` preset), `mirror_off`, `not_configured` and `instance_changed`.
- The activity `filament_preset_mirror_again` (`MIRROR_AGAIN_ACTIVITY`) takes one JSON object, `{"preset_id": int}` (`PresetMirrorInput`), and validates it in its body (P16). A bad object is a non-retryable `invalid_input`. It returns a `MirrorAgain`.

- [ ] **Step 1: Write the failing tests** (respx): `test_send_again_sends_a_failed_preset`; `test_send_again_keeps_a_new_failure` (`mirror_error_at` moved); `test_send_again_of_a_sent_preset_calls_nothing` (`in_sync`); `test_send_again_refusals` (parametrized: the refusals, with no Bambuddy call); `test_send_again_while_another_mirror_runs_is_busy` (hold `LOCK` with `acquire_lock`); `test_send_again_past_its_timeout_is_pending`.
- [ ] **Step 2: Implement.** The order: the row lookup; `not_mirrored`; `mirror_off` and `not_configured` from `wants_mirror` and `instance_of` (`preset_mirror.py:76`, `:81`); `instance_changed` from `_links_elsewhere` (:87); `in_sync` when `_pending(pool, [id], instance)` is empty; then `asyncio.wait_for(mirror_with(client, pool, store, instance, [id]), timeout)` inside `client_for(settings)`. A count of 1 is `sent`. Else read the row again: `mirror_error_at` at or after the start is `failed`; else `busy`. `TimeoutError` is `pending`.
- [ ] **Step 3: Lint.** Expected: clean.
- [ ] **Step 4: Commit** — `feat(filament): send one preset to Bambuddy again, as a library activity (#7204)`.

### Task B3: The Go write routes

**Files:**

- Create: `tools/go/core/internal/filamentpresets/writes.go`, `actor.go`
- Modify: `tools/go/core/internal/filamentpresets/filamentpresets.go` (`Public` takes a starter), `tools/go/core/main.go` (the case passes the `temporalx` client of core), `tools/go/core/README.md`, `test-fixtures/filament/preset-wire.json` (`Edited`, `MirrorAgainOutcome`)
- Test: `tools/go/core/internal/filamentpresets/writes_test.go` (a fake starter in place of `temporalx`)

**Interfaces:**

- `type Starter interface` in the form that `temporalx` gives: a workflow run whose one activity runs on the queue `library`, or a direct start. G5 of #7143 leaves the form to K1 layer 4. The run's id is the one of R12, and its conflict policy joins a running one.
- `func actor(c auth.Caller) string`: `actor` of `api/filament.py:1211` (the principal for a person or a token, `service:<caller>` for a service, else `anonymous`).
- `POST /api/v1/filament/presets/{preset_id}/copy`, body `{name}`, `Idempotency-Key` required → 201 `Detail` of the new preset.
- `PATCH /api/v1/filament/presets/{preset_id}`, body `{expected_version, name?, set?, unset?}`, `Idempotency-Key` required → 200 `Edited {changed: bool, preset: Detail}`.
- `POST /api/v1/filament/presets/{preset_id}/mirror` → 200 `MirrorAgainOutcome {outcome, preset: Row}`.
- Go sends each activity one JSON object, not typed arguments: `{preset_id, name, actor, idempotency_key}` (copy), `{preset_id, expected_version, name?, set?, unset?, actor, idempotency_key}` (edit) or `{preset_id}` (send again). The activity validates it again (P16). Go maps `invalid_input` to 422 and `idempotency_key_reused` to 422 (R12).
- Go checks R7 and the key before the start (422 `key_refused`, the 422 of the shape, 428). It maps the `type` of each activity failure to its problem (Global Constraints). A wait past the bound of R11, or a start that Temporal refuses, is 503 `preset_write_pending`. After a success, Go reads the answer with the `Detail` or `Row` of Phase A.

- [ ] **Step 1: Write the failing tests:** `TestCopyNeedsAKey` (428 with the type; nothing started); `TestEditBodyAppliesR7` (each refused case of `test_refused_keys` in B1 is a 422 before the start); `TestWritesStartTheActivityOnLibrary` (the queue `library`; the input has the `actor` from `auth.CallerOf`); `TestCopyJoinsARunWithTheSameKey` (two starts, one run id); `TestEditMapsEachFailureType` (a table: type → status and `code`; `version_moved` carries `current_version`); `TestAWriteTimeoutIs503Pending`; `TestTheWriteAnswersHaveTheWireKeys`; `TestAReadTokenCannotWrite` (`enforce`: 403; `observe`: served and counted).
- [ ] **Step 2: Vet and build.** Expected: the only errors name the missing code.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Vet and build.** Expected: clean.
- [ ] **Step 5: Commit** — `feat(core): copy, edit and send again a filament preset through library activities (#7204)`.

### Task B4: The agent's write tools

**Files:**

- Modify: `agent/src/api/filamentPresets.ts`, `agent/src/tools/filament.ts`, `agent/src/sessions/touched.ts` (`TOUCHES_NOTHING`, :726, with the comment "#7204: a filament preset is no recorded resource kind")
- Test: `agent/test/filament.test.ts`

**Interfaces:**

- `filament_preset_copy`, risk `outward`, `routes: []`. Input: `{ preset_id, name: string 1..200 }`; header `Idempotency-Key: idempotencyKey(ctx)`.
- `filament_preset_update`, risk `outward`, `routes: []`. Input: `{ preset_id, expected_version: positive int, name?, set?: Record<string, string | string[]>, unset?: string[] }`; header `Idempotency-Key`.
- `filament_preset_mirror`, risk `outward`, `routes: []`. Input: `{ preset_id }`.

- [ ] **Step 1: Write the failing tests:** `%s is outward tier, gated, with no OpenAPI route`; `sends an edit as a PATCH with its key and answers the new version` (the body has no `preset_id`); `says a stale edit is version_moved and names the current version`; `takes no set value that is not a string or a string list`; `the preset writes touch no recorded kind`; `the preset write tools run in durable mode`. The descriptions say that the three write Bambuddy through the mirror while `filament_preset_mirror` is on, and that only a `manual` preset can be updated (R3).
- [ ] **Step 2: Type-check, implement, then lint and type-check** (the agent line). Expected: clean.
- [ ] **Step 3: Commit** — `feat(agent): filament preset tools: copy, update, send again (#7204)`.

### Task B5: The frontend's writes

Load `frontend-design:frontend-design` first.

**Files:**

- Modify: `frontend/src/api/filamentPresets.ts` (`copyPreset(id, name, key)`, `editPreset(id, body, key)`, `mirrorPreset(id)`), `frontend/src/mocks/features/filament/presets.ts`, `frontend/src/pages/filament/PresetsTab.tsx` ("Send again" only for `mirror` `failed` or `pending`), `frontend/src/pages/filament/PresetPage.tsx` (copy, edit, send again)
- Test: `frontend/src/mocks/features/filament/presets.test.ts`, `frontend/src/pages/filament/PresetsTab.test.tsx`, `frontend/src/pages/filament/PresetPage.test.tsx`

- [ ] **Step 1: Write the failing tests:** the mock answers 409 `version_moved`, 409 `not_manual`, 428 with no key, and `outcome: 'sent'` for the failed preset; `sends a failed preset again and shows the new state` (tab and page); `makes a manual copy and opens it`; `offers edit only on a manual preset` (a note says to copy it first); `saves an edit as a new version and shows the mirror state` (the body has `expected_version`, `set` and `unset`); `says another edit came first and reloads` on 409 `version_moved`; `sends the same key again after a 503 preset_write_pending`.
- [ ] **Step 2: Implement.** Each opened form has one `Idempotency-Key`, kept for a retry after a 503.
- [ ] **Step 3: Lint, type-check and run the touched tests.** Expected: clean and green.
- [ ] **Step 4: Commit** — `feat(frontend): copy, edit and send again a preset (#7204)`.

### Task B6: Docs and the check on prod

- [ ] **Step 1: Edit** `docs/features/spools-and-filament.md`, `frontend/CLAUDE.md`, `backend/CLAUDE.md` (`preset_writes.py`, `mirror_again`), `agent/CLAUDE.md` and `tools/go/core/README.md` for the writes: the activities on `library`, the bounds of R11, the key of R12, and the codes. No counts.
- [ ] **Step 2: Check** with `npx markdownlint-cli2@0.20.0` on the changed files.
- [ ] **Step 3: Commit** — `docs(filament): the Presets view's writes (#7204)`.
- [ ] **Step 4: After the deploy,** check that the `scadbuddy-core` container has what `temporalx` needs (its Temporal address). The area name is on already (N1), so the name needs no clusters change. Post one copy and one send again from prod on #7204.

## Decisions made while building

Each bullet says what was decided, why, and the cost if it was wrong. The codes P1 to P25 are the rulings of the build ledger.

- P1. The rulings R1, R12 and N1 were posted on #7204 when the plan was revised on 2026-11-26. A1 Step 1 is done. Cost if wrong: one more comment.
- P2. N3 wins over the Architecture line: nothing is added to `internal/blobstore`, and the read is `OpenKey` of #8130. The rulings are the plan's decisions, and the summary line is older. Cost if wrong: none.
- P3. The implementer of A4 reads the pattern with `git show origin/feat/7143-unknown-spool:agent/src/api/unknownTrays.ts`, because PR #8123 was not merged. The file is the named pattern. Cost if wrong: if #8123 changes it, A4 still stands alone.
- P4. `select-go.sh` conflicts with #8123, because both edit the vector group on line 8. The PR that merges second keeps every name. Cost if wrong: a merge conflict, and no change in behaviour.
- P5. The read of #8130 waits `readTTL` only for the headers (the merged fix e5f6b832f). `document.go` reads the whole stream, at most 1 MiB, and closes it. This matches N3. Cost if wrong: none.
- P6. `document.go` stays its own and does not wait for `blobstore.ReadFile` of #8138. `ReadFile` reads with no cap, forgets the row of a lost object (N3 says a read must not write), and folds every failure into `found=false`. The codes `document_missing`, `document_too_large`, `document_unreadable` and `store_*` would be lost. Cost if wrong: two unzips of member `file` in core until one folds into the other.
- P7. The `Blobs` interface gets `Size(ctx, key)`, a `SELECT size FROM store_blobs` that `filamentpresets.Store` adds around `*blobstore.Blobs`. `OpenKey` returns the `Ref` only with the open stream, and N10 allows no GET over the cap. `blobstore` is unchanged. Cost if wrong: one extra row read for each detail.
- P8. JSON that is not an object is `document_unreadable` in Go, and the README lists the difference. This is the same ruling as the A1 M3 ruling below. Cost if wrong: none.
- P9. The unzipped member is capped at 1 MiB too, because of zip bombs, and over the cap it is `document_too_large`. This is the intent of N10. Cost if wrong: none.
- P10. `crud.ProblemOf` copies the identical lines of #8123. The PR that merges second resolves the duplicate. Cost if wrong: none.
- P11. `used_by_types` and `used_by_spools` count distinct types and spools, because "used by" means things, and a spool may bind one preset under several keys. Cost if wrong: a reader who wants the binding rows sees fewer.
- P12. The README example of "Public routes" is renamed from `filament-presets` to `examples`, because the old text became false. Cost if wrong: none.
- P13. The source filter and the search run in the browser, because R14 has no paging and the whole list is loaded. Cost if wrong: none at about one preset for each type, printer and nozzle.
- P14. The Type column reads `listTypes` once. If that fails, the cell shows the start of the type id, because the row carries only `type_id`. Cost if wrong: one extra read.
- P15. `FilamentPage.test.tsx` and `e2e/page-titles.spec.ts` (`/filament/presets`) changed beyond the brief, because the new tab made their tab-list assertions false. A6 adds the title of the preset page. Cost if wrong: none.
- P16. The three preset activities each take one JSON object and validate it in the body. A decode failure before the body is untyped and Temporal retries it. Cost if wrong: a bad input retries until its timeout and shows no code.
- P17. A replay reads the stored reply (`stored()`) before any row or document read. A retry then gets the first answer, whatever changed since. Cost if wrong: a retry after a later edit answers `version_moved` or `preset_not_found` in place of the first reply.
- P18. An edit checks its version before it keeps the document, and again under the lock. The first check saves a blob for a stale edit. The second check closes the race. Cost if wrong: a stale edit leaves an unheld blob (the sweep reclaims it), or a race writes a lost update.
- P19. A refused input's failure holds no input value, only `loc` and `msg`. Credential keys are refused before type checks, and a refused key shows its first 64 characters, then "…". The failure goes to the Temporal history, the logs and the Go problem. Cost if wrong: a credential value or a very long key leaks into those places.
- P20. Bambuddy has a URL and no key: the send goes, Bambuddy answers 401, and `mirror_error` of the preset records it (`failed`). There is no new refusal. Cost if wrong: one failed call, and the error text says why.
- A1 M3. A document that is not a JSON object gets no vector. A3 says Go answers `document_unreadable` for it, a rule only Go has. The Python `{}` is `FilamentPresetStore.read`, not `strip_credentials`. Cost if wrong: one vector added in A3.
- A3 M4. The test fake stays a near-copy of the `readStore` of `blobstore`, because `blobstore` exports no test helper. Share it when a third user appears. Cost if wrong: one copy.
- A6 Minor 3. The `sb-<id>` link shows before the row is saved, and has no fix. The page answers "No such preset", and the reviewer calls it acceptable. Cost if wrong: one dead link on a row that is not saved.
- P21. The write workflow `FilamentPresetWrite` is a Go workflow on core's own worker. Its one step is the Python activity, run by name on the queue `library`. There is no Python workflow. Why: Go needs only a run with one id and one activity, and a Python workflow would add a second deploy for the same result. Cost if wrong: a Python workflow must replace it, and the run ids stay the same.
- P22. Prod's core has no Temporal address. The presets area still starts: the reads serve, no worker starts, and each write answers 503 `writes_not_configured`. Why: the address is needed only for `inventory`, and the area must not crash core when it is on before writes are. Cost if wrong: an operator who expects writes on prod sees 503 until the address is set.
- P23. The activity has a schedule-to-start timeout equal to the time the route waits (the bound of R11, plus 30 s, plus 10 s). Why: a task that no worker took by the time Go answered 503 must never run late, because the client sends the key again. Cost if wrong: a write sent while the library worker restarts fails, and the client sends the key again.
- P24. R12's workflow id also hashes the JSON body that Go sends the activity (changes R12). Why: two sends of one key with different bodies must not join one run, so the ledger can answer the second with 422 `idempotency_key_reused`. Cost if wrong: none, because the same key with the same body still joins one run.
- P25. Go refuses an `expected_version` that is not a JSON integer above 0, so `"1"` is 422 `invalid_input`. Why: Python's model takes a strict integer. Cost if wrong: a client that sends `"1"` gets 422.
