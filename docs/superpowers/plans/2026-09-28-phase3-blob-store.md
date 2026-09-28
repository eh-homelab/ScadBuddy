# Phase 3 — One content-addressed store, backed by Bambuddy's library — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every file a render step reads or writes (pieces, template snapshots, `// file` uploads, downloaded fonts) lives in a store any worker can reach, with Bambuddy's library as the production backend, so the render worker Deployment can run more than one replica with no shared volume.

**Architecture:** Phase 1's directory-shaped `BlobStore` stays the interface the activities use. Under it, this phase adds §6.2's byte-stream store (`ContentStore`: `put/get/stat/delete/list`) over a pluggable backend (`LocalContentBackend`, `BambuddyContentBackend`), with the index, caps and usage in Postgres (`store_blobs`). A `CachedBlobStore` gives each process a local cache in front of it: a piece directory is packed into one blob when a stage finishes (`publish`) and unpacked on the next worker that needs it (`fetch`), so a piece is downloaded once per worker. Snapshots, assets and fonts are mirrored the same way. `store_backend` is a stored setting (`local` by default); the render key `bambuddy_render_api_key` is stored like `bambuddy_api_key` and re-read by workers so a rotation needs no restart.

**Tech Stack:** Python 3.12, FastAPI, psycopg 3 + psycopg_pool, httpx, respx (recorded-client tests), `temporalio` activities from phase 1, React 19 + vitest + msw for the Settings page.

**Spec:** `docs/superpowers/specs/2026-09-27-template-pipelines-design.md` — §6 (6.1 why one store, 6.2 interface, 6.3 the Bambuddy backend and its verifications), §3.1 (the worker scales past one replica once the store is shared), §8.4 (Part refs on the record), §9 (the render key), §10 (Settings), §11 item 3, §12 (#316/#317 folder rules). Epic: **#426** (parent #423; blocks #427). Phase-1 plan: `docs/superpowers/plans/2026-09-28-phase1-render-on-temporal.md` (Task 4 and the Rollout addendum).

**Base:** phase 1 (#424) through its PR3, as on the `wt-service` stack at 69306836: the Temporal path behind `SCADBUDDY_TEMPORAL_ADDRESS`, `RenderActivities`/`WorkerDeps` (`workflows/activities.py`), `python -m scadbuddy.worker` (`build_worker_deps`, `worker_deps_from_state`), `RenderService` (`render/submit.py`, with its settable `client`), `LocalBlobStore`/`BlobRefs`/`sweep_blobs` (`store/`), and the legacy `RenderQueue` still selectable. Phase 1 did **not** ship `bambuddy_render_api_key`, a `Settings.render_bambuddy_key()` or a health flag for it; Task 1 adds the field and its reader here. Every file this plan calls "phase 1's" is read from that stack.

## Global Constraints

- "`bambuddy` is the one production backend. A `local` backend (the data volume, today's layout) exists for tests, `verify.sh`, and the phase-1 deployment, where it is correct only with a single render worker on the same volume as the API" (§6.2). `local` stays the default and keeps phase 1's behaviour byte for byte.
- "`put` refuses past `SCADBUDDY_STORE_MAX_TOTAL_BYTES` / `_MAX_COUNT`, except for re-puts of what already exists" (§6.2).
- "Workers keep a local LRU cache by sha256 under `SCADBUDDY_WORKER_CACHE_DIR` (sticky scheduling is an optimisation, never a correctness requirement)" (§6.2). Deviation, stated: the cache is the worker's own `SCADBUDDY_DATA_DIR/blobs` (an `emptyDir`), because `JobResult.model_3mf` / `preview_glb` are paths relative to the data dir; the cap is `SCADBUDDY_WORKER_CACHE_MAX_BYTES`.
- "The Settings `library_folder_id` folder is ScadBuddy's inbox and the only place it deletes from; project folders are the user's record, flat except `Media/`, never moved or deleted by ScadBuddy; no dot-named folders (Bambuddy does not hide them)" (§6.3).
- "Files live in Bambuddy; metadata (manifests, BOM, inputs, revision, `library_files` per #316) lives in Postgres. Render workers use a key with *Manage Library* only" (§6.3).
- `bambuddy_render_api_key`: "stored like the existing `bambuddy_api_key` (same encryption, same rotation UI, §10). Render workers read only that field. When it is unset they fall back to the full key and the Settings page shows a persistent warning — 'render workers hold the full Bambuddy key; template code can print'" (§9). Controller ruling: store-backed, not env-only; `SCADBUDDY_BAMBUDDY_RENDER_API_KEY` only seeds it (`ENV_SEEDED`).
- "Same encryption" means: the render key is stored in plaintext in the `settings` table, exactly as `bambuddy_api_key` is today (`library/settings_store.py`: "the backend has no secret store"), and never returned by the API (only `has_render_api_key`). Encrypting both is out of scope here; follow-up issue: encrypt stored keys.
- "Regenerate `backend/openapi.json`, `frontend/src/api/schema.d.ts`, `agent/src/api/schema.d.ts` per CLAUDE.md" (§10) — which since #492 means: never commit them; after a backend API change run `cd frontend && pnpm gen:api` then `cd agent && pnpm gen:api` (each exports the spec with uv), then the package's `typecheck`/`test`. The `freshness` job posts the diff on the PR.
- Every `/api/v1` operation needs an agent tool or a `agent/src/tools/coverage.ts` entry, or `agent/test/coverage.test.ts` fails (CLAUDE.md).
- Schema changes are a NEW file in `backend/scadbuddy/migrations/` named `<yyyymmdd>T<hhmm>Z_<slug>.sql`; never edit a merged one (CLAUDE.md, #491).
- Backend gates per task: `cd backend && uv run --frozen ruff check . && uv run --frozen ruff format --check . && uv run --frozen mypy && uv run --frozen pytest` green. Run `uv run --frozen ruff format .` first in every task: the plan's test code is not wrapped to `line-length = 100`, and the gate is `format --check` (Postgres tests need `SCADBUDDY_TEST_DATABASE_URL`). Frontend: `pnpm lint && pnpm typecheck && pnpm test && pnpm build`.
- Tests never reach a live Bambuddy: `respx` over bodies recorded from, or shaped field-for-field by, Bambuddy's recorded OpenAPI (`tests/bambuddy/recordings/`). The live measurements are the manual `python -m scadbuddy.store.verify_bambuddy` step (Task 10).

## Review Focus

1. **A `Work/` file deleted or replaced by a person in Bambuddy's UI** must make the piece render again, never fail the job or feed altered bytes to a later step — Task 5, `test_a_piece_deleted_or_altered_in_the_backend_is_rendered_again`.
2. **Two workers storing the first blob of a new template at once** must create exactly one `<Template>/Work` pair in Bambuddy — Task 4, `test_concurrent_first_uploads_create_one_folder_pair`.
3. **A zombie attempt** (heartbeat timed out, still running) publishing after its retry has published must be refused, never overwrite the newer piece — Task 5, `test_a_stale_publisher_cannot_overwrite_a_newer_piece`.
4. **A delete aimed at a file outside a ScadBuddy `Work/` folder** (a project folder, a folder the user made) must be refused without sending the DELETE — Task 4, `test_delete_outside_a_work_folder_is_refused_without_a_request`.
5. **The render key rotated in Settings while workers run** must be used by the next store call without a restart — Task 4, `test_a_rotated_render_key_is_used_without_a_restart`.
6. **A `render_main` retried on a worker whose cache has never seen the piece**, after an earlier attempt (or an earlier `RenderPiece` that failed later) already published it, must publish over it, never fail with `StaleBlobError` until the sweep — Task 5, `test_render_main_on_a_worker_without_the_piece_publishes_over_the_index`.

---

## File Structure

Created:
- `backend/scadbuddy/migrations/20260928T1800Z_blob_store.sql` — `store_blobs` (the index) and `store_folders` (the Bambuddy folders ScadBuddy made).
- `backend/scadbuddy/store/content_models.py` — the value types and exceptions (`BlobScope`, `BlobRef`, `BlobStat`, `StoreUsage`, `BlobMissingError`, …), imported by `index.py`, `local.py` and `bambuddy.py` without a cycle.
- `backend/scadbuddy/store/content.py` — §6.2's byte-stream types and `ContentStore` (caps, dedupe, sha check, CAS replace), `ContentBackend` protocol, `sweep_content`, `template_title`.
- `backend/scadbuddy/store/index.py` — `BlobIndex` over `store_blobs`; the `Pool` alias.
- `backend/scadbuddy/store/archive.py` — `pack_dir` / `unpack_dir` / the sha marker: a directory as one blob.
- `backend/scadbuddy/store/bambuddy.py` — `BambuddyContentBackend`, `BambuddyTarget`, `RenderSettingsSource`, `folder_name`.
- `backend/scadbuddy/store/cache.py` — `CachedBlobStore` (the worker/API cache), `StaleBlobError`, `materialize_result`.
- `backend/scadbuddy/store/snapshots.py` — `SnapshotStore`: template source per (slug, revision).
- `backend/scadbuddy/store/fonts.py` — `FontMirror`: downloaded font families through the store.
- `backend/scadbuddy/store/assets.py` — `RemoteAssets`: `AssetStore` on the store.
- `backend/scadbuddy/store/factory.py` — `StoreBundle`, `build_store`, `store_usage`, `StoreHealth`, `store_health`.
- `backend/scadbuddy/store/verify_bambuddy.py` — the manual §6.3 measurements.
- `backend/scadbuddy/api/store.py` — `GET /store/usage`.
- `backend/tests/support/store.py`, `backend/tests/store/__init__.py`, `backend/tests/store/conftest.py`, `backend/tests/store/test_content_store.py`, `test_bambuddy_backend.py`, `test_worker_cache.py`, `test_snapshots_and_fonts.py`, `test_remote_assets.py`, `test_factory.py`; `backend/tests/bambuddy/test_library_blobs.py`; `backend/tests/api/test_store.py`.

Modified:
- `backend/scadbuddy/core/config.py`, `core/settings.py` — caps, cache cap, `StoreBackend`; `store_backend` env seed.
- `backend/scadbuddy/library/settings_store.py` — `bambuddy_render_api_key` and `store_backend` stored; `RenderStoreSettings`; `SettingsStore.pool`.
- `backend/scadbuddy/api/settings.py` — `has_render_api_key`, `render_key_fallback`, `store_backend`.
- `backend/scadbuddy/bambuddy/client.py`, `bambuddy/models.py` — `download_library_file`, upload media type, `LibraryFile.folder_id`/`file_hash`.
- `backend/scadbuddy/store/__init__.py`, `store/local.py` — `fetch`/`publish` on the protocol; `LocalContentBackend`.
- `backend/scadbuddy/workflows/activities.py` — fetch before, publish after each stage; snapshots, fonts and assets before a render.
- `backend/scadbuddy/render/jobs.py` — `_export_atomically` becomes public `export_revision`.
- `backend/scadbuddy/render/submit.py` — pin the revision and ensure its snapshot (bambuddy backend).
- `backend/scadbuddy/library/assets.py` — `AssetStore.adopt`, `AssetStore.ids`, `asset_ids_in`.
- `backend/scadbuddy/core/metrics.py`, `api/metrics.py`, `api/health.py`, `api/deps.py`, `api/assets.py`, `api/fonts.py`, `api/jobs.py`, `api/outputs.py`, `main.py`, `worker.py` — wiring.
- `frontend/src/pages/SettingsPage.tsx`, `SettingsPage.test.tsx`, `frontend/src/api/client.ts`, `api/types.ts`, `mocks/handlers.ts`, `mocks/fixtures.ts`.
- `agent/src/tools/customizer.ts` — a tool for `GET /store/usage`.
- `backend/tests/bambuddy/conftest.py`, `tests/bambuddy/recordings/README.md`, `README.md`, `CLAUDE.md`.

**Parallel worktrees.** Tasks 1, 2 and 3 are independent. Task 4 needs 1 (settings reader), 2 and 3. Task 5 needs 2; it can run beside Task 4. Task 6 needs 5; Task 7 runs after Task 6 (both add `WorkerDeps` fields and insert at the top of `render_main`/`prepare` in `workflows/activities.py`; resolved order at the top of `render_main`: the index baseline read (Task 5), then assets (Task 7); in `prepare`: snapshots, then fonts (Task 6)). Task 8 needs 1–7. Task 9 needs 8's API (its render-key half needs only 1). Task 10 is last.

---

### Task 1: Settings — the render key and the store backend, stored

**Files:**
- Modify: `backend/scadbuddy/core/config.py`, `backend/scadbuddy/core/settings.py`
- Modify: `backend/scadbuddy/library/settings_store.py`
- Modify: `backend/scadbuddy/api/settings.py`
- Test: `backend/tests/test_settings_store.py`, `backend/tests/api/test_settings.py`

**Interfaces:**
- Produces: `StoreBackend = Literal["local", "bambuddy"]` (core/config.py); `Config.store_max_total_bytes: int`, `Config.store_max_count: int`, `Config.worker_cache_max_bytes: int`; `Settings.store_backend`, `.store_max_total_bytes`, `.store_max_count`, `.worker_cache_max_bytes`. `StoredSettings.bambuddy_render_api_key: str | None`, `StoredSettings.store_backend: StoreBackend`, `StoredSettings.render_bambuddy_key() -> tuple[str | None, bool]` (new); `SettingsPatch.bambuddy_render_api_key`, `.store_backend`; `StoreNotReadyError(ValueError)`; `SettingsStore.pool -> ConnectionPool[Connection[DictRow]]` (the same type `store/index.py` names `Pool`); `class RenderStoreSettings(BaseModel): store_backend; bambuddy_url; api_key; key_is_fallback: bool; library_folder_id`; `load_render_store_settings(pool, defaults: Settings) -> RenderStoreSettings`. `SettingsView.has_render_api_key`, `.render_key_fallback`, `.store_backend`.

- [ ] **Step 1: Write the failing store tests**

Append to `backend/tests/test_settings_store.py`:

```python
from scadbuddy.library.settings_store import (
    RenderStoreSettings,
    StoreNotReadyError,
    load_render_store_settings,
)


def test_the_render_key_is_seeded_stored_and_cleared_like_the_full_key(
    tmp_path: Path, pg_conninfo: str
) -> None:
    seeded = Settings(
        data_dir=tmp_path, database_url=pg_conninfo, bambuddy_render_api_key="from-env"
    )
    store = SettingsStore(seeded)
    store.open()
    try:
        assert store.load().bambuddy_render_api_key == "from-env"
        store.save(SettingsPatch(bambuddy_render_api_key="rotated"))
        assert store.load().bambuddy_render_api_key == "rotated"
        store.save(SettingsPatch(bambuddy_render_api_key=""))
        assert store.load().bambuddy_render_api_key is None  # cleared beats the env
    finally:
        store.close()


def test_render_workers_fall_back_to_the_full_key_and_say_so(store: SettingsStore) -> None:
    store.save(SettingsPatch(bambuddy_api_key="full"))
    assert store.load().render_bambuddy_key() == ("full", True)
    store.save(SettingsPatch(bambuddy_render_api_key="narrow"))
    assert store.load().render_bambuddy_key() == ("narrow", False)


def test_a_render_worker_reads_only_its_fields(store: SettingsStore, settings: Settings) -> None:
    store.save(
        SettingsPatch(
            bambuddy_url="https://b.test",
            bambuddy_api_key="full",
            bambuddy_render_api_key="narrow",
            library_folder_id=7,
            pipeline_id=3,
        )
    )
    assert load_render_store_settings(store.pool, settings) == RenderStoreSettings(
        store_backend="local",
        bambuddy_url="https://b.test",
        api_key="narrow",
        key_is_fallback=False,
        library_folder_id=7,
    )


def test_the_bambuddy_store_needs_a_url_and_an_inbox_first(store: SettingsStore) -> None:
    with pytest.raises(StoreNotReadyError, match="library folder"):
        store.save(SettingsPatch(store_backend="bambuddy"))
    store.save(
        SettingsPatch(bambuddy_url="https://b.test", library_folder_id=7, store_backend="bambuddy")
    )
    assert store.load().store_backend == "bambuddy"
    store.save(SettingsPatch(store_backend=None))
    assert store.load().store_backend == "local"
```

(`pytest`, `Path`, `Settings`, `SettingsPatch`, `SettingsStore` are already imported at the top of that file; add any the linter reports missing.)

- [ ] **Step 2: Write the failing API tests**

Append to `backend/tests/api/test_settings.py`:

```python
import json


def test_the_render_key_is_write_only_and_its_absence_is_flagged(client: TestClient) -> None:
    body = client.put(
        "/api/v1/settings",
        json={"bambuddy_url": "https://bambuddy.test", "bambuddy_api_key": "full"},
    ).json()
    assert body["render_key_fallback"] is True
    assert body["has_render_api_key"] is False
    response = client.put("/api/v1/settings", json={"bambuddy_render_api_key": "narrow"})
    body = response.json()
    assert body["has_render_api_key"] is True
    assert body["render_key_fallback"] is False
    assert "narrow" not in response.text
    assert "bambuddy_render_api_key" not in json.dumps(body)


def test_choosing_the_bambuddy_store_without_an_inbox_is_refused(client: TestClient) -> None:
    response = client.put("/api/v1/settings", json={"store_backend": "bambuddy"})
    assert response.status_code == 422
    assert "library folder" in response.json()["detail"]
```

In `test_defaults_are_empty_and_the_key_is_absent`, add to the expected dict: `"has_render_api_key": False, "render_key_fallback": False, "store_backend": "local",`.

- [ ] **Step 3: Run to verify they fail**

Run: `cd backend && uv run --frozen pytest tests/test_settings_store.py tests/api/test_settings.py -q`
Expected: FAIL — `ImportError: cannot import name 'RenderStoreSettings'`.

- [ ] **Step 4: Config and env settings**

In `backend/scadbuddy/core/config.py`, beside the asset defaults:

```python
#: Where blobs live (spec 2026-09-27 §6.2). `local` is the data volume and is correct
#: only with one render worker sharing the API's volume; `bambuddy` is Bambuddy's library.
StoreBackend = Literal["local", "bambuddy"]
DEFAULT_STORE_MAX_TOTAL_BYTES = 50 * 1024**3
DEFAULT_STORE_MAX_COUNT = 200_000
#: A worker's local copy of pieces it fetched or rendered; least recently used goes first.
DEFAULT_WORKER_CACHE_MAX_BYTES = 10 * 1024**3
```

(import `Literal` from `typing` if the module does not yet). In the `Config` dataclass add:

```python
    store_max_total_bytes: int = DEFAULT_STORE_MAX_TOTAL_BYTES
    store_max_count: int = DEFAULT_STORE_MAX_COUNT
    worker_cache_max_bytes: int = DEFAULT_WORKER_CACHE_MAX_BYTES
```

In `backend/scadbuddy/core/settings.py`, import the three defaults and `StoreBackend`, then add, right after the `bambuddy_api_key: str | None = None` line (phase 1 has no render-key field; this adds it):

```python
    # SCADBUDDY_BAMBUDDY_RENDER_API_KEY / SCADBUDDY_STORE_BACKEND seed the stored values
    # (`library.settings_store`, ENV_SEEDED), like `bambuddy_api_key`: a value saved in
    # Settings wins. Render workers read them from there (spec §9).
    bambuddy_render_api_key: str | None = None
    store_backend: StoreBackend = "local"
    # SCADBUDDY_STORE_MAX_TOTAL_BYTES / _MAX_COUNT: the store's caps (spec §6.2); a put
    # past either is refused unless the content is already stored. 0 is no limit.
    store_max_total_bytes: int = DEFAULT_STORE_MAX_TOTAL_BYTES
    store_max_count: int = DEFAULT_STORE_MAX_COUNT
    worker_cache_max_bytes: int = DEFAULT_WORKER_CACHE_MAX_BYTES
```

and pass the three caps in `to_config` (`store_max_total_bytes=self.store_max_total_bytes, store_max_count=self.store_max_count, worker_cache_max_bytes=self.worker_cache_max_bytes`).

- [ ] **Step 5: The stored settings**

In `backend/scadbuddy/library/settings_store.py`:

```python
from scadbuddy.core.config import StoreBackend

ENV_SEEDED = (
    "bambuddy_url",
    "bambuddy_api_key",
    "bambuddy_render_api_key",
    "public_url",
    "default_plate",
    "store_backend",
)
#: The fields a render worker reads (spec §9): the store and the key it uses. The full
#: key only when no render key is stored, as the fallback.
RENDER_FIELDS = (
    "store_backend",
    "bambuddy_url",
    "bambuddy_render_api_key",
    "bambuddy_api_key",
    "library_folder_id",
)


class StoreNotReadyError(ValueError):
    """`store_backend = bambuddy` without the Bambuddy URL and inbox folder it needs."""
```

In `StoredSettings`, after `bambuddy_api_key`:

```python
    #: The Manage-Library-only key render workers hold (spec 2026-09-27 §9). Stored
    #: exactly as `bambuddy_api_key` is (the backend has no secret store; see
    #: tests/api/test_settings.py), written from Settings, never sent to the browser.
    bambuddy_render_api_key: str | None = None
    #: Where blobs live. Read at process start by the API and every worker.
    store_backend: StoreBackend = "local"

    @field_validator("store_backend", mode="before")
    @classmethod
    def _cleared_backend_is_local(cls, value: object) -> object:
        # A JSON null row is an env-seeded field the UI cleared: back to the default.
        return "local" if value is None else value

    def render_bambuddy_key(self) -> tuple[str | None, bool]:
        """The key render workers use, and whether it is the full key by fallback."""
        if self.bambuddy_render_api_key:
            return self.bambuddy_render_api_key, False
        return self.bambuddy_api_key, True
```

In `SettingsPatch`, add `bambuddy_render_api_key: str | None = None` and `store_backend: StoreBackend | None = None`.

In `SettingsStore`, add the property and change the start of `save`:

```python
    @property
    def pool(self) -> ConnectionPool[Connection[DictRow]]:
        return self._pool

    def save(self, patch: SettingsPatch) -> StoredSettings:
        changes = patch.model_dump(mode="json", exclude_unset=True)
        for secret in ("bambuddy_api_key", "bambuddy_render_api_key"):
            if changes.get(secret) == "":
                changes[secret] = None
        if changes.get("store_backend") == "bambuddy":
            current = self.load()
            url = changes.get("bambuddy_url", current.bambuddy_url)
            inbox = changes.get("library_folder_id", current.library_folder_id)
            if not url or inbox is None:
                raise StoreNotReadyError(
                    "the Bambuddy store needs a Bambuddy URL and a library folder (its inbox)"
                    " saved first"
                )
        with self._pool.connection() as conn, conn.transaction():
            ...  # unchanged from here
```

At module end:

```python
class RenderStoreSettings(BaseModel):
    """What a render worker knows of the settings, and nothing more (spec §9)."""

    store_backend: StoreBackend = "local"
    bambuddy_url: str | None = None
    api_key: str | None = None
    #: True when `api_key` is the full key because no render key is stored.
    key_is_fallback: bool = True
    library_folder_id: int | None = None


def load_render_store_settings(
    pool: ConnectionPool[Connection[DictRow]], defaults: Settings
) -> RenderStoreSettings:
    """Read only `RENDER_FIELDS`: a worker holds no settings store (spec §9)."""
    with pool.connection() as conn:
        rows = conn.execute(
            "SELECT name, value FROM settings WHERE name = ANY(%s)", (list(RENDER_FIELDS),)
        ).fetchall()
    values: dict[str, Any] = {
        name: getattr(defaults, name) for name in ENV_SEEDED if name in RENDER_FIELDS
    }
    for row in rows:
        values[row["name"]] = row["value"]
    stored = StoredSettings.model_validate(values)
    key, fallback = stored.render_bambuddy_key()
    return RenderStoreSettings(
        store_backend=stored.store_backend,
        bambuddy_url=stored.bambuddy_url,
        api_key=key,
        key_is_fallback=fallback,
        library_folder_id=stored.library_folder_id,
    )
```

- [ ] **Step 6: The settings view**

In `backend/scadbuddy/api/settings.py`, add to `SettingsView` after `has_api_key`:

```python
    has_render_api_key: bool = False
    #: True while render workers would hold the full key (spec §9): a key is stored and
    #: no render key is. The Settings page shows a persistent warning.
    render_key_fallback: bool = False
    store_backend: StoreBackend = "local"
```

In `_view` pass `has_render_api_key=bool(settings.bambuddy_render_api_key)`, `render_key_fallback=bool(settings.bambuddy_api_key) and not settings.bambuddy_render_api_key`, `store_backend=settings.store_backend`. Replace `put_settings`'s body:

```python
    try:
        saved = store.save(patch)
    except StoreNotReadyError as error:
        raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from None
    return _view(saved, state.settings.media_upload_max_bytes)
```

(import `StoreNotReadyError` and `StoreBackend`).

- [ ] **Step 7: Run the tests**

Run: `cd backend && uv run --frozen pytest tests/test_settings_store.py tests/api/test_settings.py tests/test_config.py -q`
Expected: PASS. Then the backend gates from Global Constraints.

- [ ] **Step 8: Generated files**

`SettingsView` changed. In order: `cd frontend && pnpm gen:api && pnpm typecheck`, then `cd agent && pnpm gen:api && pnpm typecheck`. Both pass unchanged (new optional fields). Nothing to commit (#492).

- [ ] **Step 9: Commit**

```bash
git add backend/scadbuddy/core backend/scadbuddy/library/settings_store.py backend/scadbuddy/api/settings.py backend/tests/test_settings_store.py backend/tests/api/test_settings.py
git commit -m "feat(settings): store-backed render key and store backend (#426)"
```

---

### Task 2: The store index and `ContentStore`

**Files:**
- Create: `backend/scadbuddy/migrations/20260928T1800Z_blob_store.sql`
- Create: `backend/scadbuddy/store/content.py`, `backend/scadbuddy/store/index.py`
- Modify: `backend/scadbuddy/store/local.py` (append `LocalContentBackend`), `backend/scadbuddy/core/metrics.py`
- Create: `backend/tests/support/store.py`, `backend/tests/store/__init__.py` (empty), `backend/tests/store/conftest.py`
- Test: `backend/tests/store/test_content_store.py` (`requires_postgres`)

**Interfaces:**
- Produces (`store/content.py`):
  ```python
  BlobKind = Literal["piece", "snapshot", "asset", "font"]
  SWEPT_KINDS: tuple[BlobKind, ...] = ("piece", "snapshot")
  class BlobScope(BaseModel): slug: str | None = None; title: str | None = None; folder: Literal["work", "output"] = "work"; project_id: int | None = None
  class BlobRef(BaseModel): sha256: str; kind: BlobKind; backend: str; backend_id: str; size: int
  class BlobStat(BaseModel): key: str; ref: BlobRef; slug: str | None; meta: dict[str, Any]; touched_at: datetime
  class StoreUsage(BaseModel): backend: str; count: int; bytes: int; max_count: int; max_total_bytes: int; by_kind: dict[str, int]
  class StoreFullError(Exception); class BlobCorruptError(Exception); class BlobMissingError(KeyError)
  class ContentBackend(Protocol): backend: str; async upload(kind, data: bytes, *, name: str, scope: BlobScope) -> str; download(backend_id: str) -> AsyncIterator[bytes]; async exists(backend_id) -> bool; async remove(backend_id) -> None
  class ContentStore:
      index: BlobIndex; backend: ContentBackend
      def __init__(self, backend, index, *, max_total_bytes: int = 0, max_count: int = 0, metrics: Metrics | None = None)
      name: str  (property: backend.backend)
      async def put(self, kind, data: bytes, *, name: str, scope: BlobScope, key: str | None = None, meta: dict[str, Any] | None = None) -> BlobRef
      async def replace(self, key, kind, data, *, name, scope, expected: str | None, meta=None) -> BlobRef | None
      def get(self, ref: BlobRef) -> AsyncIterator[bytes]            # raises BlobCorruptError at the end on a sha mismatch
      async def read(self, ref: BlobRef) -> bytes
      async def stat(self, key: str) -> BlobStat | None
      async def delete(self, key: str) -> None
      async def delete_if_stale(self, key: str, cutoff: datetime) -> bool
      async def forget(self, key: str) -> None                        # index row only
      def list(self, scope: BlobScope) -> AsyncIterator[BlobStat]
      def usage(self) -> StoreUsage
  async def sweep_content(content: ContentStore, refs: BlobRefs, *, grace: float, now: float | None = None) -> list[str]   # a RefusedDeleteError is logged per key, never aborts the sweep
  def template_title(model_dir: Path, fallback: str) -> str
  ```
  `store/index.py`: `Pool = ConnectionPool[Connection[DictRow]]`; `class BlobIndex(pool)` with `get(key) -> BlobStat | None`, `by_sha(sha256, kind, backend) -> BlobRef | None`, `put(key, ref, *, slug, meta) -> None`, `swap(key, ref, *, expected: str | None, slug, meta) -> bool`, `touch(key) -> None`, `delete(key) -> BlobStat | None`, `delete_if_stale(key, cutoff) -> BlobStat | None`, `shares_backend_id(backend, backend_id) -> bool`, `stats(kinds: Sequence[str] | None, slug: str | None = None) -> list[BlobStat]`, `usage(backend) -> tuple[int, int, dict[str, int]]`.
  `store/local.py`: `class LocalContentBackend(root: Path)`, `backend = "local"`.
  `Metrics.store_ops: Counter` labels `op`, `outcome`.
  Tests: `tests/support/store.py` `store_pool(conninfo) -> ContextManager[Pool]`, `local_content(root, pool, **caps) -> ContentStore`; fixture `pool` in `tests/store/conftest.py`.

- [ ] **Step 1: Test support**

`backend/tests/support/store.py`:

```python
"""A Postgres pool with every migration applied, and a store over a directory."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from psycopg import Connection
from psycopg.rows import DictRow, dict_row
from psycopg_pool import ConnectionPool

from scadbuddy.render.pg_store import migrate
from scadbuddy.store.content import ContentStore
from scadbuddy.store.index import BlobIndex, Pool
from scadbuddy.store.local import LocalContentBackend


@contextmanager
def store_pool(conninfo: str) -> Iterator[Pool]:
    pool: Pool = ConnectionPool(
        conninfo,
        min_size=1,
        max_size=6,
        open=True,
        connection_class=Connection[DictRow],
        kwargs={"autocommit": True, "row_factory": dict_row},
    )
    try:
        with pool.connection() as conn:
            migrate(conn)
        yield pool
    finally:
        pool.close()


def local_content(root: Path, pool: Pool, **caps: int) -> ContentStore:
    return ContentStore(LocalContentBackend(root), BlobIndex(pool), **caps)
```

`backend/tests/store/conftest.py`:

```python
from __future__ import annotations

from collections.abc import Iterator

import pytest

from scadbuddy.store.index import Pool
from tests.support.store import store_pool


@pytest.fixture
def pool(pg_conninfo: str) -> Iterator[Pool]:
    with store_pool(pg_conninfo) as opened:
        yield opened
```

- [ ] **Step 2: Write the failing tests**

`backend/tests/store/test_content_store.py`:

```python
"""§6.2's byte store: dedupe, caps, integrity, compare-and-swap, sweep."""

from __future__ import annotations

import hashlib
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from scadbuddy.store.content import (
    BlobCorruptError,
    BlobMissingError,
    BlobScope,
    StoreFullError,
    sweep_content,
)
from scadbuddy.store.index import Pool
from scadbuddy.store.refs import BlobRefs
from tests.support.store import local_content

pytestmark = pytest.mark.requires_postgres
SCOPE = BlobScope(slug="demo", title="Demo")


async def test_put_then_read_and_the_same_bytes_are_stored_once(tmp_path: Path, pool: Pool) -> None:
    store = local_content(tmp_path / "remote", pool)
    first = await store.put("asset", b"<svg/>", name="a.svg", scope=SCOPE)
    again = await store.put("asset", b"<svg/>", name="b.svg", scope=SCOPE)
    assert first == again
    assert await store.read(first) == b"<svg/>"
    usage = store.usage()
    assert (usage.count, usage.bytes, usage.by_kind) == (1, 6, {"asset": 6})
    assert (await store.stat(f"asset-{first.sha256}")) is not None


async def test_put_refuses_past_the_cap_except_a_re_put(tmp_path: Path, pool: Pool) -> None:
    store = local_content(tmp_path / "remote", pool, max_count=1)
    await store.put("asset", b"one", name="1", scope=SCOPE)
    with pytest.raises(StoreFullError, match="SCADBUDDY_STORE_MAX_COUNT"):
        await store.put("asset", b"two", name="2", scope=SCOPE)
    await store.put("asset", b"one", name="1 again", scope=SCOPE)  # already stored: never refused


async def test_altered_or_missing_bytes_are_reported_not_returned(tmp_path: Path, pool: Pool) -> None:
    store = local_content(tmp_path / "remote", pool)
    ref = await store.put("piece", b"real", name="p.zip", scope=SCOPE, key="k")
    (tmp_path / "remote" / ref.backend_id).write_bytes(b"fake")
    with pytest.raises(BlobCorruptError):
        await store.read(ref)
    (tmp_path / "remote" / ref.backend_id).unlink()
    with pytest.raises(BlobMissingError):
        await store.read(ref)
    assert await store.stat("k") is None  # a vanished object drops its index row
    assert store.index.get("k") is None


async def test_replace_is_a_compare_and_swap(tmp_path: Path, pool: Pool) -> None:
    store = local_content(tmp_path / "remote", pool)
    v1 = await store.replace("k", "piece", b"v1", name="p", scope=SCOPE, expected=None)
    assert v1 is not None
    lost = await store.replace("k", "piece", b"other", name="p", scope=SCOPE, expected=None)
    assert lost is None
    assert not (tmp_path / "remote" / "piece" / hashlib.sha256(b"other").hexdigest()).exists()
    v2 = await store.replace("k", "piece", b"v2", name="p", scope=SCOPE, expected=v1.sha256)
    assert v2 is not None and store.index.get("k") is not None
    assert not (tmp_path / "remote" / v1.backend_id).exists()  # the superseded object is gone


async def test_delete_keeps_an_object_another_key_still_names(tmp_path: Path, pool: Pool) -> None:
    store = local_content(tmp_path / "remote", pool)
    ref = await store.put("snapshot", b"same", name="a", scope=SCOPE, key="a")
    await store.put("snapshot", b"same", name="b", scope=SCOPE, key="b")
    await store.delete("a")
    assert await store.read(ref) == b"same"
    await store.delete("b")
    assert not (tmp_path / "remote" / ref.backend_id).exists()


async def test_sweep_takes_only_unreferenced_stale_pieces_and_snapshots(
    tmp_path: Path, pool: Pool
) -> None:
    store = local_content(tmp_path / "remote", pool)
    refs = BlobRefs(pool)
    for key in ("kept", "fresh", "stale"):
        await store.put("piece", key.encode(), name=key, scope=SCOPE, key=key)
    await store.put("asset", b"asset", name="a.svg", scope=SCOPE, key="asset-x")
    refs.add("kept", "job", "j1")
    old = datetime.now(UTC) - timedelta(hours=2)
    with pool.connection() as conn:
        conn.execute(
            "UPDATE store_blobs SET touched_at = %s WHERE key IN ('kept', 'stale', 'asset-x')",
            (old,),
        )
    assert await sweep_content(store, refs, grace=3600, now=time.time()) == ["stale"]
    assert {s.key for s in store.index.stats(None)} == {"kept", "fresh", "asset-x"}
```

- [ ] **Step 3: Run to verify they fail**

Run: `cd backend && uv run --frozen pytest tests/store/test_content_store.py -q`
Expected: FAIL — `ModuleNotFoundError: scadbuddy.store.content`.

- [ ] **Step 4: The migration**

`backend/scadbuddy/migrations/20260928T1800Z_blob_store.sql`:

```sql
-- The blob store's index (spec 2026-09-27 §6.2, §6.3). The bytes live in a backend (the
-- data volume, or Bambuddy's library); what each blob is, where it is and when it was
-- last wanted live here, so a fetch is by id, never a folder scan, and the caps and the
-- Settings usage are one query. `key` is the directory key the activities use
-- (`piece_key`, `src-<slug>-<revision>`, `asset-<sha256>`, `font-<dir>`).
CREATE TABLE store_blobs (
    key         text PRIMARY KEY,
    sha256      text NOT NULL,
    kind        text NOT NULL,
    backend     text NOT NULL,
    backend_id  text NOT NULL,
    size        bigint NOT NULL,
    slug        text,
    meta        jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at  timestamptz NOT NULL DEFAULT now(),
    touched_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX store_blobs_sha ON store_blobs (sha256, kind, backend);
CREATE INDEX store_blobs_object ON store_blobs (backend, backend_id);
CREATE INDEX store_blobs_kind ON store_blobs (kind, touched_at);

-- The Bambuddy folders ScadBuddy created (or adopted) under its inbox. A delete is
-- refused unless the file sits in one recorded with role 'work' (#316).
CREATE TABLE store_folders (
    inbox_id   bigint NOT NULL,
    slug       text NOT NULL,
    role       text NOT NULL CHECK (role IN ('template', 'work')),
    folder_id  bigint NOT NULL UNIQUE,
    PRIMARY KEY (inbox_id, slug, role)
);
```

If `tests/test_pg_migrations.py` pins the list of migration files, add this one there.

- [ ] **Step 5: The index**

`backend/scadbuddy/store/index.py`:

```python
"""`store_blobs`: which blob a key names, where its bytes are, when it was last wanted."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from psycopg import Connection
from psycopg.rows import DictRow
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from scadbuddy.store.content_models import BlobRef, BlobStat

Pool = ConnectionPool[Connection[DictRow]]
_COLUMNS = "key, sha256, kind, backend, backend_id, size, slug, meta, touched_at"


def _stat(row: DictRow) -> BlobStat:
    return BlobStat(
        key=row["key"],
        ref=BlobRef(
            sha256=row["sha256"],
            kind=row["kind"],
            backend=row["backend"],
            backend_id=row["backend_id"],
            size=row["size"],
        ),
        slug=row["slug"],
        meta=row["meta"],
        touched_at=row["touched_at"],
    )


class BlobIndex:
    def __init__(self, pool: Pool) -> None:
        self._pool = pool

    def get(self, key: str) -> BlobStat | None:
        with self._pool.connection() as conn:
            row = conn.execute(f"SELECT {_COLUMNS} FROM store_blobs WHERE key = %s", (key,)).fetchone()
        return _stat(row) if row is not None else None

    def by_sha(self, sha256: str, kind: str, backend: str) -> BlobRef | None:
        with self._pool.connection() as conn:
            row = conn.execute(
                f"SELECT {_COLUMNS} FROM store_blobs WHERE sha256 = %s AND kind = %s"
                " AND backend = %s LIMIT 1",
                (sha256, kind, backend),
            ).fetchone()
        return _stat(row).ref if row is not None else None

    def put(self, key: str, ref: BlobRef, *, slug: str | None, meta: dict[str, Any]) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                "INSERT INTO store_blobs (key, sha256, kind, backend, backend_id, size, slug, meta)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (key) DO UPDATE SET"
                " sha256 = EXCLUDED.sha256, kind = EXCLUDED.kind, backend = EXCLUDED.backend,"
                " backend_id = EXCLUDED.backend_id, size = EXCLUDED.size, slug = EXCLUDED.slug,"
                " meta = EXCLUDED.meta, touched_at = now()",
                (key, ref.sha256, ref.kind, ref.backend, ref.backend_id, ref.size, slug, Jsonb(meta)),
            )

    def swap(
        self, key: str, ref: BlobRef, *, expected: str | None, slug: str | None, meta: dict[str, Any]
    ) -> bool:
        """Point ``key`` at ``ref`` only if it still names ``expected`` (None: no row)."""
        values = (ref.sha256, ref.kind, ref.backend, ref.backend_id, ref.size, slug, Jsonb(meta))
        with self._pool.connection() as conn:
            if expected is None:
                cursor = conn.execute(
                    "INSERT INTO store_blobs (sha256, kind, backend, backend_id, size, slug, meta, key)"
                    " VALUES (%s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (key) DO NOTHING",
                    (*values, key),
                )
            else:
                cursor = conn.execute(
                    "UPDATE store_blobs SET sha256 = %s, kind = %s, backend = %s, backend_id = %s,"
                    " size = %s, slug = %s, meta = %s, touched_at = now()"
                    " WHERE key = %s AND sha256 = %s",
                    (*values, key, expected),
                )
            return cursor.rowcount == 1

    def touch(self, key: str) -> None:
        with self._pool.connection() as conn:
            conn.execute("UPDATE store_blobs SET touched_at = now() WHERE key = %s", (key,))

    def delete(self, key: str) -> BlobStat | None:
        with self._pool.connection() as conn:
            row = conn.execute(
                f"DELETE FROM store_blobs WHERE key = %s RETURNING {_COLUMNS}", (key,)
            ).fetchone()
        return _stat(row) if row is not None else None

    def delete_if_stale(self, key: str, cutoff: datetime) -> BlobStat | None:
        """Atomic against a concurrent `touch`: a blob claimed after the sweep's snapshot
        has a fresh `touched_at` and is not matched."""
        with self._pool.connection() as conn:
            row = conn.execute(
                f"DELETE FROM store_blobs WHERE key = %s AND touched_at <= %s RETURNING {_COLUMNS}",
                (key, cutoff),
            ).fetchone()
        return _stat(row) if row is not None else None

    def shares_backend_id(self, backend: str, backend_id: str) -> bool:
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT EXISTS (SELECT 1 FROM store_blobs WHERE backend = %s AND backend_id = %s)"
                " AS shared",
                (backend, backend_id),
            ).fetchone()
        return bool(row and row["shared"])

    def stats(self, kinds: Sequence[str] | None, slug: str | None = None) -> list[BlobStat]:
        with self._pool.connection() as conn:
            rows = conn.execute(
                f"SELECT {_COLUMNS} FROM store_blobs"
                " WHERE (%s::text[] IS NULL OR kind = ANY(%s::text[]))"
                " AND (%s::text IS NULL OR slug = %s) ORDER BY key",
                (list(kinds) if kinds is not None else None,) * 2 + (slug, slug),
            ).fetchall()
        return [_stat(row) for row in rows]

    def usage(self, backend: str) -> tuple[int, int, dict[str, int]]:
        """Distinct stored objects and their bytes, in total and per kind."""
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT kind, count(*) AS n, coalesce(sum(size), 0) AS b FROM ("
                " SELECT DISTINCT ON (backend_id) backend_id, kind, size FROM store_blobs"
                " WHERE backend = %s) AS objects GROUP BY kind",
                (backend,),
            ).fetchall()
        by_kind = {row["kind"]: int(row["b"]) for row in rows}
        return sum(int(row["n"]) for row in rows), sum(by_kind.values()), by_kind
```

The models live in a module of their own so `index.py` and `content.py` do not import each other. `backend/scadbuddy/store/content_models.py`:

```python
"""The store's value types (spec 2026-09-27 §6.2). `Scope` is `BlobScope` here:
`bambuddy.errors.Scope` already names Bambuddy's API-key scopes."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel

BlobKind = Literal["piece", "snapshot", "asset", "font"]
#: Kinds whose life the sweep decides: pieces by `blob_refs` and grace, snapshots (a
#: cache of git) by grace alone. Assets have `AssetStore.sweep`; fonts are never swept.
SWEPT_KINDS: tuple[BlobKind, ...] = ("piece", "snapshot")


class BlobScope(BaseModel):
    #: None for what belongs to no template (fonts).
    slug: str | None = None
    #: The template's display name, which names its Bambuddy folder.
    title: str | None = None
    folder: Literal["work", "output"] = "work"
    #: With `folder="output"`: the project whose folder receives the file (#317).
    project_id: int | None = None


class BlobRef(BaseModel):
    sha256: str
    kind: BlobKind
    backend: str
    backend_id: str
    size: int


class BlobStat(BaseModel):
    key: str
    ref: BlobRef
    slug: str | None
    meta: dict[str, Any]
    touched_at: datetime


class StoreUsage(BaseModel):
    """What the store holds against its caps; Settings shows it (spec §10)."""

    backend: str
    count: int
    bytes: int
    max_count: int
    max_total_bytes: int
    #: Bytes per kind.
    by_kind: dict[str, int]
```

Append the exceptions to `content_models.py` too, so the backends can raise them without importing `content.py`:

```python
class StoreFullError(Exception):
    """A put would take the store past SCADBUDDY_STORE_MAX_TOTAL_BYTES / _MAX_COUNT."""


class BlobCorruptError(Exception):
    """The backend returned bytes whose sha256 is not the one recorded."""


class BlobMissingError(KeyError):
    """The backend no longer has the object (deleted behind ScadBuddy's back)."""


class RefusedDeleteError(RuntimeError):
    """A delete aimed at a file outside a ScadBuddy `Work/` folder (spec §6.3). Raised
    by the Bambuddy backend (Task 4); here so `sweep_content` can catch it per key."""
```

`content.py` re-exports every name.

- [ ] **Step 6: `ContentStore`**

`backend/scadbuddy/store/content.py`:

```python
"""The byte store under the blob store (spec 2026-09-27 §6.2).

Phase 1's `BlobStore` is directory-shaped: a piece renders into `dir_for(key)`. This is
§6.2's byte-stream interface, named `ContentStore` because `BlobStore` is taken:
`put/get/stat/delete/list` over a backend, plus the Postgres index that makes a fetch
by id, the caps and the usage O(1). `store/cache.py` puts the directory shape back on
top for processes that share no volume. `put` takes bytes, not a stream: every caller
holds the blob in memory already (a packed directory, a sanitised upload).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from scadbuddy.store.content_models import (
    SWEPT_KINDS,
    BlobCorruptError,
    BlobKind,
    BlobMissingError,
    BlobRef,
    BlobScope,
    BlobStat,
    RefusedDeleteError,
    StoreFullError,
    StoreUsage,
)
from scadbuddy.store.index import BlobIndex
from scadbuddy.store.refs import BlobRefs

if TYPE_CHECKING:
    from scadbuddy.core.metrics import Metrics

logger = logging.getLogger(__name__)


class ContentBackend(Protocol):
    backend: str

    async def upload(self, kind: BlobKind, data: bytes, *, name: str, scope: BlobScope) -> str: ...
    def download(self, backend_id: str) -> AsyncIterator[bytes]: ...
    async def exists(self, backend_id: str) -> bool: ...
    async def remove(self, backend_id: str) -> None: ...


def template_title(model_dir: Path, fallback: str) -> str:
    """A template's display name from its `model.json`, for its Bambuddy folder."""
    try:
        name = json.loads((model_dir / "model.json").read_text(encoding="utf-8")).get("name")
    except (OSError, ValueError, AttributeError):
        return fallback
    return name.strip() if isinstance(name, str) and name.strip() else fallback


class ContentStore:
    def __init__(
        self,
        backend: ContentBackend,
        index: BlobIndex,
        *,
        max_total_bytes: int = 0,
        max_count: int = 0,
        metrics: Metrics | None = None,
    ) -> None:
        self.backend = backend
        self.index = index
        self.max_total_bytes = max_total_bytes
        self.max_count = max_count
        self.metrics = metrics

    @property
    def name(self) -> str:
        return self.backend.backend

    def _count(self, op: str, outcome: str) -> None:
        if self.metrics is not None:
            self.metrics.store_ops.labels(op, outcome).inc()

    def usage(self) -> StoreUsage:
        count, total, by_kind = self.index.usage(self.name)
        return StoreUsage(
            backend=self.name,
            count=count,
            bytes=total,
            max_count=self.max_count,
            max_total_bytes=self.max_total_bytes,
            by_kind=by_kind,
        )

    def _require_room(self, size: int) -> None:
        usage = self.usage()
        if self.max_count and usage.count + 1 > self.max_count:
            raise StoreFullError(
                f"the store holds {usage.count} blobs, the most SCADBUDDY_STORE_MAX_COUNT"
                f" ({self.max_count}) allows; unreferenced blobs go after the sweep's grace"
            )
        if self.max_total_bytes and usage.bytes + size > self.max_total_bytes:
            raise StoreFullError(
                f"storing {size} bytes would take the store to {usage.bytes + size} bytes,"
                f" past SCADBUDDY_STORE_MAX_TOTAL_BYTES ({self.max_total_bytes})"
            )

    async def _store(self, kind: BlobKind, data: bytes, *, name: str, scope: BlobScope) -> BlobRef:
        sha = hashlib.sha256(data).hexdigest()
        existing = await asyncio.to_thread(self.index.by_sha, sha, kind, self.name)
        if existing is not None and await self.backend.exists(existing.backend_id):
            return existing  # a re-put: never refused, never uploaded twice
        try:
            await asyncio.to_thread(self._require_room, len(data))
        except StoreFullError:
            self._count("put", "full")
            raise
        backend_id = await self.backend.upload(kind, data, name=name, scope=scope)
        self._count("put", "ok")
        return BlobRef(sha256=sha, kind=kind, backend=self.name, backend_id=backend_id, size=len(data))

    async def _release(self, ref: BlobRef) -> None:
        """Remove the object unless an index row still names it."""
        if not await asyncio.to_thread(self.index.shares_backend_id, ref.backend, ref.backend_id):
            await self.backend.remove(ref.backend_id)
            self._count("delete", "ok")

    async def put(
        self,
        kind: BlobKind,
        data: bytes,
        *,
        name: str,
        scope: BlobScope,
        key: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> BlobRef:
        ref = await self._store(kind, data, name=name, scope=scope)
        key = key or f"{kind}-{ref.sha256}"
        previous = await asyncio.to_thread(self.index.get, key)
        await asyncio.to_thread(self.index.put, key, ref, slug=scope.slug, meta=meta or {})
        if previous is not None and previous.ref.backend_id != ref.backend_id:
            await self._release(previous.ref)
        return ref

    async def replace(
        self,
        key: str,
        kind: BlobKind,
        data: bytes,
        *,
        name: str,
        scope: BlobScope,
        expected: str | None,
        meta: dict[str, Any] | None = None,
    ) -> BlobRef | None:
        """`put` under ``key`` only if the key still names ``expected``; None if it
        moved on, and this call's own upload is removed again."""
        ref = await self._store(kind, data, name=name, scope=scope)
        previous = await asyncio.to_thread(self.index.get, key)
        landed = await asyncio.to_thread(
            self.index.swap, key, ref, expected=expected, slug=scope.slug, meta=meta or {}
        )
        if not landed:
            await self._release(ref)
            return None
        if previous is not None and previous.ref.backend_id != ref.backend_id:
            await self._release(previous.ref)
        return ref

    async def get(self, ref: BlobRef) -> AsyncIterator[bytes]:
        digest = hashlib.sha256()
        try:
            async for chunk in self.backend.download(ref.backend_id):
                digest.update(chunk)
                yield chunk
        except BlobMissingError:
            self._count("get", "missing")
            raise
        if digest.hexdigest() != ref.sha256:
            self._count("get", "corrupt")
            raise BlobCorruptError(f"{ref.backend}:{ref.backend_id} is not sha256 {ref.sha256}")
        self._count("get", "ok")

    async def read(self, ref: BlobRef) -> bytes:
        return b"".join([chunk async for chunk in self.get(ref)])

    async def stat(self, key: str) -> BlobStat | None:
        stat = await asyncio.to_thread(self.index.get, key)
        if stat is None:
            return None
        if not await self.backend.exists(stat.ref.backend_id):
            await self.forget(key)
            return None
        return stat

    async def forget(self, key: str) -> None:
        await asyncio.to_thread(self.index.delete, key)

    async def delete(self, key: str) -> None:
        stat = await asyncio.to_thread(self.index.delete, key)
        if stat is not None:
            await self._release(stat.ref)

    async def delete_if_stale(self, key: str, cutoff: datetime) -> bool:
        stat = await asyncio.to_thread(self.index.delete_if_stale, key, cutoff)
        if stat is None:
            return False
        await self._release(stat.ref)
        return True

    async def list(self, scope: BlobScope) -> AsyncIterator[BlobStat]:
        for stat in await asyncio.to_thread(self.index.stats, None, scope.slug):
            yield stat


async def sweep_content(
    content: ContentStore, refs: BlobRefs, *, grace: float, now: float | None = None
) -> list[str]:
    """Remove every swept-kind blob nothing references and nothing touched for ``grace``."""
    cutoff = datetime.fromtimestamp((time.time() if now is None else now) - grace, UTC)
    kept = await asyncio.to_thread(refs.referenced)
    removed: list[str] = []
    for stat in await asyncio.to_thread(content.index.stats, SWEPT_KINDS):
        if stat.key in kept or stat.touched_at > cutoff:
            continue
        try:
            if await content.delete_if_stale(stat.key, cutoff):
                removed.append(stat.key)
        except RefusedDeleteError:
            # The index row is gone; the object is outside a Work/ folder and stays.
            # One refused key must not stop the rest of the sweep.
            logger.exception("the backend refused to delete a swept blob", extra={"key": stat.key})
    return removed


__all__ = [
    "SWEPT_KINDS",
    "BlobCorruptError",
    "BlobKind",
    "BlobMissingError",
    "BlobRef",
    "BlobScope",
    "BlobStat",
    "ContentBackend",
    "ContentStore",
    "RefusedDeleteError",
    "StoreFullError",
    "StoreUsage",
    "sweep_content",
    "template_title",
]
```

- [ ] **Step 7: `LocalContentBackend` and the metric**

Append to `backend/scadbuddy/store/local.py`:

```python
import asyncio
import hashlib
import uuid
from collections.abc import AsyncIterator

from scadbuddy.store.content_models import BlobKind, BlobMissingError, BlobScope

_OBJECT = re.compile(r"^(piece|snapshot|asset|font)/[0-9a-f]{64}$")
_CHUNK = 1 << 20


class LocalContentBackend:
    """Objects under a directory, one file per sha256: tests, `verify.sh`, and the
    stand-in for a remote backend in the worker-cache tests."""

    backend = "local"

    def __init__(self, root: Path) -> None:
        self.root = root

    def _path(self, backend_id: str) -> Path:
        if not _OBJECT.match(backend_id):
            raise ValueError(f"not a local object id: {backend_id!r}")
        return self.root / backend_id

    async def upload(self, kind: BlobKind, data: bytes, *, name: str, scope: BlobScope) -> str:
        backend_id = f"{kind}/{hashlib.sha256(data).hexdigest()}"
        path = self._path(backend_id)

        def write() -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            staging = path.with_name(f".{path.name}.{uuid.uuid4().hex}")
            staging.write_bytes(data)
            os.replace(staging, path)

        await asyncio.to_thread(write)
        return backend_id

    async def download(self, backend_id: str) -> AsyncIterator[bytes]:
        try:
            data = await asyncio.to_thread(self._path(backend_id).read_bytes)
        except FileNotFoundError:
            raise BlobMissingError(backend_id) from None
        for start in range(0, len(data), _CHUNK):
            yield data[start : start + _CHUNK]

    async def exists(self, backend_id: str) -> bool:
        return await asyncio.to_thread(self._path(backend_id).is_file)

    async def remove(self, backend_id: str) -> None:
        await asyncio.to_thread(self._path(backend_id).unlink, missing_ok=True)
```

(Merge these imports into the module's existing import block.)

In `backend/scadbuddy/core/metrics.py`, beside `assets_stored` (same `registry=r` pattern):

```python
        self.store_ops = Counter(
            "scadbuddy_store_operations_total",
            "Blob store calls by operation (put, get, delete) and outcome"
            " (ok, full, corrupt, missing).",
            ["op", "outcome"],
            registry=r,
        )
```

- [ ] **Step 8: Run the tests**

Run: `cd backend && SCADBUDDY_TEST_DATABASE_URL=… uv run --frozen pytest tests/store/test_content_store.py -q`
Expected: PASS (6). Then the backend gates.

- [ ] **Step 9: Commit**

```bash
git add backend/scadbuddy/migrations/20260928T1800Z_blob_store.sql backend/scadbuddy/store backend/scadbuddy/core/metrics.py backend/tests/support/store.py backend/tests/store
git commit -m "feat(store): ContentStore over a backend, with its index, caps and sweep in Postgres (#426)"
```

---

### Task 3: Bambuddy client — upload any file type, download by id

**Files:**
- Modify: `backend/scadbuddy/bambuddy/client.py`, `backend/scadbuddy/bambuddy/models.py`
- Modify: `backend/tests/bambuddy/conftest.py`
- Test: `backend/tests/bambuddy/test_library_blobs.py`

**Interfaces:**
- Produces: `BambuddyClient.upload_library_file(filename, content, *, folder_id=None, media_type: str = THREE_MF_MEDIA_TYPE) -> LibraryFile`; `BambuddyClient.download_library_file(file_id: int) -> AsyncIterator[bytes]` (raises `ApiError`; 404 → `status == 404`); `LibraryFile.folder_id: int | None`, `LibraryFile.file_hash: str | None`. Test helpers `recorded_schema(name) -> dict[str, Any]`, `shaped(schema, **values) -> dict[str, Any]` in `tests/bambuddy/conftest.py`.

- [ ] **Step 1: Test helpers**

Append to `backend/tests/bambuddy/conftest.py`:

```python
def recorded_schema(name: str) -> dict[str, Any]:
    spec = json.loads((RECORDINGS / "openapi" / "scadbuddy-routes.json").read_text(encoding="utf-8"))
    schema: dict[str, Any] = spec["components"]["schemas"][name]
    return schema


def shaped(schema: str, **values: Any) -> dict[str, Any]:
    """A response body built inline for a call the live instance was never asked to make
    (a POST; see recordings/README.md), checked field by field against Bambuddy's
    recorded schema so a misspelt field fails here rather than in production."""
    unknown = set(values) - set(recorded_schema(schema)["properties"])
    assert not unknown, f"{schema} has no field {sorted(unknown)}"
    return values
```

- [ ] **Step 2: Write the failing tests**

`backend/tests/bambuddy/test_library_blobs.py`:

```python
"""The library calls the blob store makes (spec 2026-09-27 §6.3)."""

from __future__ import annotations

import httpx
import pytest
import respx

from scadbuddy.bambuddy.client import BambuddyClient
from scadbuddy.bambuddy.errors import NOT_FOUND_PROBLEM, SCOPE_PROBLEM
from scadbuddy.core.problems import ApiError
from tests.bambuddy.conftest import BASE_URL, shaped

API = f"{BASE_URL}/api/v1"


@respx.mock
async def test_an_svg_uploads_with_its_own_media_type_into_a_folder(bambuddy: BambuddyClient) -> None:
    route = respx.post(f"{API}/library/files").mock(
        return_value=httpx.Response(
            200, json=shaped("FileUploadResponse", id=41, filename="asset-x.svg", file_size=6)
        )
    )
    uploaded = await bambuddy.upload_library_file(
        "asset-x.svg", b"<svg/>", folder_id=11, media_type="image/svg+xml"
    )
    assert uploaded.id == 41
    request = route.calls.last.request
    assert request.url.params["folder_id"] == "11"
    assert b'filename="asset-x.svg"' in request.content
    assert b"Content-Type: image/svg+xml" in request.content


@respx.mock
async def test_a_file_downloads_by_id_without_a_folder_scan(bambuddy: BambuddyClient) -> None:
    respx.get(f"{API}/library/files/41/download").mock(
        return_value=httpx.Response(200, content=b"x" * 100_000)
    )
    folders = respx.get(f"{API}/library/folders")
    chunks = [chunk async for chunk in bambuddy.download_library_file(41)]
    assert b"".join(chunks) == b"x" * 100_000
    assert not folders.called


@respx.mock
async def test_a_missing_file_is_a_404_problem(bambuddy: BambuddyClient) -> None:
    respx.get(f"{API}/library/files/9/download").mock(
        return_value=httpx.Response(404, json={"detail": "File not found"})
    )
    with pytest.raises(ApiError) as caught:
        async for _ in bambuddy.download_library_file(9):
            pass
    assert caught.value.status == 404 and caught.value.type == NOT_FOUND_PROBLEM


@respx.mock
async def test_a_key_without_manage_library_is_named(bambuddy: BambuddyClient) -> None:
    respx.post(f"{API}/library/files").mock(
        return_value=httpx.Response(403, json={"detail": "Forbidden"})
    )
    with pytest.raises(ApiError) as caught:
        await bambuddy.upload_library_file("a.zip", b"z", folder_id=1, media_type="application/zip")
    assert caught.value.type == SCOPE_PROBLEM and "Manage Library" in caught.value.detail


def test_a_library_file_reads_its_folder_and_hash() -> None:
    from scadbuddy.bambuddy.models import LibraryFile

    row = shaped("FileResponse", id=5, filename="a.zip", folder_id=11, file_hash="ab" * 32)
    parsed = LibraryFile.model_validate(row)
    assert (parsed.folder_id, parsed.file_hash) == (11, "ab" * 32)
```

- [ ] **Step 3: Run to verify they fail**

Run: `cd backend && uv run --frozen pytest tests/bambuddy/test_library_blobs.py -q`
Expected: FAIL — `TypeError: upload_library_file() got an unexpected keyword argument 'media_type'`.

- [ ] **Step 4: Implement**

In `backend/scadbuddy/bambuddy/models.py`, `LibraryFile`, after `duplicate_of`:

```python
    #: The folder the file sits in (``FileResponse``); the blob store deletes only files
    #: in a `Work/` folder it made (spec 2026-09-27 §6.3).
    folder_id: int | None = None
    #: Bambuddy's own content hash. Whether it is a sha256 is one of §6.3's measurements
    #: (`python -m scadbuddy.store.verify_bambuddy`); ScadBuddy checks its own.
    file_hash: str | None = None
```

In `backend/scadbuddy/bambuddy/client.py`, change `upload_library_file`:

```python
    async def upload_library_file(
        self,
        filename: str,
        content: bytes,
        *,
        folder_id: int | None = None,
        media_type: str = THREE_MF_MEDIA_TYPE,
    ) -> LibraryFile:
        response = await self._send(
            "POST",
            "/library/files",
            scope=Scope.MANAGE_LIBRARY,
            what=f"upload {filename}",
            params={"folder_id": folder_id} if folder_id is not None else None,
            files={"file": (filename, content, media_type)},
            timeout=self.config.upload_timeout,
        )
        return LibraryFile.model_validate(response.json())
```

and add after `library_file`:

```python
    async def download_library_file(self, file_id: int) -> AsyncIterator[bytes]:
        """``GET /library/files/{id}/download`` (``openapi/routes.txt``): by id, so the
        blob store never scans a folder to find a file (spec 2026-09-27 §6.3)."""
        what = f"download library file {file_id}"
        try:
            async with self._http.stream(
                "GET",
                self.config.url(f"/library/files/{file_id}/download"),
                headers=self._headers,
                timeout=self.config.upload_timeout,
            ) as response:
                if not response.is_success:
                    await response.aread()
                    raise map_response(response, scope=Scope.MANAGE_LIBRARY, what=what)
                async for chunk in response.aiter_bytes():
                    yield chunk
        except httpx.HTTPError as error:
            raise map_transport(error, what=what) from error
```

- [ ] **Step 5: Run the tests**

Run: `cd backend && uv run --frozen pytest tests/bambuddy -q`
Expected: PASS (the new five and every existing client test).

- [ ] **Step 6: Commit**

```bash
git add backend/scadbuddy/bambuddy/client.py backend/scadbuddy/bambuddy/models.py backend/tests/bambuddy/conftest.py backend/tests/bambuddy/test_library_blobs.py
git commit -m "feat(bambuddy): upload any file type and download a library file by id (#426)"
```

---

### Task 4: The Bambuddy backend

**Files:**
- Create: `backend/scadbuddy/store/bambuddy.py`, `backend/scadbuddy/store/verify_bambuddy.py`
- Test: `backend/tests/store/test_bambuddy_backend.py` (`requires_postgres`, respx)

**Interfaces:**
- Consumes: `RenderStoreSettings`, `load_render_store_settings`, `Pool` (Task 1); `BlobScope`, `BlobKind`, `BlobMissingError` (Task 2); `BambuddyClient.upload_library_file(..., media_type=)`, `download_library_file`, `library_file`, `delete_library_file`, `folders`, `create_folder`, `folders_by_project` (Task 3 and existing).
- Produces:
  ```python
  @dataclass(frozen=True) class BambuddyTarget: config: BambuddyConfig; inbox_id: int
  class RenderSettingsSource:
      def __init__(self, pool: Pool, defaults: Settings, *, ttl: float = 30.0, clock: Callable[[], float] = time.monotonic)
      async def current(self) -> RenderStoreSettings
      def invalidate(self) -> None
      async def target(self) -> BambuddyTarget          # raises not_configured(...) without URL or inbox
  RefusedDeleteError   # defined in content_models.py (Task 2), re-exported here
  def folder_name(title: str) -> str
  class BambuddyContentBackend:                         # a ContentBackend
      backend = "bambuddy"
      def __init__(self, target: Callable[[], Awaitable[BambuddyTarget]], pool: Pool, *, http: httpx.AsyncClient | None = None)
      async def aclose(self) -> None
  WORK = "Work"; SHARED_TITLE = "Shared"
  ```

- [ ] **Step 1: Write the failing tests**

`backend/tests/store/test_bambuddy_backend.py`:

```python
"""The Bambuddy backend against recorded/shaped responses (spec 2026-09-27 §6.3)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from pathlib import Path

import httpx
import pytest
import respx
from psycopg.types.json import Jsonb

from scadbuddy.bambuddy.client import BambuddyConfig
from scadbuddy.core.settings import Settings
from scadbuddy.store.bambuddy import (
    BambuddyContentBackend,
    BambuddyTarget,
    RefusedDeleteError,
    RenderSettingsSource,
    folder_name,
)
from scadbuddy.store.content import BlobMissingError, BlobScope
from scadbuddy.store.index import Pool
from tests.bambuddy.conftest import BASE_URL, shaped
from tests.conftest import UNUSED_DATABASE_URL

pytestmark = pytest.mark.requires_postgres
API = f"{BASE_URL}/api/v1"
INBOX = 1
SCOPE = BlobScope(slug="dollhouse-kit", title="Dollhouse Kit")


def target(key: str = "narrow") -> Callable[[], Awaitable[BambuddyTarget]]:
    async def current() -> BambuddyTarget:
        return BambuddyTarget(config=BambuddyConfig(base_url=BASE_URL, api_key=key), inbox_id=INBOX)

    return current


def inbox_tree() -> httpx.Response:
    return httpx.Response(200, json=[{"id": INBOX, "name": "ScadBuddy", "children": []}])


async def create_folder(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    await asyncio.sleep(0.05)  # long enough for a second worker to arrive meanwhile
    ids = {"Dollhouse Kit": 10, "Work": 11}
    return httpx.Response(200, json={"id": ids[body["name"]], **body})


def uploaded(file_id: int = 500) -> httpx.Response:
    return httpx.Response(200, json=shaped("FileUploadResponse", id=file_id, filename="f", file_size=1))


@respx.mock
async def test_the_first_upload_makes_the_template_folder_and_work(pool: Pool) -> None:
    respx.get(f"{API}/library/folders").mock(return_value=inbox_tree())
    created = respx.post(f"{API}/library/folders/").mock(side_effect=create_folder)
    upload = respx.post(f"{API}/library/files").mock(return_value=uploaded())
    backend = BambuddyContentBackend(target(), pool)
    assert await backend.upload("piece", b"zip", name="piece-k.zip", scope=SCOPE) == "500"
    await backend.upload("piece", b"zip2", name="piece-j.zip", scope=SCOPE)
    assert [json.loads(c.request.content) for c in created.calls] == [
        {"name": "Dollhouse Kit", "parent_id": INBOX},
        {"name": "Work", "parent_id": 10},
    ]
    request = upload.calls[0].request
    assert request.url.params["folder_id"] == "11"
    assert request.headers["X-API-Key"] == "narrow"
    assert b"application/zip" in request.content
    await backend.aclose()


@respx.mock
async def test_concurrent_first_uploads_create_one_folder_pair(pool: Pool) -> None:
    respx.get(f"{API}/library/folders").mock(return_value=inbox_tree())
    created = respx.post(f"{API}/library/folders/").mock(side_effect=create_folder)
    respx.post(f"{API}/library/files").mock(return_value=uploaded())
    workers = [BambuddyContentBackend(target(), pool) for _ in range(2)]
    await asyncio.gather(
        *(w.upload("piece", b"z", name="piece-k.zip", scope=SCOPE) for w in workers)
    )
    assert created.call_count == 2  # one template folder, one Work, however many workers
    for w in workers:
        await w.aclose()


@respx.mock
async def test_an_existing_folder_of_that_name_is_adopted(pool: Pool) -> None:
    respx.get(f"{API}/library/folders").mock(
        return_value=httpx.Response(200, json=[{"id": INBOX, "name": "ScadBuddy", "children": [
            {"id": 20, "name": "Dollhouse Kit", "parent_id": INBOX, "children": [
                {"id": 21, "name": "Work", "parent_id": 20}]}]}])
    )
    created = respx.post(f"{API}/library/folders/")
    upload = respx.post(f"{API}/library/files").mock(return_value=uploaded())
    backend = BambuddyContentBackend(target(), pool)
    await backend.upload("snapshot", b"src", name="src-1.zip", scope=SCOPE)
    assert not created.called
    assert upload.calls[0].request.url.params["folder_id"] == "21"
    await backend.aclose()


def test_folder_names_carry_no_dots_or_separators() -> None:
    assert folder_name(".hidden/Kit\\2") == "hidden Kit 2"
    assert folder_name("...") == "Template"


@respx.mock
async def test_delete_outside_a_work_folder_is_refused_without_a_request(pool: Pool) -> None:
    respx.get(f"{API}/library/files/77").mock(
        return_value=httpx.Response(200, json=shaped("FileResponse", id=77, filename="house.3mf", folder_id=99))
    )
    delete = respx.delete(f"{API}/library/files/77")
    backend = BambuddyContentBackend(target(), pool)
    with pytest.raises(RefusedDeleteError):
        await backend.remove("77")
    assert not delete.called
    await backend.aclose()


@respx.mock
async def test_delete_in_work_is_sent_and_an_already_gone_file_is_fine(pool: Pool) -> None:
    respx.get(f"{API}/library/folders").mock(return_value=inbox_tree())
    respx.post(f"{API}/library/folders/").mock(side_effect=create_folder)
    respx.post(f"{API}/library/files").mock(return_value=uploaded(500))
    respx.get(f"{API}/library/files/500").mock(
        return_value=httpx.Response(200, json=shaped("FileResponse", id=500, filename="p", folder_id=11))
    )
    delete = respx.delete(f"{API}/library/files/500").mock(return_value=httpx.Response(200, json={}))
    respx.get(f"{API}/library/files/501").mock(return_value=httpx.Response(404, json={"detail": "gone"}))
    backend = BambuddyContentBackend(target(), pool)
    await backend.upload("piece", b"z", name="piece-k.zip", scope=SCOPE)
    await backend.remove("500")
    await backend.remove("501")
    assert delete.call_count == 1
    await backend.aclose()


@respx.mock
async def test_a_missing_download_is_blob_missing(pool: Pool) -> None:
    respx.get(f"{API}/library/files/9/download").mock(return_value=httpx.Response(404, json={"detail": "x"}))
    backend = BambuddyContentBackend(target(), pool)
    with pytest.raises(BlobMissingError):
        async for _ in backend.download("9"):
            pass
    await backend.aclose()


@respx.mock
async def test_a_work_folder_deleted_in_bambuddy_is_made_again(pool: Pool) -> None:
    respx.get(f"{API}/library/folders").mock(return_value=inbox_tree())
    created = respx.post(f"{API}/library/folders/").mock(side_effect=create_folder)
    upload = respx.post(f"{API}/library/files").mock(
        side_effect=[uploaded(1), httpx.Response(404, json={"detail": "Folder not found"}), uploaded(2)]
    )
    backend = BambuddyContentBackend(target(), pool)
    await backend.upload("piece", b"a", name="a.zip", scope=SCOPE)
    assert await backend.upload("piece", b"b", name="b.zip", scope=SCOPE) == "2"
    assert created.call_count == 4 and upload.call_count == 3
    await backend.aclose()


@respx.mock
async def test_a_rotated_render_key_is_used_without_a_restart(pool: Pool, tmp_path: Path) -> None:
    def put(name: str, value: object) -> None:
        with pool.connection() as conn:
            conn.execute(
                "INSERT INTO settings (name, value) VALUES (%s, %s)"
                " ON CONFLICT (name) DO UPDATE SET value = EXCLUDED.value",
                (name, Jsonb(value)),
            )

    put("bambuddy_url", BASE_URL)
    put("library_folder_id", INBOX)
    put("bambuddy_render_api_key", "old")
    source = RenderSettingsSource(
        pool, Settings(data_dir=tmp_path, database_url=UNUSED_DATABASE_URL), ttl=0
    )
    route = respx.get(f"{API}/library/files/5").mock(
        return_value=httpx.Response(200, json=shaped("FileResponse", id=5, filename="a.zip"))
    )
    backend = BambuddyContentBackend(source.target, pool)
    await backend.exists("5")
    put("bambuddy_render_api_key", "new")
    await backend.exists("5")
    assert [c.request.headers["X-API-Key"] for c in route.calls] == ["old", "new"]
    put("bambuddy_render_api_key", None)
    put("bambuddy_api_key", "full")
    assert (await source.current()).key_is_fallback is True
    await backend.aclose()
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd backend && SCADBUDDY_TEST_DATABASE_URL=… uv run --frozen pytest tests/store/test_bambuddy_backend.py -q`
Expected: FAIL — `ModuleNotFoundError: scadbuddy.store.bambuddy`.

- [ ] **Step 3: Implement**

`backend/scadbuddy/store/bambuddy.py`:

```python
"""The Bambuddy backend of the blob store (spec 2026-09-27 §6.3).

Layout (#316, #317): the Settings `library_folder_id` folder is ScadBuddy's inbox and
the only place it deletes from. Under it, one folder per template named by its title,
and under that `Work/`, where pieces, snapshots and assets live; fonts, which belong to
no template, live in `Shared/Work/`. A project's folder is the user's record: an output
may be written there (`folder="output"` with a `project_id`), nothing there is moved or
deleted. No dot-named folders: Bambuddy shows them.

Every folder ScadBuddy makes or adopts is recorded in `store_folders`, and a delete is
refused unless the file sits in one recorded as `work`. What each file is lives in
`store_blobs`, so a fetch is by file id, never a folder scan.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import httpx

from scadbuddy.bambuddy.client import BambuddyClient, BambuddyConfig
from scadbuddy.bambuddy.errors import not_configured
from scadbuddy.bambuddy.models import FolderCreate
from scadbuddy.core.problems import ApiError
from scadbuddy.core.settings import Settings
from scadbuddy.library.settings_store import RenderStoreSettings, load_render_store_settings
from scadbuddy.store.content_models import BlobKind, BlobMissingError, BlobScope
from scadbuddy.store.content_models import RefusedDeleteError as RefusedDeleteError
from scadbuddy.store.index import Pool

WORK = "Work"
SHARED_TITLE = "Shared"
_MEDIA_TYPES = {
    ".zip": "application/zip",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".3mf": "model/3mf",
}


@dataclass(frozen=True)
class BambuddyTarget:
    config: BambuddyConfig
    inbox_id: int


def folder_name(title: str) -> str:
    """A Bambuddy folder name from a template title: no separators, no leading dots."""
    cleaned = re.sub(r"\s+", " ", re.sub(r"[\\/\x00-\x1f]", " ", title)).strip()
    cleaned = cleaned.lstrip(".").strip()
    return cleaned[:100] or "Template"


class RenderSettingsSource:
    """The render key and the inbox, re-read at most every ``ttl`` seconds, so a key
    rotated in Settings reaches every worker without a restart (spec §9, §10)."""

    def __init__(
        self,
        pool: Pool,
        defaults: Settings,
        *,
        ttl: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._pool = pool
        self._defaults = defaults
        self.ttl = ttl
        self._clock = clock
        self._cached: RenderStoreSettings | None = None
        self._at = 0.0

    async def current(self) -> RenderStoreSettings:
        now = self._clock()
        if self._cached is None or now - self._at >= self.ttl:
            self._cached = await asyncio.to_thread(
                load_render_store_settings, self._pool, self._defaults
            )
            self._at = now
        return self._cached

    def invalidate(self) -> None:
        """Re-read on the next call: the API calls this after its own settings write."""
        self._cached = None

    async def target(self) -> BambuddyTarget:
        current = await self.current()
        if not current.bambuddy_url or current.library_folder_id is None:
            raise not_configured(
                "the Bambuddy store needs a Bambuddy URL and a library folder in Settings"
            )
        return BambuddyTarget(
            config=BambuddyConfig(base_url=current.bambuddy_url.rstrip("/"), api_key=current.api_key),
            inbox_id=current.library_folder_id,
        )


class BambuddyContentBackend:
    backend = "bambuddy"

    def __init__(
        self,
        target: Callable[[], Awaitable[BambuddyTarget]],
        pool: Pool,
        *,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self._target = target
        self._pool = pool
        self._http = http or httpx.AsyncClient()
        self._owns_http = http is None
        self._folders: dict[tuple[int, str, str], int] = {}

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    @asynccontextmanager
    async def _client(self) -> AsyncIterator[tuple[BambuddyClient, int]]:
        target = await self._target()
        async with BambuddyClient(target.config, http=self._http) as client:
            yield client, target.inbox_id

    # --- ContentBackend ------------------------------------------------------

    async def upload(self, kind: BlobKind, data: bytes, *, name: str, scope: BlobScope) -> str:
        media = _MEDIA_TYPES.get(Path(name).suffix.lower(), "application/octet-stream")
        async with self._client() as (client, inbox):
            for attempt in (1, 2):
                folder_id = await self._folder_for(client, inbox, scope)
                try:
                    uploaded = await client.upload_library_file(
                        name, data, folder_id=folder_id, media_type=media
                    )
                except ApiError as error:
                    # The folder was deleted in Bambuddy's UI: forget it, make it again.
                    if error.status != 404 or attempt == 2:
                        raise
                    await asyncio.to_thread(self._forget, inbox, scope.slug or "")
                    continue
                return str(uploaded.id)
        raise AssertionError("unreachable")

    async def download(self, backend_id: str) -> AsyncIterator[bytes]:
        async with self._client() as (client, _):
            try:
                async for chunk in client.download_library_file(int(backend_id)):
                    yield chunk
            except ApiError as error:
                if error.status == 404:
                    raise BlobMissingError(backend_id) from None
                raise

    async def exists(self, backend_id: str) -> bool:
        async with self._client() as (client, _):
            try:
                await client.library_file(int(backend_id))
            except ApiError as error:
                if error.status == 404:
                    return False
                raise
        return True

    async def remove(self, backend_id: str) -> None:
        async with self._client() as (client, _):
            try:
                file = await client.library_file(int(backend_id))
            except ApiError as error:
                if error.status == 404:
                    return
                raise
            work = await asyncio.to_thread(self._work_folders)
            if file.folder_id not in work:
                raise RefusedDeleteError(
                    f"library file {backend_id} is in folder {file.folder_id}, not a ScadBuddy"
                    " Work folder; it is left alone"
                )
            try:
                await client.delete_library_file(int(backend_id))
            except ApiError as error:
                if error.status != 404:
                    raise

    # --- folders -------------------------------------------------------------

    async def _folder_for(self, client: BambuddyClient, inbox: int, scope: BlobScope) -> int:
        if scope.folder == "output" and scope.project_id is not None:
            folders = await client.folders_by_project(scope.project_id)
            if not folders:
                raise not_configured(f"project {scope.project_id} has no library folder in Bambuddy")
            return folders[0].id
        slug = scope.slug or ""
        title = folder_name(scope.title or scope.slug or SHARED_TITLE)
        template = await self._ensure(client, inbox, slug, "template", title, inbox)
        if scope.folder == "output":
            return template
        return await self._ensure(client, inbox, slug, "work", WORK, template)

    async def _ensure(
        self, client: BambuddyClient, inbox: int, slug: str, role: str, name: str, parent_id: int
    ) -> int:
        cache_key = (inbox, slug, role)
        found = self._folders.get(cache_key)
        if found is not None:
            return found
        async with self._locked("store-folder", inbox, slug, role):
            found = await asyncio.to_thread(self._recorded, inbox, slug, role)
            if found is None:
                found = await self._adopt_or_create(client, name, parent_id)
                await asyncio.to_thread(self._record, inbox, slug, role, found)
        self._folders[cache_key] = found
        return found

    async def _adopt_or_create(self, client: BambuddyClient, name: str, parent_id: int) -> int:
        for root in await client.folders():
            for folder in root.walk():
                if folder.parent_id == parent_id and folder.name == name:
                    return folder.id
        return (await client.create_folder(FolderCreate(name=name, parent_id=parent_id))).id

    @asynccontextmanager
    async def _locked(self, *parts: object) -> AsyncIterator[None]:
        """A Postgres advisory lock, so two workers never both create a folder."""
        key = int.from_bytes(hashlib.sha256(repr(parts).encode()).digest()[:8], "big", signed=True)
        conn = await asyncio.to_thread(self._pool.getconn)
        try:
            await asyncio.to_thread(conn.execute, "SELECT pg_advisory_lock(%s)", (key,))
            try:
                yield
            finally:
                await asyncio.to_thread(conn.execute, "SELECT pg_advisory_unlock(%s)", (key,))
        finally:
            await asyncio.to_thread(self._pool.putconn, conn)

    def _recorded(self, inbox: int, slug: str, role: str) -> int | None:
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT folder_id FROM store_folders WHERE inbox_id = %s AND slug = %s AND role = %s",
                (inbox, slug, role),
            ).fetchone()
        return int(row["folder_id"]) if row is not None else None

    def _record(self, inbox: int, slug: str, role: str, folder_id: int) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                "INSERT INTO store_folders (inbox_id, slug, role, folder_id) VALUES (%s, %s, %s, %s)"
                " ON CONFLICT DO NOTHING",
                (inbox, slug, role, folder_id),
            )

    def _forget(self, inbox: int, slug: str) -> None:
        with self._pool.connection() as conn:
            conn.execute("DELETE FROM store_folders WHERE inbox_id = %s AND slug = %s", (inbox, slug))
        for role in ("template", "work"):
            self._folders.pop((inbox, slug, role), None)

    def _work_folders(self) -> set[int]:
        with self._pool.connection() as conn:
            rows = conn.execute("SELECT folder_id FROM store_folders WHERE role = 'work'").fetchall()
        return {int(row["folder_id"]) for row in rows}
```

Note in the test `test_a_work_folder_deleted_in_bambuddy_is_made_again` the forget drops both roles, so both folders are made again (4 creates in all) — the template folder may have been deleted with its `Work/`.

- [ ] **Step 4: Run the tests**

Run: `cd backend && SCADBUDDY_TEST_DATABASE_URL=… uv run --frozen pytest tests/store/test_bambuddy_backend.py -q`
Expected: PASS (9).

- [ ] **Step 5: The manual verification against a live Bambuddy**

`backend/scadbuddy/store/verify_bambuddy.py` — never run by CI; Task 10 runs it once and records the results:

```python
"""Measure spec 2026-09-27 §6.3's facts against a live Bambuddy. Manual; never CI.

    SCADBUDDY_VERIFY_BAMBUDDY_URL=https://bambuddy.example \\
    SCADBUDDY_VERIFY_BAMBUDDY_KEY=<a Manage-Library-only key> \\
    SCADBUDDY_VERIFY_INBOX=<the inbox folder id> \\
    uv run python -m scadbuddy.store.verify_bambuddy

It works in `ScadBuddy verify/Work` under the inbox, deletes every file it uploaded,
and prints a Markdown table for tests/bambuddy/recordings/README.md. With
SCADBUDDY_VERIFY_RECORD_DIR set it also writes the upload and file responses there.
The folders stay: ScadBuddy never deletes folders.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import sys
import time
import zipfile
from pathlib import Path

from PIL import Image

from scadbuddy.bambuddy.client import BambuddyClient, BambuddyConfig
from scadbuddy.bambuddy.models import FolderCreate
from scadbuddy.core.problems import ApiError


def _samples() -> list[tuple[str, bytes, str]]:
    png = io.BytesIO()
    Image.new("L", (4, 4), 255).save(png, format="PNG")
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("piece.json", "{}")
    svg = b'<svg xmlns="http://www.w3.org/2000/svg"><rect width="1" height="1"/></svg>'
    return [
        ("verify.svg", svg, "image/svg+xml"),
        ("verify.png", png.getvalue(), "image/png"),
        ("verify.zip", archive.getvalue(), "application/zip"),
    ]


async def _folder(client: BambuddyClient, name: str, parent: int) -> int:
    for root in await client.folders():
        for folder in root.walk():
            if folder.parent_id == parent and folder.name == name:
                return folder.id
    return (await client.create_folder(FolderCreate(name=name, parent_id=parent))).id


async def main() -> int:
    url = os.environ["SCADBUDDY_VERIFY_BAMBUDDY_URL"].rstrip("/")
    key = os.environ.get("SCADBUDDY_VERIFY_BAMBUDDY_KEY")
    inbox = int(os.environ["SCADBUDDY_VERIFY_INBOX"])
    record = os.environ.get("SCADBUDDY_VERIFY_RECORD_DIR")
    rows: list[tuple[str, str]] = []
    uploaded: list[int] = []
    config = BambuddyConfig(base_url=url, api_key=key, upload_timeout=600.0)
    async with BambuddyClient(config) as client:
        work = await _folder(client, "Work", await _folder(client, "ScadBuddy verify", inbox))
        try:
            for name, data, media in _samples():
                file = await client.upload_library_file(name, data, folder_id=work, media_type=media)
                uploaded.append(file.id)
                got = b"".join([c async for c in client.download_library_file(file.id)])
                detail = await client.library_file(file.id)
                same = "identical" if got == data else f"differ ({len(got)} vs {len(data)} bytes)"
                rows.append((f"{media} upload, download by id", f"accepted; bytes {same}"))
                is_sha = detail.file_hash == hashlib.sha256(data).hexdigest()
                rows.append((f"`file_hash` of {name}", "sha256" if is_sha else repr(detail.file_hash)))
                if record:
                    Path(record, f"store-file-{name}.json").write_text(detail.model_dump_json(indent=2))
            again = await client.upload_library_file(
                "verify-again.zip", _samples()[2][1], folder_id=work, media_type="application/zip"
            )
            uploaded.append(again.id)
            rows.append(("re-upload of identical bytes", f"new id {again.id}, duplicate_of={again.duplicate_of}"))
            for mib in (8, 32, 128, 512):
                try:
                    big = await client.upload_library_file(
                        f"verify-{mib}.zip", os.urandom(mib << 20), folder_id=work,
                        media_type="application/zip",
                    )
                    uploaded.append(big.id)
                    rows.append((f"upload {mib} MiB", "accepted"))
                except ApiError as error:
                    rows.append((f"upload {mib} MiB", f"refused: {error.status} {error.detail}"))
                    break
            started = time.monotonic()
            for i in range(40):
                small = await client.upload_library_file(
                    f"verify-t{i}.zip", os.urandom(64 << 10), folder_id=work,
                    media_type="application/zip",
                )
                uploaded.append(small.id)
            rows.append(("40 uploads of 64 KiB, sequential", f"{time.monotonic() - started:.1f} s"))
            try:
                await client.upload_library_file("x.zip", b"x", folder_id=2**31 - 1, media_type="application/zip")
                rows.append(("upload into a folder that does not exist", "accepted (!)"))
            except ApiError as error:
                rows.append(("upload into a folder that does not exist", f"{error.status}"))
        finally:
            for file_id in uploaded:
                await client.delete_library_file(file_id)
    print("| Measurement | Result |\n|---|---|")
    for what, result in rows:
        print(f"| {what} | {result} |")
    print(json.dumps({"bambuddy": url, "files_cleaned": len(uploaded)}), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
```

Run the gates (mypy covers this module): `cd backend && uv run --frozen ruff check . && uv run --frozen mypy`.

- [ ] **Step 6: Commit**

```bash
git add backend/scadbuddy/store/bambuddy.py backend/scadbuddy/store/verify_bambuddy.py backend/tests/store/test_bambuddy_backend.py
git commit -m "feat(store): Bambuddy backend — inbox/Template/Work layout, deletes only in Work (#426)"
```

---

### Task 5: The worker cache — a piece fetched once per worker

**Files:**
- Create: `backend/scadbuddy/store/archive.py`, `backend/scadbuddy/store/cache.py`
- Modify: `backend/scadbuddy/store/__init__.py`, `backend/scadbuddy/store/local.py`
- Modify: `backend/scadbuddy/workflows/activities.py`, `backend/scadbuddy/core/metrics.py`
- Test: `backend/tests/store/test_worker_cache.py` (`requires_postgres`; no network)

**Interfaces:**
- Consumes: `ContentStore.replace/read/forget/index`, `BlobScope`, `BlobMissingError`, `BlobCorruptError`, `template_title` (Task 2); phase 1's `LocalBlobStore`, `RenderActivities`, `WorkerDeps`, `PieceRequest`, `PieceResult`, `PrepareResult`, `_write_piece`, `_heartbeating`, `PIECE_NAME`.
- Produces:
  ```python
  # store/archive.py
  MARKER = ".blob-sha256"
  def pack_dir(directory: Path) -> bytes
  def unpack_dir(data: bytes, directory: Path, *, sha256: str | None = None) -> None
  def read_marker(directory: Path) -> str | None
  def write_marker(directory: Path, sha256: str) -> None
  # store/__init__.py — BlobStore protocol gains:
  async def fetch(self, key: str) -> bool                           # the blob is in dir_for(key) now
  async def publish(self, key: str, *, scope: BlobScope) -> None    # the dir, as it is, is the blob now (CAS on its marker)
  async def indexed_sha(self, key: str) -> str | None               # the sha the store holds for key now
  async def publish_fresh(self, key: str, *, scope: BlobScope, expected: str | None) -> None   # CAS on `expected`
  # store/cache.py
  class StaleBlobError(RuntimeError)
  class CachedBlobStore:                                             # a BlobStore
      def __init__(self, local: LocalBlobStore, content: ContentStore, *, max_bytes: int, min_age: float, metrics: Metrics | None = None)
      backend: str; local: LocalBlobStore; content: ContentStore
      def evict(self, *, now: float | None = None) -> list[str]
      def cached_bytes(self) -> int
  async def materialize_result(blobs: BlobStore, result: JobResult | None) -> None
  Metrics.worker_cache: Counter labels ("result",)  # hit | miss
  ```

- [ ] **Step 1: Write the failing tests**

`backend/tests/store/test_worker_cache.py`:

```python
"""Two workers, no shared volume: a piece rendered on A is consumed on B (epic #426)."""

from __future__ import annotations

import io
import os
import time
import zipfile
from pathlib import Path
from typing import cast

import pytest

from scadbuddy.core.config import Config
from scadbuddy.core.paths import DataPaths
from scadbuddy.library.assets import AssetStore
from scadbuddy.render.glb import BoundingBox
from scadbuddy.render.job_models import JobResult, PartInfo
from scadbuddy.render.projection import JobProjection
from scadbuddy.store.archive import MARKER, pack_dir, unpack_dir
from scadbuddy.store.cache import CachedBlobStore, StaleBlobError
from scadbuddy.store.content import BlobScope, ContentStore
from scadbuddy.store.index import Pool
from scadbuddy.store.local import LocalBlobStore
from scadbuddy.store.refs import BlobRefs
from scadbuddy.workflows.activities import PIECE_NAME, RenderActivities, WorkerDeps, _write_piece
from scadbuddy.workflows.models import PieceRequest, PieceResult
from tests.support.store import local_content

pytestmark = pytest.mark.requires_postgres
SCOPE = BlobScope(slug="demo", title="Demo")


def worker(root: Path, content: ContentStore, **kw: float) -> CachedBlobStore:
    return CachedBlobStore(
        LocalBlobStore(root / "blobs"), content, max_bytes=int(kw.get("max_bytes", 1 << 30)),
        min_age=kw.get("min_age", 0.0),
    )


@pytest.fixture
def content(tmp_path: Path, pool: Pool) -> ContentStore:
    return local_content(tmp_path / "remote", pool)


async def test_a_published_piece_is_fetched_once_on_another_worker(tmp_path: Path, content: ContentStore) -> None:
    a, b = worker(tmp_path / "a", content), worker(tmp_path / "b", content)
    (a.dir_for("k") / "model.3mf").write_bytes(b"3mf")
    await a.publish("k", scope=SCOPE)
    assert await b.fetch("k")
    assert (b.dir_for("k") / "model.3mf").read_bytes() == b"3mf"
    downloads = content.backend.download
    calls = 0

    def counting(backend_id: str):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        return downloads(backend_id)

    content.backend.download = counting  # type: ignore[method-assign]
    assert await b.fetch("k")
    assert calls == 0  # the second fetch is a cache hit


async def test_a_later_stage_published_elsewhere_replaces_the_cached_copy(
    tmp_path: Path, content: ContentStore
) -> None:
    a, b = worker(tmp_path / "a", content), worker(tmp_path / "b", content)
    (a.dir_for("k") / "model.3mf").write_bytes(b"main")
    await a.publish("k", scope=SCOPE)
    assert await b.fetch("k")
    (b.dir_for("k") / "plate-1.3mf").write_bytes(b"solids")
    await b.publish("k", scope=SCOPE)
    assert await a.fetch("k")
    assert (a.dir_for("k") / "plate-1.3mf").read_bytes() == b"solids"


async def test_a_stale_publisher_cannot_overwrite_a_newer_piece(
    tmp_path: Path, content: ContentStore
) -> None:
    a, b = worker(tmp_path / "a", content), worker(tmp_path / "b", content)
    (a.dir_for("k") / "model.3mf").write_bytes(b"v1")
    await a.publish("k", scope=SCOPE)
    await b.fetch("k")
    (a.dir_for("k") / "model.3mf").write_bytes(b"v2 from the retry")
    await a.publish("k", scope=SCOPE)
    (b.dir_for("k") / "model.3mf").write_bytes(b"v2 from the zombie")
    with pytest.raises(StaleBlobError):
        await b.publish("k", scope=SCOPE)
    c = worker(tmp_path / "c", content)
    assert await c.fetch("k")
    assert (c.dir_for("k") / "model.3mf").read_bytes() == b"v2 from the retry"


async def test_a_piece_deleted_or_altered_in_the_backend_is_rendered_again(
    tmp_path: Path, content: ContentStore
) -> None:
    a = worker(tmp_path / "a", content)
    for key, damage in (("gone", "delete"), ("altered", "rewrite")):
        (a.dir_for(key) / "model.3mf").write_bytes(key.encode())
        await a.publish(key, scope=SCOPE)
        stat = content.index.get(key)
        assert stat is not None
        path = tmp_path / "remote" / stat.ref.backend_id
        path.unlink() if damage == "delete" else path.write_bytes(b"someone else's")
        b = worker(tmp_path / f"b-{key}", content)
        assert await b.fetch(key) is False
        assert content.index.get(key) is None  # forgotten: the next render stores it again


def test_an_archive_entry_outside_its_directory_is_refused(tmp_path: Path) -> None:
    evil = io.BytesIO()
    with zipfile.ZipFile(evil, "w") as z:
        z.writestr("../escape.txt", "x")
    with pytest.raises(ValueError, match="outside"):
        unpack_dir(evil.getvalue(), tmp_path / "piece")
    assert not (tmp_path / "escape.txt").exists()


def test_packing_is_deterministic_and_leaves_out_dotfiles(tmp_path: Path) -> None:
    d = tmp_path / "d"
    d.mkdir()
    (d / "a.txt").write_text("a")
    (d / MARKER).write_text("x")
    (d / ".piece.json.123").write_text("staging")
    first = pack_dir(d)
    os.utime(d / "a.txt", (1, 1))
    assert pack_dir(d) == first
    assert zipfile.ZipFile(io.BytesIO(first)).namelist() == ["a.txt"]


async def test_eviction_keeps_unpublished_and_recent_pieces(tmp_path: Path, content: ContentStore) -> None:
    a = worker(tmp_path / "a", content, max_bytes=0, min_age=60.0)
    for key in ("old", "recent"):
        (a.dir_for(key) / "m").write_bytes(b"x" * 10)
        await a.publish(key, scope=SCOPE)
    (a.dir_for("rendering") / "m").write_bytes(b"x" * 10)  # never published
    past = time.time() - 3600
    os.utime(a.local.root / "old", (past, past))
    os.utime(a.local.root / "rendering", (past, past))
    assert a.evict() == ["old"]
    assert a.local.exists("recent") and a.local.exists("rendering")


async def test_render_main_on_a_worker_without_the_piece_publishes_over_the_index(
    tmp_path: Path, content: ContentStore
) -> None:
    a, b = worker(tmp_path / "a", content), worker(tmp_path / "b", content)
    a_baseline = await a.indexed_sha("k")
    (a.dir_for("k") / "model.3mf").write_bytes(b"from A")
    await a.publish_fresh("k", scope=SCOPE, expected=a_baseline)
    # B's cache has never seen k; its baseline comes from the index, not a marker.
    b_baseline = await b.indexed_sha("k")
    assert b_baseline is not None
    (b.dir_for("k") / "model.3mf").write_bytes(b"from B")
    await b.publish_fresh("k", scope=SCOPE, expected=b_baseline)
    c = worker(tmp_path / "c", content)
    assert await c.fetch("k")
    assert (c.dir_for("k") / "model.3mf").read_bytes() == b"from B"
    # A zombie render_main that read its baseline before B published is still refused.
    with pytest.raises(StaleBlobError):
        await a.publish_fresh("k", scope=SCOPE, expected=a_baseline)


async def test_local_store_fetch_and_publish_keep_phase_one_behaviour(tmp_path: Path) -> None:
    local = LocalBlobStore(tmp_path / "blobs")
    assert await local.fetch("k") is False
    local.dir_for("k")
    assert await local.fetch("k") is True
    await local.publish("k", scope=SCOPE)  # nothing to do on a shared volume


async def test_cached_piece_answers_on_a_worker_that_never_rendered_it(
    tmp_path: Path, content: ContentStore, pool: Pool
) -> None:
    a, b = worker(tmp_path / "a", content), worker(tmp_path / "b", content)
    result = JobResult(
        model_3mf="blobs/k/model.3mf",
        preview_glb="blobs/k/preview.glb",
        parts=[PartInfo(name="Color 1", colour="#FF0000", extruder=1, watertight=True)],
        bbox_mm=BoundingBox(min=(0, 0, 0), max=(1, 1, 1), size=(1, 1, 1)),
    )
    _write_piece(a.dir_for("k"), PieceResult(result=result, log_tail=["ok"]))
    await a.publish("k", scope=SCOPE)
    deps = WorkerDeps(
        config=Config(data_dir=tmp_path / "b"),
        paths=DataPaths(tmp_path / "b"),
        assets=AssetStore(tmp_path / "b" / "assets"),
        blobs=b,
        refs=BlobRefs(pool),
        projection=cast(JobProjection, object()),
    )
    req = PieceRequest(slug="demo", revision="a" * 40, params={}, piece_key="k")
    piece = await RenderActivities(deps).cached_piece(req)
    assert piece is not None and piece.result == result
    assert (b.dir_for("k") / PIECE_NAME).is_file()
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd backend && SCADBUDDY_TEST_DATABASE_URL=… uv run --frozen pytest tests/store/test_worker_cache.py -q`
Expected: FAIL — `ModuleNotFoundError: scadbuddy.store.archive`.

- [ ] **Step 3: `store/archive.py`**

```python
"""A directory as one blob: how a piece, a snapshot or a font family travels through a
remote store (spec 2026-09-27 §6.3). Deviation from §6.3's `piece-….3mf`: a phase-1
piece is a directory (model 3MF, GLB, plates, `piece.json`), so its blob is a zip of
that directory; a per-colour-objects 3MF Part arrives with phase 5's manifests."""

from __future__ import annotations

import io
import os
import shutil
import uuid
import zipfile
from pathlib import Path

#: The sha256 of the blob a cached directory was last published as or fetched from.
MARKER = ".blob-sha256"
_EPOCH = (1980, 1, 1, 0, 0, 0)


def pack_dir(directory: Path) -> bytes:
    """Every regular file under ``directory`` except dot-named ones, in name order with
    fixed timestamps, so the same content always packs to the same bytes."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(directory.rglob("*")):
            rel = path.relative_to(directory)
            if path.is_symlink() or not path.is_file() or any(p.startswith(".") for p in rel.parts):
                continue
            info = zipfile.ZipInfo(rel.as_posix(), date_time=_EPOCH)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, path.read_bytes())
    return buffer.getvalue()


def unpack_dir(data: bytes, directory: Path, *, sha256: str | None = None) -> None:
    """Replace ``directory`` with the archive's content, atomically; refuse any entry
    that would land outside it."""
    directory.parent.mkdir(parents=True, exist_ok=True)
    staging = directory.with_name(f".{directory.name}.{uuid.uuid4().hex}")
    staging.mkdir()
    root = staging.resolve()
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            for info in archive.infolist():
                target = (staging / info.filename).resolve()
                if info.filename.startswith("/") or not target.is_relative_to(root):
                    raise ValueError(f"refused an archive entry outside its directory: {info.filename!r}")
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.read(info))
        if sha256 is not None:
            (staging / MARKER).write_text(sha256, encoding="ascii")
        if directory.exists():
            shutil.rmtree(directory)
        os.replace(staging, directory)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def read_marker(directory: Path) -> str | None:
    try:
        return (directory / MARKER).read_text(encoding="ascii").strip() or None
    except OSError:
        return None


def write_marker(directory: Path, sha256: str) -> None:
    staging = directory / f"{MARKER}.{uuid.uuid4().hex}"
    staging.write_text(sha256, encoding="ascii")
    os.replace(staging, directory / MARKER)
```

- [ ] **Step 4: The protocol and `LocalBlobStore`**

In `backend/scadbuddy/store/__init__.py`, add to the `BlobStore` protocol (import `BlobScope` from `scadbuddy.store.content_models`):

```python
    async def fetch(self, key: str) -> bool:
        """Make ``dir_for(key)`` hold the stored blob; False when there is none."""
        ...

    async def publish(self, key: str, *, scope: BlobScope) -> None:
        """Store ``dir_for(key)`` as it now is, for any other process to fetch; refused
        if the store moved on since this directory was fetched."""
        ...

    async def indexed_sha(self, key: str) -> str | None:
        """The sha the store holds for ``key`` now; None when it holds nothing."""
        ...

    async def publish_fresh(self, key: str, *, scope: BlobScope, expected: str | None) -> None:
        """`publish` for a directory built from nothing (`render_main`): the swap is
        against ``expected``, read with `indexed_sha` when the activity started, not
        against a marker the directory never had."""
        ...
```

and update the module docstring's last sentence to: "Phase 3 (#426) adds `fetch`/`publish`, which `store/cache.py` implements over a remote `ContentStore`." In `backend/scadbuddy/store/local.py`, add to `LocalBlobStore`:

```python
    async def fetch(self, key: str) -> bool:
        # One volume: the directory is the blob.
        return self.exists(key)

    async def publish(self, key: str, *, scope: BlobScope) -> None:
        return None

    async def indexed_sha(self, key: str) -> str | None:
        return None

    async def publish_fresh(self, key: str, *, scope: BlobScope, expected: str | None) -> None:
        return None
```

- [ ] **Step 5: `store/cache.py`**

```python
"""A process's local cache in front of a remote `ContentStore`, in phase 1's
directory shape (spec 2026-09-27 §6.2).

Each render stage runs in `dir_for(key)` exactly as on the shared volume; `publish`
packs the directory into one blob when a stage ends, `fetch` unpacks it on whichever
worker the next stage lands on. A directory whose marker matches the index is a hit and
costs one index read. Sticky scheduling would make every fetch a hit; nothing depends
on it. Publishing is a compare-and-swap on the sha the directory was fetched at, so an
attempt Temporal has already retried elsewhere cannot overwrite its successor.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING

from scadbuddy.render.job_models import JobResult
from scadbuddy.store import BlobStore
from scadbuddy.store.archive import pack_dir, read_marker, unpack_dir, write_marker
from scadbuddy.store.content import BlobCorruptError, BlobMissingError, BlobScope, ContentStore
from scadbuddy.store.local import LocalBlobStore

if TYPE_CHECKING:
    from scadbuddy.core.metrics import Metrics

logger = logging.getLogger(__name__)


class StaleBlobError(RuntimeError):
    """The key moved on since this process fetched it. The activity fails; Temporal
    retries it, and the retry fetches the newer state."""


def _size(directory: Path) -> int:
    return sum(p.stat().st_size for p in directory.rglob("*") if p.is_file())


class CachedBlobStore:
    def __init__(
        self,
        local: LocalBlobStore,
        content: ContentStore,
        *,
        max_bytes: int,
        min_age: float,
        metrics: Metrics | None = None,
    ) -> None:
        self.local = local
        self.content = content
        #: The cache's cap (SCADBUDDY_WORKER_CACHE_MAX_BYTES).
        self.max_bytes = max_bytes
        #: Never evict a directory touched this recently: an activity may be writing it.
        #: The wiring passes `Config.activity_timeout`, the longest an attempt can live.
        self.min_age = min_age
        self.metrics = metrics
        self.backend = content.name

    # --- phase 1's BlobStore, over the local cache ---------------------------

    def dir_for(self, key: str) -> Path:
        return self.local.dir_for(key)

    def exists(self, key: str) -> bool:
        return self.local.exists(key) or self.content.index.get(key) is not None

    def remove(self, key: str) -> None:
        self.local.remove(key)

    def keys(self) -> list[str]:
        return self.local.keys()

    def touched_at(self, key: str) -> float:
        return self.local.touched_at(key)

    # --- the remote half -----------------------------------------------------

    def _cache(self, result: str) -> None:
        if self.metrics is not None:
            self.metrics.worker_cache.labels(result).inc()

    async def fetch(self, key: str) -> bool:
        stat = await asyncio.to_thread(self.content.index.get, key)
        if stat is None:
            return False
        directory = self.local.dir_for(key)
        if read_marker(directory) == stat.ref.sha256:
            self._cache("hit")
        else:
            self._cache("miss")
            try:
                data = await self.content.read(stat.ref)
            except (BlobMissingError, BlobCorruptError) as error:
                logger.warning(
                    "a stored piece is gone or altered; it will be rendered again",
                    extra={"key": key, "error": repr(error)},
                )
                await self.content.forget(key)
                return False
            await asyncio.to_thread(unpack_dir, data, directory, sha256=stat.ref.sha256)
        # Claimed: the sweep's `delete_if_stale` now skips it (see `sweep_content`).
        await asyncio.to_thread(self.content.index.touch, key)
        return True

    async def publish(self, key: str, *, scope: BlobScope) -> None:
        directory = self.local.dir_for(key)
        await self._publish(key, scope, expected=read_marker(directory))

    async def indexed_sha(self, key: str) -> str | None:
        stat = await asyncio.to_thread(self.content.index.get, key)
        return stat.ref.sha256 if stat is not None else None

    async def publish_fresh(self, key: str, *, scope: BlobScope, expected: str | None) -> None:
        await self._publish(key, scope, expected=expected)

    async def _publish(self, key: str, scope: BlobScope, *, expected: str | None) -> None:
        directory = self.local.dir_for(key)
        data = await asyncio.to_thread(pack_dir, directory)
        ref = await self.content.replace(
            key, "piece", data, name=f"piece-{key}.zip", scope=scope, expected=expected
        )
        if ref is None:
            raise StaleBlobError(f"piece {key} was published by a later attempt")
        await asyncio.to_thread(write_marker, directory, ref.sha256)

    def cached_bytes(self) -> int:
        return sum(_size(self.local.root / key) for key in self.local.keys())

    def evict(self, *, now: float | None = None) -> list[str]:
        """Least recently used first, down to `max_bytes`; only published directories
        (a marker) not touched within `min_age`."""
        cutoff = (time.time() if now is None else now) - self.min_age
        entries = []
        for key in self.local.keys():
            if key.startswith("."):
                continue  # an `unpack_dir` staging directory, gone in a moment
            directory = self.local.root / key  # not dir_for: that would touch it
            try:
                mtime = directory.stat().st_mtime
            except FileNotFoundError:
                continue
            entries.append((mtime, key, _size(directory), read_marker(directory)))
        total = sum(size for _, _, size, _ in entries)
        removed: list[str] = []
        for mtime, key, size, marker in sorted(entries):
            if total <= self.max_bytes:
                break
            if marker is None or mtime > cutoff:
                continue
            self.local.remove(key)
            total -= size
            removed.append(key)
        return removed


async def materialize_result(blobs: BlobStore, result: JobResult | None) -> None:
    """Pull a finished job's piece into this process's cache before its files are read
    (`JobResult` paths are `blobs/<key>/<file>`, relative to the data dir)."""
    if result is None:
        return
    for rel in {result.model_3mf, result.preview_glb}:
        parts = Path(rel).parts
        if len(parts) >= 3 and parts[0] == "blobs":
            await blobs.fetch(parts[1])
```

In `backend/scadbuddy/core/metrics.py`, beside `store_ops`:

```python
        self.worker_cache = Counter(
            "scadbuddy_worker_cache_total",
            "Piece fetches answered from this process's local cache (hit) or downloaded (miss).",
            ["result"],
            registry=r,
        )
```

- [ ] **Step 6: The activities fetch before and publish after each stage**

In `backend/scadbuddy/workflows/activities.py`:

```python
from scadbuddy.store.content import BlobScope, template_title


def _scope(req: PieceRequest, prepared: PrepareResult) -> BlobScope:
    """Where the piece's blob goes: its template's folder, named by `model.json`."""
    return BlobScope(slug=req.slug, title=template_title(Path(prepared.scad).parent, req.slug))
```

`cached_piece` becomes:

```python
    @activity.defn(name="cached_piece")
    async def cached_piece(self, req: PieceRequest) -> PieceResult | None:
        """The piece as a finished render left it, so it is never rendered in place again."""
        blobs = self.deps.blobs
        # Phase 1's guard (85b83de0) stays: without a revision the key stands for a
        # live source that can change under it.
        if req.revision is None or not await blobs.fetch(req.piece_key):
            return None
        return await asyncio.to_thread(_read_piece, blobs.dir_for(req.piece_key) / PIECE_NAME)
```

In `render_main`, first line after `d = self.deps` (it renders into a directory it never fetched, so its compare-and-swap baseline is what the index holds now, not a marker):

```python
        baseline = await d.blobs.indexed_sha(req.piece_key)
```

and after `output = await _heartbeating(work)` and its `except`, before `return _main_result(output)`:

```python
        await _heartbeating(
            asyncio.create_task(
                d.blobs.publish_fresh(req.piece_key, scope=_scope(req, prepared), expected=baseline)
            )
        )
```

In `render_solids`, first line after `d = self.deps`: `await d.blobs.fetch(req.piece_key)` (the main 3MF may have been rendered on another worker); after its `_heartbeating(work)` block, `await _heartbeating(asyncio.create_task(d.blobs.publish(req.piece_key, scope=_scope(req, prepared))))`: `publish`, not `publish_fresh`, because this stage fetched, so its marker is the right baseline and a zombie is refused (Review Focus 3). In `finish_piece`, first line after `d = self.deps`: `await d.blobs.fetch(req.piece_key)`; after `await asyncio.to_thread(_write_piece, work, piece)`, the same `publish` call. A `StaleBlobError` propagates as a retryable activity failure. With the `local` backend `fetch` is `exists` and `publish` does nothing, so phase 1's behaviour is unchanged.

- [ ] **Step 7: Run the tests**

Run: `cd backend && SCADBUDDY_TEST_DATABASE_URL=… uv run --frozen pytest tests/store tests/test_blob_store.py tests/test_workflows.py tests/test_activities.py -q`
Expected: PASS (the 10 new; phase 1's blob-store, workflow and activity tests unchanged, `test_a_piece_without_a_revision_is_never_answered_from_its_blob` included).

- [ ] **Step 8: Commit**

```bash
git add backend/scadbuddy/store backend/scadbuddy/workflows/activities.py backend/scadbuddy/core/metrics.py backend/tests/store/test_worker_cache.py
git commit -m "feat(store): worker cache over the remote store; stages fetch and publish their piece (#426)"
```

---

### Task 6: Template snapshots and fonts in the store

**Files:**
- Create: `backend/scadbuddy/store/snapshots.py`, `backend/scadbuddy/store/fonts.py`
- Modify: `backend/scadbuddy/render/jobs.py` (`_export_atomically` → `export_revision`; `resolve_source`'s history assertion moves into its export branch), `backend/scadbuddy/render/submit.py`, `backend/scadbuddy/workflows/activities.py`, `backend/scadbuddy/api/fonts.py`
- Test: `backend/tests/store/test_snapshots_and_fonts.py` (`requires_postgres`; the pin test also `requires_git`)

**Interfaces:**
- Consumes: `ContentStore`, `BlobScope`, `template_title` (Task 2); `pack_dir`, `unpack_dir` (Task 5); `ModelHistory.available`, `.last_commit(path)`, `model_path` (`core/paths.py`); `DataPaths.model_revision_dir`; `FontService.root`, `.family_dir`, `.prepare`, `.refresh_cache`.
- Produces:
  ```python
  def snapshot_key(slug: str, revision: str) -> str
  class SnapshotUnavailableError(RuntimeError)
  class SnapshotStore:
      def __init__(self, content: ContentStore, paths: DataPaths, history: ModelHistory | None)
      async def pin(self, slug: str, revision: str | None) -> str | None
      async def ensure(self, slug: str, revision: str) -> str
      async def materialize(self, slug: str, revision: str) -> bool
  def font_key(directory_name: str) -> str
  class FontMirror:
      def __init__(self, content: ContentStore, fonts: FontService)
      async def publish(self, family: str) -> None
      async def backfill(self) -> int
      async def sync(self) -> list[str]
  def export_revision(history: ModelHistory, slug: str, version: str, directory: Path) -> None   # render/jobs.py
  WorkerDeps.snapshots: SnapshotStore | None = None; WorkerDeps.fonts_mirror: FontMirror | None = None
  RenderService.snapshots: SnapshotStore | None = None   # a settable attribute, like phase 1's `client`; set in the lifespan (Task 8)
  ```

- [ ] **Step 1: Write the failing tests**

`backend/tests/store/test_snapshots_and_fonts.py`:

```python
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import cast

import pytest

from scadbuddy.core.config import Config
from scadbuddy.core.paths import DataPaths, model_path
from scadbuddy.library.assets import AssetStore
from scadbuddy.library.fonts import FontService
from scadbuddy.library.history import ModelHistory
from scadbuddy.render.projection import JobProjection
from scadbuddy.store.content import ContentStore
from scadbuddy.store.fonts import FontMirror, font_key
from scadbuddy.store.index import Pool
from scadbuddy.store.local import LocalBlobStore
from scadbuddy.store.refs import BlobRefs
from scadbuddy.store.snapshots import SnapshotStore, snapshot_key
from scadbuddy.workflows.activities import RenderActivities, WorkerDeps
from scadbuddy.workflows.models import PieceRequest
from tests.support.store import local_content

pytestmark = pytest.mark.requires_postgres


@pytest.fixture
def content(tmp_path: Path, pool: Pool) -> ContentStore:
    return local_content(tmp_path / "remote", pool)


async def test_a_worker_without_the_volume_gets_the_template_at_its_revision(
    tmp_path: Path, content: ContentStore
) -> None:
    api_paths = DataPaths(tmp_path / "api")
    export = api_paths.model_revision_dir("demo", "a" * 40)
    export.mkdir(parents=True)
    (export / "model.scad").write_text("cube(1);")
    (export / "model.json").write_text('{"name": "Demo box"}')
    api = SnapshotStore(content, api_paths, history=None)
    assert await api.ensure("demo", "a" * 40) == snapshot_key("demo", "a" * 40)
    stat = content.index.get(snapshot_key("demo", "a" * 40))
    assert stat is not None and stat.slug == "demo"
    worker_paths = DataPaths(tmp_path / "worker")
    worker = SnapshotStore(content, worker_paths, history=None)
    assert await worker.materialize("demo", "a" * 40)
    got = worker_paths.model_revision_dir("demo", "a" * 40)
    assert (got / "model.scad").read_text() == "cube(1);"
    assert await worker.materialize("demo", "b" * 40) is False


@pytest.mark.requires_git
async def test_an_unpinned_request_renders_the_last_commit(tmp_path: Path, content: ContentStore) -> None:
    paths = DataPaths(tmp_path / "data")
    paths.ensure()
    history = ModelHistory(paths.models)
    history.ensure_repo()
    source = paths.model_source("demo")
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("cube(2);")
    history.commit("add demo", model_path("demo"))
    head = subprocess.run(
        ["git", "-C", str(paths.models), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    snapshots = SnapshotStore(content, paths, history)
    assert await snapshots.pin("demo", None) == head
    assert content.index.get(snapshot_key("demo", head)) is not None


async def test_prepare_on_a_worker_without_history_uses_the_materialized_snapshot(
    tmp_path: Path, content: ContentStore, pool: Pool
) -> None:
    rev = "c" * 40
    api_paths = DataPaths(tmp_path / "api")
    export = api_paths.model_revision_dir("demo", rev)
    export.mkdir(parents=True)
    (export / "model.scad").write_text("cube(3);")
    (export / "model.json").write_text('{"name": "Demo"}')
    await SnapshotStore(content, api_paths, history=None).ensure("demo", rev)
    worker_paths = DataPaths(tmp_path / "worker")
    deps = WorkerDeps(
        config=Config(data_dir=tmp_path / "worker"),
        paths=worker_paths,
        assets=AssetStore(tmp_path / "worker" / "assets"),
        blobs=LocalBlobStore(tmp_path / "worker" / "blobs"),
        refs=BlobRefs(pool),
        projection=cast(JobProjection, object()),
        history=None,
        snapshots=SnapshotStore(content, worker_paths, history=None),
    )
    req = PieceRequest(slug="demo", revision=rev, params={}, piece_key="k")
    prepared = await RenderActivities(deps).prepare(req)
    assert Path(prepared.scad) == worker_paths.model_revision_dir("demo", rev) / "model.scad"
    assert prepared.version == rev


async def test_downloaded_fonts_reach_a_worker_that_never_installed_them(
    tmp_path: Path, content: ContentStore
) -> None:
    api_fonts = FontService(tmp_path / "api")
    family = api_fonts.family_dir("Lobster Two")
    family.mkdir(parents=True)
    (family / "LobsterTwo-Regular.ttf").write_bytes(b"ttf")
    await FontMirror(content, api_fonts).publish("Lobster Two")
    worker_fonts = FontService(tmp_path / "worker")
    mirror = FontMirror(content, worker_fonts)
    assert await mirror.sync() == [family.name]
    assert (worker_fonts.root / family.name / "LobsterTwo-Regular.ttf").read_bytes() == b"ttf"
    assert await mirror.sync() == []
    assert content.index.get(font_key(family.name)) is not None
```

(`ModelHistory.ensure_repo()` and `commit(message, *paths)` are `library/history.py:323,373`; `FontService(data_dir, *, …)` takes the rest by keyword with defaults.)

- [ ] **Step 2: Run to verify they fail**

Run: `cd backend && SCADBUDDY_TEST_DATABASE_URL=… uv run --frozen pytest tests/store/test_snapshots_and_fonts.py -q`
Expected: FAIL — `ModuleNotFoundError: scadbuddy.store.snapshots`.

- [ ] **Step 3: `export_revision`**

In `backend/scadbuddy/render/jobs.py`, rename `_export_atomically` to `export_revision` (same signature `(history, slug, version, directory)`, same body) and update its one caller, in `resolve_source`. In that same function, move `assert history is not None  # a requested revision implies a repository` from before `directory = paths.model_revision_dir(slug, requested)` into the `else:` branch, right before the `export_revision` call: a worker on the bambuddy backend has no history, and a snapshot it materialized is a populated export that needs none (`test_prepare_on_a_worker_without_history_uses_the_materialized_snapshot`).

- [ ] **Step 4: `store/snapshots.py`**

```python
"""A template's source at a revision, as one blob (spec 2026-09-27 §6.1).

The API makes it (it has git); a worker unpacks it into the same revision-export
directory `prepare_source` reads, so a populated export is found and git is never
needed on the worker. A snapshot is a cache of git: nothing references it, it is swept
after the grace once no render touches it, and `ensure` makes it again on demand.
"""

from __future__ import annotations

import asyncio
import re

from scadbuddy.core.paths import DataPaths, model_path
from scadbuddy.library.history import ModelHistory
from scadbuddy.render.jobs import export_revision
from scadbuddy.store.archive import pack_dir, unpack_dir
from scadbuddy.store.content import BlobScope, ContentStore, template_title


def snapshot_key(slug: str, revision: str) -> str:
    return f"src-{re.sub(r'[^A-Za-z0-9._-]', '_', slug)}-{revision}"


class SnapshotUnavailableError(RuntimeError):
    """No snapshot is stored and this process has no git history to make one."""


class SnapshotStore:
    def __init__(self, content: ContentStore, paths: DataPaths, history: ModelHistory | None) -> None:
        self.content = content
        self.paths = paths
        self.history = history

    async def pin(self, slug: str, revision: str | None) -> str | None:
        """The revision a render uses, with its snapshot stored. An unpinned request
        renders the template's last commit; None when there is no history at all."""
        if revision is None:
            if self.history is None or not self.history.available:
                return None
            revision = await asyncio.to_thread(self.history.last_commit, model_path(slug))
            if revision is None:
                return None
        await self.ensure(slug, revision)
        return revision

    async def ensure(self, slug: str, revision: str) -> str:
        key = snapshot_key(slug, revision)
        if await asyncio.to_thread(self.content.index.get, key) is not None:
            await asyncio.to_thread(self.content.index.touch, key)
            return key
        directory = self.paths.model_revision_dir(slug, revision)
        if not directory.is_dir():
            if self.history is None:
                raise SnapshotUnavailableError(f"no snapshot of {slug}@{revision} and no history")
            await asyncio.to_thread(export_revision, self.history, slug, revision, directory)
        data = await asyncio.to_thread(pack_dir, directory)
        await self.content.put(
            "snapshot",
            data,
            name=f"src-{revision[:12]}.zip",
            scope=BlobScope(slug=slug, title=template_title(directory, slug)),
            key=key,
        )
        return key

    async def materialize(self, slug: str, revision: str) -> bool:
        directory = self.paths.model_revision_dir(slug, revision)
        if directory.is_dir():
            return True
        key = snapshot_key(slug, revision)
        stat = await asyncio.to_thread(self.content.index.get, key)
        if stat is None:
            return False
        data = await self.content.read(stat.ref)
        await asyncio.to_thread(unpack_dir, data, directory)
        await asyncio.to_thread(self.content.index.touch, key)
        return True
```

- [ ] **Step 5: `store/fonts.py`**

```python
"""Downloaded Google Fonts through the store (spec 2026-09-27 §6.1). A worker without
the API's volume would otherwise fall back to DejaVu silently, and that changes the
geometry (CLAUDE.md, "Fonts"). One blob per family directory under `Shared/Work/`."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

from scadbuddy.library.fonts import FontService
from scadbuddy.store.archive import pack_dir, unpack_dir
from scadbuddy.store.bambuddy import SHARED_TITLE
from scadbuddy.store.content import BlobScope, ContentStore

SHARED = BlobScope(slug=None, title=SHARED_TITLE)


def font_key(directory_name: str) -> str:
    return "font-" + re.sub(r"[^A-Za-z0-9._-]", "_", directory_name)[:100]


class FontMirror:
    def __init__(self, content: ContentStore, fonts: FontService) -> None:
        self.content = content
        self.fonts = fonts

    async def _publish_dir(self, directory: Path) -> None:
        data = await asyncio.to_thread(pack_dir, directory)
        await self.content.put(
            "font",
            data,
            name=f"{font_key(directory.name)}.zip",
            scope=SHARED,
            key=font_key(directory.name),
            meta={"dir": directory.name},
        )

    async def publish(self, family: str) -> None:
        directory = self.fonts.family_dir(family)
        if directory.is_dir():
            await self._publish_dir(directory)

    async def backfill(self) -> int:
        """Mirror every family installed before the store was (the API's boot)."""
        root = self.fonts.root
        if not root.is_dir():
            return 0
        done = 0
        for directory in sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")):
            if await asyncio.to_thread(self.content.index.get, font_key(directory.name)) is None:
                await self._publish_dir(directory)
                done += 1
        return done

    async def sync(self) -> list[str]:
        """Install every mirrored family this process lacks; one index query when none."""
        added: list[str] = []
        for stat in await asyncio.to_thread(self.content.index.stats, ("font",)):
            name = str(stat.meta.get("dir", ""))
            if not name or "/" in name or name.startswith("."):
                continue
            target = self.fonts.root / name
            if target.is_dir():
                continue
            data = await self.content.read(stat.ref)
            await asyncio.to_thread(unpack_dir, data, target)
            added.append(name)
        if added:
            await asyncio.to_thread(self.fonts.prepare)
            await asyncio.to_thread(self.fonts.refresh_cache)
        return added
```

- [ ] **Step 6: Wire snapshots and fonts in**

`backend/scadbuddy/workflows/activities.py`: add to `WorkerDeps` `snapshots: SnapshotStore | None = None` and `fonts_mirror: FontMirror | None = None`; at the top of `prepare`, after `d = self.deps`:

```python
        if d.snapshots is not None and req.revision is not None:
            await d.snapshots.materialize(req.slug, req.revision)
        if d.fonts_mirror is not None:
            await d.fonts_mirror.sync()
```

`backend/scadbuddy/render/submit.py`: in `RenderService.__init__`, beside `self.client = client`, add `self.snapshots: SnapshotStore | None = None` (a settable attribute, as `client` is: the lifespan sets it once the store is built, Task 8; no constructor keyword), and at the top of `submit`:

```python
        if self.snapshots is not None:
            # The bambuddy store (spec §6.1): workers read the source from the store,
            # so every job names a revision whose snapshot exists before it starts.
            model_version = await self.snapshots.pin(slug, model_version)
```

`backend/scadbuddy/api/fonts.py`: add `state: StateDep` to `install_font`'s signature, and after the install succeeds and before returning:

```python
    store = getattr(state, "store", None)  # Task 8's StoreBundle; the guard goes then
    if store is not None and store.fonts is not None:
        await store.fonts.publish(body.family)
```

- [ ] **Step 7: Run the tests**

Run: `cd backend && SCADBUDDY_TEST_DATABASE_URL=… uv run --frozen pytest tests/store tests/test_jobs.py tests/test_submit.py tests/test_activities.py -q`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add backend/scadbuddy/store/snapshots.py backend/scadbuddy/store/fonts.py backend/scadbuddy/render/jobs.py backend/scadbuddy/render/submit.py backend/scadbuddy/workflows/activities.py backend/scadbuddy/api/fonts.py backend/tests/store/test_snapshots_and_fonts.py
git commit -m "feat(store): template snapshots and downloaded fonts reach workers through the store (#426)"
```

---

### Task 7: `AssetStore` on the store

**Files:**
- Create: `backend/scadbuddy/store/assets.py`
- Modify: `backend/scadbuddy/library/assets.py`, `backend/scadbuddy/workflows/activities.py`, `backend/scadbuddy/api/assets.py`, `backend/scadbuddy/main.py`
- Test: `backend/tests/store/test_remote_assets.py` (`requires_postgres`), `backend/tests/test_assets.py` (append)

**Interfaces:**
- Consumes: `ContentStore`, `BlobScope`, `template_title` (Task 2); `AssetStore`, `AssetMeta`, `AssetNotFoundError`, `AssetRejectedError`, `_ids_in`.
- Produces:
  ```python
  # library/assets.py
  def asset_ids_in(params: Mapping[str, object]) -> set[str]
  AssetStore.adopt(self, meta: AssetMeta, data: bytes) -> None
  AssetStore.ids(self) -> list[str]
  # store/assets.py
  def asset_key(asset_id: str) -> str
  class RemoteAssets:
      def __init__(self, content: ContentStore)
      async def mirror(self, store: AssetStore, meta: AssetMeta, *, slug: str | None, title: str | None) -> None
      async def ensure(self, store: AssetStore, ids: Iterable[str]) -> list[str]
      async def drop(self, ids: Iterable[str]) -> None
      async def backfill(self, store: AssetStore) -> int
  WorkerDeps.remote_assets: RemoteAssets | None = None
  main.drop_swept_assets(state: AppState, removed: list[str]) -> Awaitable[None]
  ```

- [ ] **Step 1: Write the failing tests**

`backend/tests/store/test_remote_assets.py`:

```python
from __future__ import annotations

from pathlib import Path

import pytest

from scadbuddy.library.assets import AssetMeta, AssetRejectedError, AssetStore, asset_ids_in
from scadbuddy.store.assets import RemoteAssets, asset_key
from scadbuddy.store.index import Pool
from tests.support.store import local_content

pytestmark = pytest.mark.requires_postgres
SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><rect width="4" height="4"/></svg>'


async def test_an_upload_on_the_api_is_readable_on_a_worker(tmp_path: Path, pool: Pool) -> None:
    remote = RemoteAssets(local_content(tmp_path / "remote", pool))
    api = AssetStore(tmp_path / "api" / "assets")
    meta = api.put(SVG, "logo.svg")
    await remote.mirror(api, meta, slug="demo", title="Demo")
    worker = AssetStore(tmp_path / "worker" / "assets")
    params = {"logo": meta.id, "width": 3}
    assert await remote.ensure(worker, asset_ids_in(params)) == [meta.id]
    assert worker.get(meta.id) == meta
    assert worker.blob_path(meta).read_bytes() == api.blob_path(meta).read_bytes()
    assert await remote.ensure(worker, [meta.id]) == []  # already local: no download
    assert await remote.ensure(worker, ["f" * 64]) == []  # unknown id: the render reports it
    await remote.drop([meta.id])
    assert remote.content.index.get(asset_key(meta.id)) is None


async def test_backfill_mirrors_uploads_made_before_the_store(tmp_path: Path, pool: Pool) -> None:
    remote = RemoteAssets(local_content(tmp_path / "remote", pool))
    api = AssetStore(tmp_path / "assets")
    meta = api.put(SVG, "old.svg")
    assert await remote.backfill(api) == 1
    assert await remote.backfill(api) == 0
    assert remote.content.index.get(asset_key(meta.id)) is not None


def test_adopt_refuses_bytes_that_are_not_the_id(tmp_path: Path) -> None:
    store = AssetStore(tmp_path / "assets")
    meta = AssetMeta(id="0" * 64, name="x.svg", kind="svg", size=3)
    with pytest.raises(AssetRejectedError):
        store.adopt(meta, b"abc")
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd backend && SCADBUDDY_TEST_DATABASE_URL=… uv run --frozen pytest tests/store/test_remote_assets.py -q`
Expected: FAIL — `ImportError: cannot import name 'asset_ids_in'`.

- [ ] **Step 3: `AssetStore.adopt`, `ids`, `asset_ids_in`**

In `backend/scadbuddy/library/assets.py`, in `AssetStore` after `put`:

```python
    def adopt(self, meta: AssetMeta, data: bytes) -> None:
        """Keep an asset fetched from the blob store exactly as it was stored elsewhere.

        No caps: the upload that created it was checked against them. The bytes must be
        the id, so a damaged or substituted download never becomes a render's input.
        """
        if hashlib.sha256(data).hexdigest() != meta.id or len(data) != meta.size:
            raise AssetRejectedError(f"the fetched file does not match asset {meta.id}")
        with self._locked():
            count, total = self._tracked()
            had_blob = self.blob_path(meta).is_file()
            if not had_blob:
                self._write_ledger(count, total, dirty=True)
            _write_atomically(self.blob_path(meta), data)
            _write_atomically(
                self._meta_path(meta.id),
                (json.dumps(meta.model_dump(), indent=2) + "\n").encode(),
            )
            if not had_blob:
                self._write_ledger(count + 1, total + len(data))

    def ids(self) -> list[str]:
        return sorted(self._blobs())
```

and at module level after `_ids_in`:

```python
def asset_ids_in(params: Mapping[str, object]) -> set[str]:
    """Every asset id a render's parameters could name (as loose as the sweep's scan)."""
    return _ids_in(json.dumps(params, sort_keys=True).encode())
```

- [ ] **Step 4: `store/assets.py`**

```python
"""`// file` uploads through the store (spec 2026-09-27 §6.1). The API's `AssetStore`
stays the place uploads are validated, capped and swept (#204, #296); this mirrors each
upload into the store and brings it into a worker's own `AssetStore` before a render."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable

from scadbuddy.library.assets import AssetMeta, AssetNotFoundError, AssetStore
from scadbuddy.store.content import BlobScope, ContentStore


def asset_key(asset_id: str) -> str:
    return f"asset-{asset_id}"


class RemoteAssets:
    def __init__(self, content: ContentStore) -> None:
        self.content = content

    async def mirror(
        self, store: AssetStore, meta: AssetMeta, *, slug: str | None, title: str | None
    ) -> None:
        data = await asyncio.to_thread(store.blob_path(meta).read_bytes)
        await self.content.put(
            "asset",
            data,
            name=f"asset-{meta.id}.{meta.kind}",
            scope=BlobScope(slug=slug, title=title),
            key=asset_key(meta.id),
            meta=meta.model_dump(),
        )

    async def ensure(self, store: AssetStore, ids: Iterable[str]) -> list[str]:
        fetched: list[str] = []
        for asset_id in sorted(set(ids)):
            try:
                await asyncio.to_thread(store.get, asset_id)
                continue
            except AssetNotFoundError:
                pass
            stat = await asyncio.to_thread(self.content.index.get, asset_key(asset_id))
            if stat is None:
                continue
            data = await self.content.read(stat.ref)
            await asyncio.to_thread(store.adopt, AssetMeta.model_validate(stat.meta), data)
            fetched.append(asset_id)
        return fetched

    async def drop(self, ids: Iterable[str]) -> None:
        for asset_id in ids:
            await self.content.delete(asset_key(asset_id))

    async def backfill(self, store: AssetStore) -> int:
        done = 0
        for asset_id in await asyncio.to_thread(store.ids):
            if await asyncio.to_thread(self.content.index.get, asset_key(asset_id)) is None:
                meta = await asyncio.to_thread(store.get, asset_id)
                await self.mirror(store, meta, slug=None, title=None)
                done += 1
        return done
```

- [ ] **Step 5: Wire it in**

`backend/scadbuddy/workflows/activities.py`: add `remote_assets: RemoteAssets | None = None` to `WorkerDeps`; at the top of `render_main` (after Task 5's `baseline = …` line) and of `render_solids` (after its `fetch`):

```python
        if d.remote_assets is not None:
            await d.remote_assets.ensure(d.assets, asset_ids_in(req.params))
```

`backend/scadbuddy/api/assets.py`, `upload_asset`: bind the `put` result, then mirror before returning (keep the existing `try/except` around the `put` exactly as it is):

```python
        meta = await asyncio.to_thread(assets.put, data, file.filename)
    ...
    store = getattr(state, "store", None)  # Task 8's StoreBundle; the guard goes then
    if store is not None and store.remote_assets is not None:
        await store.remote_assets.mirror(
            assets, meta, slug=slug, title=template_title(state.paths.model_source(slug).parent, slug)
        )
    return meta
```

(`from scadbuddy.store.content import template_title`.) A failed mirror (Bambuddy unreachable) fails the upload with Bambuddy's problem document: a file a worker could not read must not look uploaded.

`backend/scadbuddy/main.py`, beside `sweep_assets`:

```python
async def drop_swept_assets(state: AppState, removed: list[str]) -> None:
    """The store's copies of what `sweep_assets` just removed from the volume."""
    store = getattr(state, "store", None)  # Task 8's StoreBundle; the guard goes then
    if store is not None and store.remote_assets is not None and removed:
        await store.remote_assets.drop(removed)
```

and at `sweep_assets`'s call site (`grep -n "sweep_assets" backend/scadbuddy/main.py`; it runs in a thread from the periodic loop), await `drop_swept_assets(state, removed)` with its result on the loop.

- [ ] **Step 6: Run the tests**

Run: `cd backend && SCADBUDDY_TEST_DATABASE_URL=… uv run --frozen pytest tests/store tests/test_assets.py tests/test_asset_quota_and_sweep.py tests/api/test_assets.py -q`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add backend/scadbuddy/store/assets.py backend/scadbuddy/library/assets.py backend/scadbuddy/workflows/activities.py backend/scadbuddy/api/assets.py backend/scadbuddy/main.py backend/tests/store/test_remote_assets.py
git commit -m "feat(store): // file uploads are mirrored to the store and fetched by workers (#426)"
```

---

### Task 8: Wiring, `/healthz`, metrics, `GET /store/usage`, sweep and eviction

**Files:**
- Create: `backend/scadbuddy/store/factory.py`, `backend/scadbuddy/api/store.py`
- Modify: `backend/scadbuddy/api/deps.py`, `api/health.py`, `api/metrics.py`, `api/jobs.py`, `api/outputs.py`, `api/settings.py`, `api/fonts.py`, `api/assets.py` (Tasks 6–7 guards removed), `main.py` (lifespan, sweeps; no router include), `worker.py`, `core/metrics.py`
- Modify: `agent/src/tools/customizer.ts`
- Test: `backend/tests/store/test_factory.py`, `backend/tests/api/test_store.py`

**Interfaces:**
- Consumes: everything above.
- Produces:
  ```python
  @dataclass class StoreBundle: backend: StoreBackend; blobs: BlobStore; content: ContentStore | None; snapshots: SnapshotStore | None; remote_assets: RemoteAssets | None; fonts: FontMirror | None; source: RenderSettingsSource; remote: BambuddyContentBackend | None = None; async def aclose(self) -> None
  def build_store(*, backend: StoreBackend, config: Config, paths: DataPaths, pool: Pool, source: RenderSettingsSource, history: ModelHistory | None, fonts: FontService, metrics: Metrics | None) -> StoreBundle
  def store_usage(bundle: StoreBundle, config: Config) -> StoreUsage
  class StoreHealth(BaseModel): backend: StoreBackend; configured_backend: StoreBackend; render_key_fallback: bool; multi_worker: bool
  async def store_health(bundle: StoreBundle) -> StoreHealth
  AppState.store: StoreBundle = field(init=False)   # set in the lifespan, always; AppState.blobs: BlobStore | None = bundle.blobs
  Health.store: StoreHealth
  GET /api/v1/store/usage -> StoreUsage
  Metrics: store_blobs, store_bytes{kind}, store_max_blobs, store_max_bytes, store_render_key_fallback, worker_cache_bytes (Gauges)
  ```

- [ ] **Step 1: Write the failing tests**

`backend/tests/store/test_factory.py`:

```python
from __future__ import annotations

from pathlib import Path

import pytest

from scadbuddy.core.config import Config
from scadbuddy.core.paths import DataPaths
from scadbuddy.core.settings import Settings
from scadbuddy.library.fonts import FontService
from scadbuddy.store.bambuddy import RenderSettingsSource
from scadbuddy.store.cache import CachedBlobStore
from scadbuddy.store.factory import build_store, store_health, store_usage
from scadbuddy.store.index import Pool
from scadbuddy.store.local import LocalBlobStore
from tests.conftest import UNUSED_DATABASE_URL

pytestmark = pytest.mark.requires_postgres


def _build(tmp_path: Path, pool: Pool, backend: str):  # type: ignore[no-untyped-def]
    config = Config(data_dir=tmp_path)
    source = RenderSettingsSource(pool, Settings(data_dir=tmp_path, database_url=UNUSED_DATABASE_URL))
    return config, build_store(
        backend=backend,  # type: ignore[arg-type]
        config=config,
        paths=DataPaths(tmp_path),
        pool=pool,
        source=source,
        history=None,
        fonts=FontService(tmp_path),
        metrics=None,
    )


async def test_local_is_phase_one_exactly(tmp_path: Path, pool: Pool) -> None:
    config, bundle = _build(tmp_path, pool, "local")
    assert isinstance(bundle.blobs, LocalBlobStore) and bundle.content is None
    assert bundle.snapshots is None and bundle.remote_assets is None and bundle.fonts is None
    (bundle.blobs.dir_for("k") / "m").write_bytes(b"12345")
    usage = store_usage(bundle, config)
    assert (usage.backend, usage.count, usage.bytes) == ("local", 1, 5)
    health = await store_health(bundle)
    assert health.backend == "local" and health.multi_worker is False
    await bundle.aclose()


async def test_bambuddy_puts_a_cache_in_front_of_the_remote(tmp_path: Path, pool: Pool) -> None:
    _, bundle = _build(tmp_path, pool, "bambuddy")
    assert isinstance(bundle.blobs, CachedBlobStore)
    assert bundle.blobs.local.root == DataPaths(tmp_path).blobs
    assert bundle.content is not None and bundle.content.name == "bambuddy"
    assert (await store_health(bundle)).multi_worker is True
    await bundle.aclose()
```

`backend/tests/api/test_store.py`:

```python
from __future__ import annotations

from fastapi.testclient import TestClient


def test_store_usage_reports_the_local_backend(client: TestClient) -> None:
    body = client.get("/api/v1/store/usage").json()
    assert body["backend"] == "local"
    assert {"count", "bytes", "max_count", "max_total_bytes", "by_kind"} <= set(body)


def test_healthz_says_where_blobs_live_and_whether_workers_hold_the_full_key(client: TestClient) -> None:
    store = client.get("/healthz").json()["store"]
    assert store == {
        "backend": "local",
        "configured_backend": "local",
        "render_key_fallback": False,
        "multi_worker": False,
    }
    client.put("/api/v1/settings", json={"bambuddy_api_key": "full"})
    # the API invalidates its settings source on its own write, so this is immediate
    assert client.get("/healthz").json()["store"]["render_key_fallback"] is True
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd backend && SCADBUDDY_TEST_DATABASE_URL=… uv run --frozen pytest tests/store/test_factory.py tests/api/test_store.py -q`
Expected: FAIL — `ModuleNotFoundError: scadbuddy.store.factory`.

- [ ] **Step 3: `store/factory.py`**

```python
"""Which store a process runs on, built once at start (spec 2026-09-27 §6.2)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic import BaseModel

from scadbuddy.core.config import Config, StoreBackend
from scadbuddy.core.paths import DataPaths
from scadbuddy.library.fonts import FontService
from scadbuddy.library.history import ModelHistory
from scadbuddy.store import BlobStore
from scadbuddy.store.assets import RemoteAssets
from scadbuddy.store.bambuddy import BambuddyContentBackend, RenderSettingsSource
from scadbuddy.store.cache import CachedBlobStore
from scadbuddy.store.content import ContentStore, StoreUsage
from scadbuddy.store.fonts import FontMirror
from scadbuddy.store.index import BlobIndex, Pool
from scadbuddy.store.local import LocalBlobStore
from scadbuddy.store.snapshots import SnapshotStore

if TYPE_CHECKING:
    from scadbuddy.core.metrics import Metrics


@dataclass
class StoreBundle:
    backend: StoreBackend
    blobs: BlobStore
    content: ContentStore | None
    snapshots: SnapshotStore | None
    remote_assets: RemoteAssets | None
    fonts: FontMirror | None
    source: RenderSettingsSource
    remote: BambuddyContentBackend | None = None

    async def aclose(self) -> None:
        if self.remote is not None:
            await self.remote.aclose()


def build_store(
    *,
    backend: StoreBackend,
    config: Config,
    paths: DataPaths,
    pool: Pool,
    source: RenderSettingsSource,
    history: ModelHistory | None,
    fonts: FontService,
    metrics: Metrics | None,
) -> StoreBundle:
    local = LocalBlobStore(paths.blobs)
    if backend == "local":
        return StoreBundle("local", local, None, None, None, None, source)
    remote = BambuddyContentBackend(source.target, pool)
    content = ContentStore(
        remote,
        BlobIndex(pool),
        max_total_bytes=config.store_max_total_bytes,
        max_count=config.store_max_count,
        metrics=metrics,
    )
    blobs = CachedBlobStore(
        local, content, max_bytes=config.worker_cache_max_bytes,
        min_age=config.activity_timeout, metrics=metrics,
    )
    return StoreBundle(
        "bambuddy",
        blobs,
        content,
        SnapshotStore(content, paths, history),
        RemoteAssets(content),
        FontMirror(content, fonts),
        source,
        remote,
    )


def store_usage(bundle: StoreBundle, config: Config) -> StoreUsage:
    if bundle.content is not None:
        return bundle.content.usage()
    blobs = bundle.blobs
    assert isinstance(blobs, LocalBlobStore)
    total = sum(
        p.stat().st_size for key in blobs.keys() for p in (blobs.root / key).rglob("*") if p.is_file()
    )
    return StoreUsage(
        backend="local",
        count=len(blobs.keys()),
        bytes=total,
        max_count=config.store_max_count,
        max_total_bytes=config.store_max_total_bytes,
        by_kind={"piece": total},
    )


class StoreHealth(BaseModel):
    #: The backend this process runs on (read at start).
    backend: StoreBackend
    #: The backend stored in Settings; differs from `backend` until a restart.
    configured_backend: StoreBackend
    #: Render workers hold the full Bambuddy key (spec §9): template code can print.
    render_key_fallback: bool
    #: The render worker Deployment may run more than one replica (spec §3.1).
    multi_worker: bool


async def store_health(bundle: StoreBundle) -> StoreHealth:
    current = await bundle.source.current()
    return StoreHealth(
        backend=bundle.backend,
        configured_backend=current.store_backend,
        render_key_fallback=current.key_is_fallback and bool(current.api_key),
        multi_worker=bundle.backend != "local",
    )
```

- [ ] **Step 4: Metrics**

In `backend/scadbuddy/core/metrics.py`, beside `worker_cache` (same `registry=r` pattern):

```python
        self.store_blobs = Gauge("scadbuddy_store_blobs", "Distinct objects in the blob store.", registry=r)
        self.store_bytes = Gauge(
            "scadbuddy_store_bytes", "Bytes in the blob store, by kind.", ["kind"], registry=r
        )
        self.store_max_blobs = Gauge(
            "scadbuddy_store_max_blobs", "SCADBUDDY_STORE_MAX_COUNT; 0 is no limit.", registry=r
        )
        self.store_max_bytes = Gauge(
            "scadbuddy_store_max_bytes", "SCADBUDDY_STORE_MAX_TOTAL_BYTES; 0 is no limit.", registry=r
        )
        self.store_render_key_fallback = Gauge(
            "scadbuddy_store_render_key_fallback",
            "1 while render workers hold the full Bambuddy key (spec 2026-09-27 §9).",
            registry=r,
        )
        self.worker_cache_bytes = Gauge(
            "scadbuddy_worker_cache_bytes", "Bytes in this process's local piece cache.", registry=r
        )
```

In `backend/scadbuddy/api/metrics.py`, beside `refresh_asset_metrics`, and call it wherever that one is called:

```python
async def refresh_store_metrics(state: AppState) -> None:
    usage = await asyncio.to_thread(store_usage, state.store, state.config)
    state.metrics.store_blobs.set(usage.count)
    for kind, size in usage.by_kind.items():
        state.metrics.store_bytes.labels(kind).set(size)
    state.metrics.store_max_blobs.set(usage.max_count)
    state.metrics.store_max_bytes.set(usage.max_total_bytes)
    health = await store_health(state.store)
    state.metrics.store_render_key_fallback.set(1 if health.render_key_fallback else 0)
    if isinstance(state.store.blobs, CachedBlobStore):
        state.metrics.worker_cache_bytes.set(await asyncio.to_thread(state.store.blobs.cached_bytes))
```

If `refresh_asset_metrics`'s caller is synchronous, call this one from the same periodic loop's async side instead (`grep -n "refresh_asset_metrics" backend/scadbuddy`).

- [ ] **Step 5: API wiring**

The store is built in the lifespan, not in `build_state`: its index (`store_blobs`) and the render settings live in the settings pool, which exists only once `settings_store.open()` has run (`main.py` `lifespan`, its first line). It is built on both render paths: `local` on the legacy `RenderQueue` (the API test app's path, `tests/api/conftest.py` overrides `get_queue`), as configured on the Temporal path.

`backend/scadbuddy/api/deps.py`:
- `AppState`: change `blobs: LocalBlobStore | None = field(default=None)` to `blobs: BlobStore | None = field(default=None)` (`from scadbuddy.store import BlobStore`), and add `store: StoreBundle = field(init=False)` after it (`from scadbuddy.store.factory import StoreBundle`). Set in the lifespan before any request is served; nothing reads it earlier.
- `build_state`: delete `blobs: LocalBlobStore | None = None`, `blobs = LocalBlobStore(paths.blobs)` inside `if projection is not None:`, and `blobs=blobs,` in the `AppState(...)` call. `refs = BlobRefs(projection.pool)` stays.

`backend/scadbuddy/main.py` `lifespan`, right after `await asyncio.to_thread(state.settings_store.open)`:

```python
    # The blob store (#426), on both render paths: its index and the render settings
    # live in the settings pool, open only from here on.
    pool = state.settings_store.pool
    source = RenderSettingsSource(pool, state.settings)
    backend: StoreBackend = "local"
    if state.projection is not None:
        backend = (
            await asyncio.to_thread(load_render_store_settings, pool, state.settings)
        ).store_backend
    state.store = build_store(
        backend=backend, config=state.config, paths=state.paths, pool=pool, source=source,
        history=state.history, fonts=state.fonts, metrics=state.metrics,
    )
    state.blobs = state.store.blobs
    if isinstance(state.queue, RenderService):
        state.queue.snapshots = state.store.snapshots
    if state.store.remote_assets is not None:
        logger.info("mirrored uploads", extra={"count": await state.store.remote_assets.backfill(state.assets)})
    if state.store.fonts is not None:
        logger.info("mirrored fonts", extra={"count": await state.store.fonts.backfill()})
```

(imports: `StoreBackend` from `scadbuddy.core.config`, `load_render_store_settings` from `scadbuddy.library.settings_store`, `RenderSettingsSource` from `scadbuddy.store.bambuddy`, `build_store` from `scadbuddy.store.factory`; `RenderService` is already imported). In the lifespan's `finally`, before `await asyncio.to_thread(state.settings_store.close)`: `await state.store.aclose()`. `_close_quietly` (the boot-failure path that runs before the lifespan's `try`) also awaits `state.store.aclose()` when `state.store` is set, so a failure in `_prepare_catalogue`, `queue.start` or `events.start` still closes the bambuddy backend's httpx client.

Tasks 6 and 7's guards go: in `api/fonts.py`, `api/assets.py` and `main.py`'s `drop_swept_assets`, replace `store = getattr(state, "store", None)` and the `store is not None and` with `state.store` directly.

`backend/scadbuddy/api/settings.py`'s `put_settings`: after a successful `store.save(patch)`, call `state.store.source.invalidate()` so this process sees its own write at once (workers see it within the 30 s TTL); add `state: StateDep` to its signature if it lacks it.

`backend/scadbuddy/api/health.py`: add `store: StoreHealth` to `Health`; make `healthz` `async def` and pass `store=await store_health(state.store)`. Update `tests/api/test_health.py` if it compares the whole body.

`backend/scadbuddy/api/store.py`:

```python
"""The blob store's usage for the Settings page (spec 2026-09-27 §10)."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter

from scadbuddy.api.deps import StateDep
from scadbuddy.store.content import StoreUsage
from scadbuddy.store.factory import store_usage

router = APIRouter(tags=["store"])


@router.get(
    "/store/usage",
    response_model=StoreUsage,
    summary="Blob store usage",
    description="What the store holds against SCADBUDDY_STORE_MAX_TOTAL_BYTES / _MAX_COUNT.",
)
async def get_store_usage(state: StateDep) -> StoreUsage:
    return await asyncio.to_thread(store_usage, state.store, state.config)
```

No `main.py` change for the route: `_api_router()` mounts every `scadbuddy.api` module's `router` (sorted by name), so `api/store.py` is picked up; including it by hand would register it twice.

Job files read through the cache: in `backend/scadbuddy/api/jobs.py`, before each `paths.root / job.result.preview_glb` (lines ~260 and ~348), `await materialize_result(state.store.blobs, job.result)` (make the enclosing route `async def` if it is `def`, wrapping its blocking calls in `asyncio.to_thread`). In `backend/scadbuddy/api/outputs.py`, `create_output` (a `def` today) calls `outputs.create(job, …)`, and `OutputStore.create` (`library/outputs.py:195-196`) copies `job.result.model_3mf` and `preview_glb`: make `create_output` `async def`, `await materialize_result(state.store.blobs, job.result)` before it, and run `outputs.create(...)` through `asyncio.to_thread`.

Sweeps: in `main.py`'s `_sweep_blobs_logged`, which calls `sweep_blobs(state.blobs, state.refs, …)` and returns early when either is None, keep that path only when `state.store.content is None`; otherwise (`state.refs` is set: the bambuddy backend exists only on the Temporal path):

```python
        removed = await sweep_content(state.store.content, state.refs, grace=state.config.asset_sweep_grace)
        if isinstance(state.store.blobs, CachedBlobStore):
            await asyncio.to_thread(state.store.blobs.evict)
```

(the store sweep reuses `SCADBUDDY_ASSET_SWEEP_GRACE` and `_INTERVAL`: §6.1 generalises the asset rules).

- [ ] **Step 6: Worker wiring**

In `backend/scadbuddy/worker.py`'s `build_worker_deps` (phase 1), after the projection pool is open:

```python
    source = RenderSettingsSource(projection.pool, settings)
    backend = load_render_store_settings(projection.pool, settings).store_backend
    store = build_store(
        backend=backend, config=config, paths=paths, pool=projection.pool, source=source,
        history=None, fonts=FontService(settings.data_dir), metrics=metrics,
    )
```

and fill `WorkerDeps(blobs=store.blobs, snapshots=store.snapshots, remote_assets=store.remote_assets, fonts_mirror=store.fonts, …)`. `history=None`: a worker on the bambuddy backend has no git (the API makes snapshots); on `local` it keeps phase 1's `history` exactly as before — pass phase 1's value when `backend == "local"`. `worker_deps_from_state` (the in-process worker, started later in the same lifespan) takes `blobs=state.store.blobs, snapshots=state.store.snapshots, remote_assets=state.store.remote_assets, fonts_mirror=state.store.fonts` in place of `blobs=state.blobs`. Add `"store": (await store_health(store)).model_dump()` to the worker's `/healthz` body, and run `store.blobs.evict` every `config.asset_sweep_interval` in the worker's background loop when it is a `CachedBlobStore`. Close with `await store.aclose()` on shutdown.

- [ ] **Step 7: The agent tool**

In `agent/src/tools/customizer.ts`, copy the `defineTool({...})` whose `routes` is `['GET /api/v1/assets/usage']`, rename it `get_store_usage`, describe it "What the blob store holds (pieces, snapshots, uploads, fonts) against its limits, and which backend it is", and set `routes: ['GET /api/v1/store/usage']` and the handler's path to `'/api/v1/store/usage'` with the label `'get store usage'`.

- [ ] **Step 8: Run the tests and generate**

Run: `cd backend && SCADBUDDY_TEST_DATABASE_URL=… uv run --frozen pytest -q`
Expected: PASS (whole suite). Then, in order: `cd frontend && pnpm gen:api && pnpm typecheck && pnpm test`; `cd agent && pnpm gen:api && pnpm lint && pnpm typecheck && pnpm test` (the coverage test passes with the new tool). Nothing generated is committed.

- [ ] **Step 9: Commit**

```bash
git add backend/scadbuddy backend/tests/store/test_factory.py backend/tests/api/test_store.py backend/tests/api/test_health.py agent/src/tools/customizer.ts
git commit -m "feat(store): choose the backend at start; /healthz, metrics and GET /store/usage (#426)"
```

---

### Task 9: Settings page — the render key, its warning, the backend, store usage

**Files:**
- Modify: `frontend/src/pages/SettingsPage.tsx`, `frontend/src/pages/SettingsPage.test.tsx`
- Modify: `frontend/src/api/client.ts`, `frontend/src/api/types.ts`, `frontend/src/mocks/handlers.ts`, `frontend/src/mocks/fixtures.ts`

**Interfaces:**
- Consumes: `SettingsView.has_render_api_key`, `.render_key_fallback`, `.store_backend` (Task 1); `GET /store/usage` → `StoreUsage` (Task 8).
- Produces: `api.getStoreUsage(): Promise<StoreUsage>`; `type StoreUsage`.

- [ ] **Step 1: Write the failing tests**

Append inside `describe('SettingsPage', …)` in `frontend/src/pages/SettingsPage.test.tsx` (it already imports `renderPage`, which returns `{ user }`):

```tsx
  const stored = {
    bambuddy_url: 'https://bambuddy.internal.nullreference.io',
    has_api_key: true,
    library_folder_id: 7,
    store_backend: 'local',
  }

  it('warns while render workers would hold the full key', async () => {
    server.use(
      http.get('/api/v1/settings', () =>
        HttpResponse.json({ ...stored, has_render_api_key: false, render_key_fallback: true }),
      ),
    )
    renderPage(<SettingsPage />)
    await seeded()
    expect(screen.getByTestId('render-key-fallback')).toHaveTextContent(
      'Render workers hold the full Bambuddy key; template code can print.',
    )
    expect(screen.getByLabelText('Render key')).toHaveAttribute('placeholder', 'Paste the key')
  })

  it('drops the warning once a render key is stored, and never shows the key', async () => {
    server.use(
      http.get('/api/v1/settings', () =>
        HttpResponse.json({ ...stored, has_render_api_key: true, render_key_fallback: false }),
      ),
    )
    renderPage(<SettingsPage />)
    await seeded()
    expect(screen.queryByTestId('render-key-fallback')).toBeNull()
    const key = screen.getByLabelText('Render key')
    expect(key).toHaveValue('')
    expect(key).toHaveAttribute('type', 'password')
    expect(key).toHaveAttribute('placeholder', expect.stringContaining('A key is stored'))
  })

  it('sends a typed render key and leaves the stored API key alone', async () => {
    const bodies: Record<string, unknown>[] = []
    server.use(
      http.get('/api/v1/settings', () =>
        HttpResponse.json({ ...stored, has_render_api_key: false, render_key_fallback: true }),
      ),
      http.put('/api/v1/settings', async ({ request }) => {
        bodies.push((await request.json()) as Record<string, unknown>)
        return HttpResponse.json({ ...stored, has_render_api_key: true, render_key_fallback: false })
      }),
    )
    const { user } = renderPage(<SettingsPage />)
    await seeded()
    await user.type(screen.getByLabelText('Render key'), 'narrow')
    await user.click(screen.getByRole('button', { name: /^save/i }))
    await waitFor(() => expect(bodies).toHaveLength(1))
    expect(bodies[0]).toMatchObject({ bambuddy_render_api_key: 'narrow' })
    expect(bodies[0]).not.toHaveProperty('bambuddy_api_key')
  })

  it('shows store usage in place of the uploads line', async () => {
    server.use(
      http.get('/api/v1/store/usage', () =>
        HttpResponse.json({
          backend: 'bambuddy',
          count: 12,
          bytes: 2048,
          max_count: 0,
          max_total_bytes: 0,
          by_kind: { piece: 1024, asset: 1024 },
        }),
      ),
    )
    renderPage(<SettingsPage />)
    const usage = await screen.findByTestId('store-usage')
    expect(usage).toHaveTextContent('Bambuddy library')
    expect(usage).toHaveTextContent('12')
    expect(screen.queryByTestId('asset-usage')).toBeNull()
  })
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd frontend && pnpm test -- SettingsPage`
Expected: FAIL — `Unable to find a label with the text of: Render key`.

- [ ] **Step 3: Client and mocks**

`frontend/src/api/types.ts`, beside `AssetUsage` and in the same form it is declared: `export type StoreUsage = Schemas['StoreUsage']` (the file's form, as `AssetUsage = Schemas['AssetUsage']`). `frontend/src/api/client.ts`, beside `getAssetUsage`: `getStoreUsage: () => request<StoreUsage>('/store/usage'),`. `frontend/src/mocks/fixtures.ts`: add `has_render_api_key: false, render_key_fallback: true, store_backend: 'local',` beside `has_api_key: true`. `frontend/src/mocks/handlers.ts`: beside the `/assets/usage` handler,

```ts
  http.get(`${base}/store/usage`, () =>
    HttpResponse.json({
      backend: 'local',
      count: 3,
      bytes: 4096,
      max_count: 200000,
      max_total_bytes: 53687091200,
      by_kind: { piece: 4096 },
    }),
  ),
```

and in the settings PUT handler (near line 2441, where `has_api_key` is merged) do the same for the render key: `has_render_api_key: typeof body.bambuddy_render_api_key === 'string' ? body.bambuddy_render_api_key.length > 0 : state.settings.has_render_api_key`, then `render_key_fallback: state.settings.has_api_key && !state.settings.has_render_api_key` after the merge.

- [ ] **Step 4: The page**

In `frontend/src/pages/SettingsPage.tsx`:

- state: `const [renderKey, setRenderKey] = useState('')`; clear it wherever `setApiKey('')` is called.
- `dirty` (the `const dirty =` expression): add `renderKey.length > 0 ||` beside `apiKey.length > 0 ||`, and `storeBackend !== settings.store_backend ||` inside the `settings !== undefined && (…)` group, so a "changed elsewhere" refresh never overwrites either edit.
- request body, after the `apiKey` line: `if (renderKey.length > 0) body.bambuddy_render_api_key = renderKey`, and `body.store_backend = storeBackend` with `const [storeBackend, setStoreBackend] = useState<'local' | 'bambuddy'>('local')` seeded from `settings.store_backend` where the other fields are seeded.
- after the API key `<div>`:

```tsx
            <div>
              <label htmlFor="bambuddy-render-key" className="block text-[13px]">
                Render key
              </label>
              <input
                id="bambuddy-render-key"
                type="password"
                value={renderKey}
                autoComplete="off"
                onChange={(event) => setRenderKey(event.target.value)}
                placeholder={
                  settings?.has_render_api_key
                    ? 'A key is stored. Paste a new one to replace it.'
                    : 'Paste the key'
                }
                className="sb-field sb-num mt-1.5"
              />
              <p className="mt-1.5 text-[12px] text-muted">
                A second key with Manage Library only. Render workers run template code and
                hold this key alone.
              </p>
              {settings?.render_key_fallback && (
                <p
                  role="alert"
                  data-testid="render-key-fallback"
                  className="mt-1.5 rounded-[6px] border border-warn px-2 py-1.5 text-[12px]"
                >
                  Render workers hold the full Bambuddy key; template code can print.
                </p>
              )}
            </div>

            <div>
              <label htmlFor="store-backend" className="block text-[13px]">
                Blob store
              </label>
              <select
                id="store-backend"
                value={storeBackend}
                onChange={(event) => setStoreBackend(event.target.value as 'local' | 'bambuddy')}
                className="sb-field mt-1.5"
              >
                <option value="local">This server&rsquo;s volume (one render worker)</option>
                <option value="bambuddy" disabled={!settings?.bambuddy_url || folderId === ''}>
                  Bambuddy library (any number of render workers)
                </option>
              </select>
              <p className="mt-1.5 text-[12px] text-muted">
                Takes effect when ScadBuddy and its render workers restart.
              </p>
            </div>
```

(`folderId` is the page's existing library-folder state; use its name as it is in the file. If `border-warn` is not a token in the Tailwind config, use the class the page's existing error banner uses.)

- replace `api.getAssetUsage()` with `api.getStoreUsage()`, the heading "Uploaded files" with "Store", `data-testid="asset-usage"` with `"store-usage"`, and add a first row `<dt className="text-muted">Where</dt><dd>{usage.backend === 'bambuddy' ? 'Bambuddy library' : 'This server’s volume'}</dd>`; the explanatory paragraph becomes: "Rendered pieces, template snapshots, uploaded SVGs and PNGs, and downloaded fonts. What no job, output or preset uses is removed once unused for the sweep's grace period (a week by default). Past either limit, new files are refused."

- [ ] **Step 5: Run the tests**

Run: `cd frontend && pnpm gen:api && pnpm lint && pnpm typecheck && pnpm test && pnpm build`
Expected: PASS. (`gen:api` first, per CLAUDE.md: `StoreUsage` and the new `SettingsView` fields come from the backend's spec.)

- [ ] **Step 6: Commit**

```bash
git add frontend/src/pages/SettingsPage.tsx frontend/src/pages/SettingsPage.test.tsx frontend/src/api/client.ts frontend/src/api/types.ts frontend/src/mocks
git commit -m "feat(frontend): render key with its fallback warning, store backend, store usage (#426)"
```

---

### Task 10: Live verification, docs and the scaling handoff

**Files:**
- Modify: `backend/tests/bambuddy/recordings/README.md`, `README.md`, `CLAUDE.md`
- Output: an issue in `eh-homelab/clusters`

This task has no code test; its deliverable is measured facts and the deployment change request.

- [ ] **Step 1: Measure against the live Bambuddy**

With a Manage-Library-only key created in Bambuddy's Settings → API keys, and the inbox folder id from ScadBuddy's Settings:

```bash
cd backend
SCADBUDDY_VERIFY_BAMBUDDY_URL=https://bambuddy.internal.nullreference.io \
SCADBUDDY_VERIFY_BAMBUDDY_KEY=… SCADBUDDY_VERIFY_INBOX=… \
SCADBUDDY_VERIFY_RECORD_DIR=tests/bambuddy/recordings \
uv run python -m scadbuddy.store.verify_bambuddy
```

Expected: a Markdown table on stdout; exit 0; every uploaded file deleted (stderr `files_cleaned`). Paste the table into `tests/bambuddy/recordings/README.md` under a new heading "Blob store (#426), measured <date> against <version>", with the recorded `store-file-*.json` files listed in its table. Then, by hand, and record each in the same section:
  - Upload 500 files to `ScadBuddy verify/Work` (run the script's 40-upload loop ×12) and open Bambuddy's library UI on that folder and on the inbox: note load time and whether the tree stays usable ("whether a `Work/` folder with hundreds of files degrades Bambuddy's library UI", §6.3). Delete them afterwards from the UI.
  - "The maximum plates per 3MF Bambu Studio opens" (§6.3): generate `dollhouse-kit` pieces onto 6, 12, 24 and 48 plates with the multi-plate writer (`render/bambu3mf.py`) and open each in Bambu Studio; record the largest that opens.
  - If `file_hash` is a sha256, or if a re-upload of identical bytes answers with `duplicate_of` and no new file, note it: `ContentStore._store` could then skip ScadBuddy's own dedupe query. If an upload into a missing folder is not a 404, change the `error.status != 404` check in `BambuddyContentBackend.upload` to the measured status and its test's response to match.
  - If any 403 appears with the Manage-Library-only key, the scope in `bambuddy/client.py` for that call is wrong: fix it there and in `bambuddy/errors.py`'s docstring.

- [ ] **Step 2: Docs**

`README.md`, "Deploying": a subsection "Blob store and render workers":
  - `local` (default): one render worker, sharing the API's `/data` volume — phase 1's topology.
  - `bambuddy`: set Bambuddy's URL and a library folder (the inbox) in Settings, create a Manage-Library-only key in Bambuddy and paste it as **Render key**, choose **Bambuddy library**, restart the API and the workers. Workers then need no shared volume: give each an `emptyDir` at `/data` (its cache, `SCADBUDDY_WORKER_CACHE_MAX_BYTES`), and scale the Deployment freely. `/healthz` → `store.multi_worker: true` says it is safe; `store.render_key_fallback: true` means template code holds the full key.
  - Env: `SCADBUDDY_STORE_BACKEND` (seeds the setting), `SCADBUDDY_BAMBUDDY_RENDER_API_KEY` (seeds the render key), `SCADBUDDY_STORE_MAX_TOTAL_BYTES`, `SCADBUDDY_STORE_MAX_COUNT`, `SCADBUDDY_WORKER_CACHE_MAX_BYTES`.
  - Metrics: `scadbuddy_store_*`, `scadbuddy_worker_cache_*`.

`CLAUDE.md`, "Layout", after the `library/` bullet:

```markdown
- `backend/scadbuddy/store/` — the blob store (spec 2026-09-27 §6): phase 1's directory
  `BlobStore` (`LocalBlobStore`, `blob_refs`), §6.2's byte `ContentStore` over a backend
  (`local.py`, `bambuddy.py`: `<inbox>/<Template>/Work/`, deletes only in `Work/`) with its
  index in `store_blobs`, the per-process cache (`cache.py`: `fetch`/`publish`, CAS on the
  sha), `snapshots.py`, `fonts.py`, `assets.py`, and `factory.py` (`store_backend`, a stored
  setting, read at start). `python -m scadbuddy.store.verify_bambuddy` re-measures §6.3.
```

- [ ] **Step 3: The clusters handoff**

```bash
gh issue create --repo eh-homelab/clusters \
  --title "scadbuddy: render workers without a shared volume, >1 replica (ScadBuddy #426)" \
  --body-file - <<'EOF'
ScadBuddy phase 3 (eh-homelab/ScadBuddy#426; spec docs/superpowers/specs/2026-09-27-template-pipelines-design.md §3.1, §6) moves every file a render reads into Bambuddy's library. Once ScadBuddy's Settings say **Blob store: Bambuddy library** and `/healthz` on the API reports `"store": {"backend": "bambuddy", "multi_worker": true}`:

1. `scadbuddy-render` Deployment: drop the `/data` PVC mount; mount an `emptyDir` (sizeLimit ~12Gi) at `/data`; set `SCADBUDDY_WORKER_CACHE_MAX_BYTES=10737418240`; `replicas: 2` (or an HPA on `scadbuddy_render_jobs_pending`).
2. No new secret: workers read the render key from Postgres (Settings → Render key).
3. Alert: `scadbuddy_store_render_key_fallback == 1` for 1h → warn ("render workers hold the full Bambuddy key").

Order: change the setting and restart first; scale only after `/healthz` says `multi_worker: true` on a worker pod.
EOF
```

Comment on #426 with the issue URL.

- [ ] **Step 4: Commit**

```bash
git add backend/tests/bambuddy/recordings README.md CLAUDE.md
git commit -m "docs: blob store measured against Bambuddy; deploying multiple render workers (#426)"
```

---

## Self-review notes

- **Spec coverage.** §6.1 table: `// file` assets → Task 7; template source → Task 6 (`SnapshotStore`); pinned libraries → phase 1's `CheckoutFetcher` already clones from the pin on a worker's own volume, unchanged; Google Fonts → Task 6 (`FontMirror`, "optionally mirrored" taken, because a silent DejaVu fallback changes geometry); per-piece Parts → Task 5; final outputs → the `output` scope and folder rule exist in Task 4, the writer is phase 4's `ctx.output`. §6.2 interface → Task 2 (`ContentStore`), refs/sweep/caps → Task 2 (`sweep_content`, `StoreFullError`), worker LRU cache → Task 5, `SCADBUDDY_STORE_BACKEND` → Task 1 (seeds the stored setting) and Task 8. §6.3 layout and delete rule → Task 4; verifications → Task 4 Step 5 (script) and Task 10 Step 1 (run, plus the two human checks). §3.1 scaling → Task 8 (`multi_worker`) and Task 10 (clusters). §8.4 "store refs of every Part" → the `store_blobs` key/sha of each piece exists for phase 4's record; no output record changes here. §9 render key → Tasks 1, 4 (`RenderSettingsSource`), 8 (health, metric), 9 (warning). §10 Settings → Tasks 1, 8, 9. §11 item 3 → all. §12 #316/#317 → Task 4.
- **Placeholders.** None intended. Task 8 Steps 5–6 and Task 9 Step 4 name existing build sites and state by their names on the `wt-service` stack at 69306836.
- **Type consistency.** `BlobScope`/`BlobRef`/`BlobStat`/`StoreUsage` live in `store/content_models.py` and are re-exported by `store/content.py`; every later task imports them from `content`. `ContentStore.replace(..., expected=)` (Task 2) is what `CachedBlobStore.publish` (Task 5) calls. `RenderSettingsSource.target` (Task 4) is the `target` callable `BambuddyContentBackend` takes and `build_store` passes (Task 8). `WorkerDeps.snapshots`/`fonts_mirror`/`remote_assets` (Tasks 6, 7) are filled from `StoreBundle.snapshots`/`fonts`/`remote_assets` (Task 8). `materialize_result` (Task 5) is called in Task 8.
- **Review Focus** — all six pinned: (6) Task 5 `test_render_main_on_a_worker_without_the_piece_publishes_over_the_index`; (1) Task 5 `test_a_piece_deleted_or_altered_in_the_backend_is_rendered_again`; (2) Task 4 `test_concurrent_first_uploads_create_one_folder_pair`; (3) Task 5 `test_a_stale_publisher_cannot_overwrite_a_newer_piece`; (4) Task 4 `test_delete_outside_a_work_folder_is_refused_without_a_request`; (5) Task 4 `test_a_rotated_render_key_is_used_without_a_restart`. Also covered, not in the five: eviction never takes an in-flight or unpublished piece (Task 5), a store at its cap still accepts a re-put (Task 2), a zip entry escaping its directory (Task 5).
- **Deviations from the spec, stated where they apply.** `ContentStore` is §6.2's `BlobStore` (the name is phase 1's); `put` takes bytes; a piece's blob is a zip of its directory, not a `.3mf`; `SCADBUDDY_WORKER_CACHE_DIR` is the worker's data dir; the store sweep reuses `SCADBUDDY_ASSET_SWEEP_GRACE`; "same encryption" is plaintext, as for `bambuddy_api_key` (Global Constraints; follow-up issue: encrypt stored keys).
- **Deferred.** §10's read-only Temporal address and namespace on the Settings page are not in this plan (nor phase 1's); they go to a follow-up.

## Revision 1 (review)

Against `.superpowers/sdd/2026-09-28-phase1-render-on-temporal/plan-phase3-review.md`, names checked on `wt-service` at 69306836.

- B1 fixed: `cached_piece` keeps phase 1's `req.revision is None` guard; the new test uses `revision="a" * 40`; Task 5 Step 7 runs `tests/test_activities.py`.
- B2 fixed: `render_main` reads `indexed_sha` at its start and publishes with `publish_fresh(..., expected=baseline)`; later stages keep the marker CAS. New test `test_render_main_on_a_worker_without_the_piece_publishes_over_the_index` (worker A publishes, worker B with an empty cache publishes over it, a stale baseline is refused), pinned as Review Focus 6.
- B3 fixed: `resolve_source`'s history assertion moves into its export branch (Task 6 Step 3); new activity test `test_prepare_on_a_worker_without_history_uses_the_materialized_snapshot`.
- B4 fixed: `StoreBundle` is built in the lifespan right after `settings_store.open()`, on both render paths (`local` on the legacy queue); `AppState.store`/`AppState.blobs` set there; `RenderService.snapshots` is a settable attribute set there; routes use `state.store.blobs`; `build_state`'s `LocalBlobStore` removed.
- I1 fixed: Task 1 adds the render-key field and the caps; the "Removes" line and the grep are gone; the Base note says what phase 1 did and did not ship.
- I2 fixed with the `getattr(state, "store", None)` guard in Task 7 (as Task 6 has); Task 8 removes all three guards.
- I3 fixed: `api/store.py` is picked up by `_api_router()`; nothing added to `main.py` for it.
- I4 fixed: `ensure_repo()`, `commit("add demo", model_path("demo"))`; hedges dropped.
- I5 fixed: Task 7 runs after Task 6; the resolved insertion order is stated; the "parallel" claim is gone.
- I6 fixed: Global Constraints state plaintext storage as for `bambuddy_api_key`, follow-up issue: encrypt stored keys.
- M1 fixed: Task 4 expects 9; Task 5's "10 new" is now correct with the B2 test.
- M2 fixed: the caller is `resolve_source`.
- M3 fixed: `body.family`, with `state: StateDep` added to `install_font`.
- M4 fixed: `create_output` / `OutputStore.create` named.
- M5 fixed: `Schemas['StoreUsage']`.
- M6 fixed: `renderKey` and `storeBackend` join `dirty`.
- M7 fixed: `const { user } = renderPage(...)`; no new import.
- M8 fixed: Global Constraints say run `ruff format .` before the gates.
- M9 fixed: `RefusedDeleteError` moves to `content_models.py` (re-exported by `bambuddy.py` and `content.py`); `sweep_content` logs it per key and continues.
- M10 fixed as a deferral line under the deviations.
- N1/N2 (re-review) fixed: `cast` import in the stdlib group; `_close_quietly` closes the store on a boot failure.
