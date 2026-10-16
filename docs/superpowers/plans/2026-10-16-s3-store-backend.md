# S3 Store Backend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A third blob store backend, `s3`, so renders never move bytes through Bambuddy, plus the migration that moves recorded blobs from one backend to another.

**Architecture:** `S3ContentBackend` implements the existing four-method `ContentBackend` protocol over `aiobotocore`; everything above it (`ContentStore`, `CachedBlobStore`, snapshots, assets, fonts, the download cache) is unchanged. Settings gain the S3 fields, the `render_settings` view and `RenderStoreSettings` carry them to workers, `build_store` wires the bundle, and a `StoreMigration` workflow on the `library` queue copies blobs between backends in heartbeating batches.

**Tech Stack:** Python 3.12, aiobotocore (+ `types-aiobotocore-s3`), moto's `ThreadedMotoServer` for tests, psycopg, Temporal (temporalio), React/TypeScript for the Settings page.

**Spec:** `docs/superpowers/specs/2026-10-16-own-store-slicer-rust-workers-design.md` §4 (tier 1), §8.

## Global Constraints

- Object key is `<kind>/<sha256>`, under the required `store_s3_prefix` (spec §4.1); `backend_id` is that full key.
- Multipart above 64 MiB (spec §4.1); part size is the builder's, within 5 MiB to 5 GiB.
- Path-style addressing on by default (`store_s3_path_style=True`).
- All S3 settings are stored settings, `applies: restart`, each seeded by `SCADBUDDY_<FIELD>` (spec §4.2); `store_s3_secret_access_key` is a `SECRET_FIELDS` member, sealed under the KEK like `bambuddy_render_api_key`.
- `build_store` raises `StoreNotReadyError` for `s3` without endpoint, bucket or both credentials, naming `RECOVER_LOCAL_SQL` (spec §4.2).
- The render worker role reads only through `render_settings` (`CLAUDE.md`; `tests/test_render_worker_role.py` must still render).
- Schema changes are new files in `backend/scadbuddy/migrations/` named `$(date -u +%Y%m%dT%H%MZ)_<slug>.sql`; never edit a merged one.
- New durable state is Postgres or the store, never a pod file.
- No commits of `backend/openapi.json` or `schema.d.ts`.
- `local` behaviour is unchanged (spec §4.5).
- Every measured or external fact in a docstring or doc cites its source (#259).

## Review Focus

1. A put that dies after the S3 upload and before the `store_blobs` row leaves an orphan object; with `sweep_unrecorded` for S3, an object older than the cutoff with no row is removed and a younger one kept (Task 4).
2. A blob over 64 MiB round-trips byte-identical through multipart and its sha256 check (Task 2).
3. A `store_blobs` row still on the old backend after the flip is a cache miss, not an exception: `_release` refuses a cross-backend ref and `get` of a foreign-backend ref raises `BlobMissingError`, which `RenderPiece` treats as lost state (Task 5 test `test_a_row_left_on_the_old_backend_is_missing_not_an_error`).
4. The S3 endpoint down at start: the API fails readiness with a clear error rather than hanging on the first call; `exists` surfaces a connection error as the SDK's exception, not `False` (Task 2 test `test_a_connection_error_is_not_a_missing_object`).
5. A Settings save that switches to `s3` without the S3 fields is refused with 422 and the same wording as the Bambuddy case, in the API and in the msw mock (Task 1, Task 7).

---

### Task 1: S3 settings, readiness and the render settings view

**Files:**
- Modify: `backend/scadbuddy/core/config.py:91` (`StoreBackend`), `:160-168` (config fields; none added here)
- Modify: `backend/scadbuddy/core/settings.py:143-156` (fields), `:563-565` (`SECRET_FIELDS`), `:623-629` (`APPLIES`), the descriptions dict around `:545`
- Modify: `backend/scadbuddy/library/settings_store.py:96-102` (`RENDER_FIELDS`), `:151` (`STORE_READINESS`), `:162` (`StoreNotReadyError` docstring), `:674-682` and `:741-749` (readiness checks), `:1049-1057` (`RenderStoreSettings`), `:1060-1088` (`load_render_store_settings`)
- Create: `backend/scadbuddy/migrations/<ts>_render_settings_s3.sql`
- Test: `backend/tests/test_settings_store.py`, `backend/tests/test_render_worker_role.py`

**Interfaces:**
- Produces: `StoreBackend = Literal["local", "bambuddy", "s3"]`; `class S3Settings(BaseModel): endpoint: str; region: str = "us-east-1"; bucket: str; prefix: str; access_key_id: str; secret_access_key: str = Field(repr=False); path_style: bool = True`; `RenderStoreSettings.s3: S3Settings | None`; `StoredSettings.s3_settings() -> S3Settings | None` (None unless endpoint, bucket and both keys are set); `S3_FIELDS: tuple[str, ...]` = the seven `store_s3_*` names.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_settings_store.py
def test_s3_backend_needs_endpoint_bucket_and_keys(store: SettingsStore) -> None:
    with pytest.raises(StoreNotReadyError, match="endpoint, bucket, access key"):
        store.save(SettingsPatch(store_backend="s3"))

def test_s3_backend_saves_once_its_fields_are_set(store: SettingsStore) -> None:
    store.save(SettingsPatch(store_s3_endpoint="http://garage:3900", store_s3_bucket="sb",
                             store_s3_prefix="prod", store_s3_access_key_id="GK1",
                             store_s3_secret_access_key="s3cr3t"))
    saved = store.save(SettingsPatch(store_backend="s3"))
    assert saved.store_backend == "s3"
    assert saved.s3_settings() == S3Settings(endpoint="http://garage:3900", bucket="sb", prefix="prod",
                                             access_key_id="GK1", secret_access_key="s3cr3t")

def test_render_store_settings_carry_s3(store: SettingsStore, pool: Pool) -> None:
    ...save the five fields and store_backend="s3"...
    current = load_render_store_settings(pool, defaults)
    assert current.store_backend == "s3" and current.s3 is not None and current.s3.bucket == "sb"

def test_the_secret_access_key_is_sealed(store: SettingsStore, pool: Pool) -> None:
    # as the existing bambuddy_render_api_key sealing test does: the raw row is not the plaintext
```

```python
# tests/test_render_worker_role.py: extend test_the_role_holds_every_grant_and_cannot_read_the_settings_table
# to SELECT name FROM render_settings and assert every S3_FIELDS name a save wrote is readable.
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd backend && uv run --frozen pytest tests/test_settings_store.py -k s3 tests/test_render_worker_role.py -v`
Expected: FAIL (`store_s3_endpoint` is not a field; `S3Settings` undefined).

- [ ] **Step 3: Add the fields**

`config.py:91`: `StoreBackend = Literal["local", "bambuddy", "s3"]`.
`settings.py`: seven fields after `store_backend`, `store_s3_endpoint: str | None = None`, `store_s3_region: str = "us-east-1"`, `store_s3_bucket: str | None = None`, `store_s3_prefix: str = "scadbuddy"`, `store_s3_access_key_id: str | None = None`, `store_s3_secret_access_key: str | None = Field(default=None, repr=False)`, `store_s3_path_style: bool = True`; add `store_s3_secret_access_key` to `SECRET_FIELDS`; all seven `"restart"` in `APPLIES`; a description each (the endpoint's says "Garage, MinIO or any S3-compatible service; path-style addressing").
`settings_store.py`: `S3_FIELDS` tuple; `RENDER_FIELDS += S3_FIELDS`; `STORE_READINESS |= set(S3_FIELDS)`; `class S3Settings`; `StoredSettings.s3_settings()`; in `save` and `_check_store_ready`, a `merged("store_backend") == "s3"` branch raising `StoreNotReadyError("the S3 store needs its endpoint, bucket, access key id and secret access key saved first")`; `RenderStoreSettings.s3: S3Settings | None = None` filled from `stored.s3_settings()`.

- [ ] **Step 4: Write the view migration**

Create `backend/scadbuddy/migrations/$(date -u +%Y%m%dT%H%MZ)_render_settings_s3.sql`: `CREATE OR REPLACE VIEW render_settings WITH (security_barrier) AS` the same body as `20261010T1517Z_render_settings.sql` with the seven `store_s3_*` names added to the `IN (...)` list. Header comment cites spec §4.2.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `cd backend && uv run --frozen pytest tests/test_settings_store.py tests/test_render_worker_role.py tests/api/test_settings.py -v`
Expected: PASS.

- [ ] **Step 6: Lint, format, type-check and commit**

Run: `cd backend && uv run --frozen ruff check . && uv run --frozen ruff format . && uv run --frozen mypy`

```bash
git add backend/scadbuddy/core/config.py backend/scadbuddy/core/settings.py backend/scadbuddy/library/settings_store.py backend/scadbuddy/migrations/*_render_settings_s3.sql backend/tests/test_settings_store.py backend/tests/test_render_worker_role.py
git commit -m "feat(store): S3 store settings, readiness and the render settings view"
```

---

### Task 2: `S3ContentBackend`

**Files:**
- Create: `backend/scadbuddy/store/s3.py`
- Modify: `backend/pyproject.toml` (deps `aiobotocore>=2.21,<3`; dev `moto[server]>=5.1`, `types-aiobotocore-s3`), `uv.lock` (with the image's uv, see memory `uv-lock-with-image-uv`)
- Create: `backend/tests/store/test_s3_backend.py`
- Modify: `backend/tests/support/store.py` (a `moto_s3` fixture helper)

**Interfaces:**
- Consumes: `ContentBackend` (`store/content.py:52-60`), `BlobKind`, `BlobData`, `BlobScope`, `blob_digest` (`content_models.py`), `BlobMissingError`, `S3Settings` (Task 1).
- Produces: `class S3ContentBackend: backend = "s3"; def __init__(self, settings: S3Settings, *, multipart_threshold: int = 64 << 20, part_size: int = 16 << 20) -> None; async def upload(self, kind, data, *, name, scope) -> str; def download(self, backend_id) -> AsyncGenerator[bytes]; async def exists(self, backend_id) -> bool; async def remove(self, backend_id) -> None; async def sweep_unrecorded(self, *, cutoff: datetime, recorded: Callable[[str], bool]) -> int; async def aclose(self) -> None`; `def object_key(prefix: str, kind: BlobKind, sha256: str) -> str` returning `f"{prefix}/{kind}/{sha256}"`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/store/test_s3_backend.py (fixture `s3` starts moto's ThreadedMotoServer on a free port,
# creates bucket "sb", yields S3Settings(endpoint=f"http://127.0.0.1:{port}", bucket="sb", prefix="t",
# access_key_id="testing", secret_access_key="testing"))
async def test_upload_returns_the_prefixed_kind_sha_key(s3: S3Settings) -> None:
    backend = S3ContentBackend(s3)
    key = await backend.upload("asset", b"<svg/>", name="a.svg", scope=SCOPE)
    assert key == f"t/asset/{hashlib.sha256(b'<svg/>').hexdigest()}"
    assert await backend.exists(key)

async def test_download_streams_the_bytes_and_a_missing_key_raises(s3) -> None:
    ...
    with pytest.raises(BlobMissingError): [c async for c in backend.download("t/asset/" + "0"*64)]

async def test_a_file_over_the_multipart_threshold_round_trips(s3, tmp_path) -> None:
    backend = S3ContentBackend(s3, multipart_threshold=5 << 20, part_size=5 << 20)
    data = os.urandom(11 << 20); f = tmp_path / "big"; f.write_bytes(data)
    with f.open("rb") as fh:
        key = await backend.upload("piece", fh, name="piece.zip", scope=SCOPE)
        assert fh.tell() == 0
    assert b"".join([c async for c in backend.download(key)]) == data

async def test_remove_is_idempotent(s3) -> None: ...

async def test_a_connection_error_is_not_a_missing_object() -> None:
    backend = S3ContentBackend(S3Settings(endpoint="http://127.0.0.1:9", ...))
    with pytest.raises(Exception) as excinfo:  # botocore EndpointConnectionError
        await backend.exists("t/asset/" + "0"*64)
    assert not isinstance(excinfo.value, BlobMissingError)

async def test_sweep_unrecorded_removes_old_unrecorded_objects_only(s3) -> None:
    # upload three; recorded() says yes for one; another is younger than cutoff (set via
    # moto's LastModified: upload it after reading `now`); exactly one removed
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd backend && uv run --frozen pytest tests/store/test_s3_backend.py -v`
Expected: FAIL with `ModuleNotFoundError: scadbuddy.store.s3`.

- [ ] **Step 3: Add the dependencies**

`pyproject.toml`: `aiobotocore>=2.21,<3` in `dependencies`; `moto[server]>=5.1` and `types-aiobotocore-s3` in the dev group. Run `uv lock` with the Dockerfile's uv version (`uvx uv@0.13.1 lock`).

- [ ] **Step 4: Implement `S3ContentBackend` in `backend/scadbuddy/store/s3.py`**

One `aiobotocore` session and a lazily created client (`endpoint_url`, `region_name`, credentials, `Config(s3={"addressing_style": "path" if path_style else "auto"})`). `upload`: digest in a thread with `blob_digest`, key by `object_key`, `head_object` first (a hit returns the key: idempotent); bytes or a file below the threshold go by `put_object`, above it by `create_multipart_upload`/`upload_part`/`complete_multipart_upload` reading `part_size` chunks in a thread, `abort_multipart_upload` on any exception; `data.seek(0)` after. `download`: `get_object`, iterate `Body.iter_chunks(1 << 20)`; a `NoSuchKey`/404 `ClientError` becomes `BlobMissingError`. `exists`: `head_object`; 404 is `False`, anything else propagates. `remove`: `delete_object` (idempotent on S3). `sweep_unrecorded`: paginate `list_objects_v2` under `prefix/`, delete each with `LastModified < cutoff` and `not recorded(key)`, return the count. Module docstring cites spec §4.1 and the S3 multipart limits (docs.aws.amazon.com/AmazonS3/latest/userguide/qfacts.html).

- [ ] **Step 5: Run the tests to verify they pass**

Run: `cd backend && uv run --frozen pytest tests/store/test_s3_backend.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add backend/pyproject.toml backend/uv.lock backend/scadbuddy/store/s3.py backend/tests/store/test_s3_backend.py backend/tests/support/store.py
git commit -m "feat(store): S3ContentBackend over aiobotocore"
```

---

### Task 3: `ContentStore` over S3

**Files:**
- Test: `backend/tests/store/test_content_store.py` (parametrise the backend), `backend/tests/store/test_worker_cache.py`

**Interfaces:**
- Consumes: `S3ContentBackend` (Task 2), `local_content` (`tests/support/store.py`).
- Produces: `tests/support/store.py: def s3_content(settings: S3Settings, pool: Pool, *, max_total_bytes=0, max_count=0) -> ContentStore`.

- [ ] **Step 1: Parametrise the existing store tests**

Add a `content` fixture in `tests/store/conftest.py` parametrised over `("local", "s3")` returning the store from `local_content` or `s3_content` (the `s3` fixture from Task 2 moves to `conftest.py`). Convert `test_put_then_read_and_the_same_bytes_are_stored_once`, the sha-check test (`BlobCorruptError`), `replace`'s CAS test and `sweep_content`'s test to use it; the test that `os.utime`s the local file stays local-only (`pytest.param(..., marks=...)` or a skip when `content.backend.backend != "local"`).

- [ ] **Step 2: Run them**

Run: `cd backend && uv run --frozen pytest tests/store/test_content_store.py tests/store/test_worker_cache.py -v`
Expected: PASS on both parameters with no change to `content.py`. If a test fails on `s3` only, the failure is in `S3ContentBackend` (fix it there) and never a change to `ContentStore`.

- [ ] **Step 3: Commit**

```bash
git add backend/tests/store/conftest.py backend/tests/store/test_content_store.py backend/tests/store/test_worker_cache.py backend/tests/support/store.py
git commit -m "test(store): run the content store tests over the S3 backend too"
```

---

### Task 4: `build_store` wires `s3`; the sweep covers unrecorded S3 objects

**Files:**
- Modify: `backend/scadbuddy/store/factory.py:37-58` (`StoreBundle.remote: ContentBackend | None`), `:61-127` (`build_store`), `:168-176` (`store_health`)
- Create: `backend/scadbuddy/store/backends.py`
- Modify: `backend/scadbuddy/main.py:332-370` (`_sweep_blobs_logged`)
- Modify: `backend/scadbuddy/store/bambuddy.py:282` (`sweep_unrecorded` gains the `recorded` keyword with its current `_recorded_id` as the default, so both backends share one signature)
- Test: `backend/tests/store/test_factory.py`, `backend/tests/test_main_sweeps.py` (or where `_sweep_blobs_logged` is tested; find with `grep -rn sweep_unrecorded tests`)

**Interfaces:**
- Consumes: `S3ContentBackend`, `RenderStoreSettings.s3` (Task 1).
- Produces: `backends.py: class UnrecordedSweeper(Protocol): async def sweep_unrecorded(self, *, cutoff: datetime, recorded: Callable[[str], bool]) -> int`; `def build_backend(backend: StoreBackend, current: RenderStoreSettings, source: RenderSettingsSource, pool: Pool) -> ContentBackend` (raises `StoreNotReadyError` for an unready `bambuddy` or `s3`; `ValueError` for `local`, which has no `ContentBackend` in production).

- [ ] **Step 1: Write the failing tests**

```python
# tests/store/test_factory.py
S3_READY = RenderStoreSettings(store_backend="s3", s3=S3Settings(endpoint="http://127.0.0.1:1", bucket="b", prefix="p", access_key_id="a", secret_access_key="s"))

async def test_s3_builds_the_content_bundle(tmp_path, pool) -> None:
    config, bundle = _build(tmp_path, pool, "s3", S3_READY)
    assert bundle.backend == "s3" and isinstance(bundle.blobs, CachedBlobStore)
    assert isinstance(bundle.remote, S3ContentBackend) and bundle.snapshots is not None
    assert (await store_health(bundle)).multi_worker is True

def test_s3_without_its_settings_refuses_to_start(tmp_path, pool) -> None:
    with pytest.raises(StoreNotReadyError, match=re.escape(RECOVER_LOCAL_SQL)):
        _build(tmp_path, pool, "s3", RenderStoreSettings(store_backend="s3"))

# the sweep test: with an S3 bundle, _sweep_blobs_logged calls sweep_unrecorded on it
# (spy via monkeypatch on S3ContentBackend.sweep_unrecorded) with recorded=index.shares_backend_id-style callable
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd backend && uv run --frozen pytest tests/store/test_factory.py -v`
Expected: FAIL (`build_store` has no `s3` branch; `S3_READY` has no `s3` field until Task 1 is merged, which it is).

- [ ] **Step 3: Implement**

`backends.py`: `build_backend` returns `BambuddyContentBackend(source.target, pool)` or `S3ContentBackend(current.s3)`; the readiness messages move here from `factory.py` (the Bambuddy one unchanged in wording). `factory.py`: `build_store` calls `build_backend` for any backend but `local`; `StoreBundle.remote` is typed `ContentBackend | None`. `main.py`: replace the `isinstance(backend, BambuddyContentBackend)` check with `isinstance(backend, UnrecordedSweeper)` (`runtime_checkable`), passing `recorded=partial(index.shares_backend_id, backend.backend)` where `index` is `state.store.content.index`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd backend && uv run --frozen pytest tests/store -v && uv run --frozen pytest tests/test_worker.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/scadbuddy/store/factory.py backend/scadbuddy/store/backends.py backend/scadbuddy/store/bambuddy.py backend/scadbuddy/main.py backend/tests/store/test_factory.py
git commit -m "feat(store): build the S3 bundle; sweep unrecorded S3 objects"
```

---

### Task 5: The migration between backends

**Files:**
- Create: `backend/scadbuddy/store/migrate.py`
- Create: `backend/scadbuddy/workflows/store_migration.py`
- Modify: `backend/scadbuddy/api/store.py` (routes), `backend/scadbuddy/main.py` (register the activity on the `library` worker beside the housekeeping activities, `:519-525`)
- Create: `backend/tests/store/test_migrate.py`, `backend/tests/api/test_store_migrate.py`

**Interfaces:**
- Consumes: `BlobIndex` (`store/index.py`: `stats`, `swap`), `build_backend` (Task 4), `start_command` (`workflows/commands.py`).
- Produces: `migrate.py: class MigrationBatch(BaseModel): moved: int; remaining: int; failed: list[str]`; `async def migrate_batch(index: BlobIndex, source: ContentBackend, target: ContentBackend, *, limit: int, heartbeat: Callable[[str], None]) -> MigrationBatch` (for each `store_blobs` row with `backend == source.backend`, ordered by `touched_at`, up to `limit`: stream `source.download(row.backend_id)` into `target.upload` through a spooled temp file, verify the sha256, then `index.swap(key, BlobRef(..., backend=target.backend, backend_id=new_id), expected=row's BlobRef, ...)`; a CAS miss, a `BlobMissingError` or a target failure records the key in `failed` and moves on). `store_migration.py: @workflow.defn class StoreMigration` with `@workflow.run async def run(self, params: StoreMigrationParams) -> StoreMigrationResult` (`params: source: StoreBackend, target: StoreBackend, batch: int = 50`; loops `store_migrate_batch` activities with `heartbeat_timeout=60s`, `start_to_close=30min`, continue-as-new every 200 batches, a `progress` query returning `{moved, remaining, failed}`); `@activity.defn(name="store_migrate_batch")`. Workflow id `store-migrate-<source>-<target>`. Routes: `POST /store/migrate` body `{"source": "bambuddy", "target": "s3"}` → 202 `{workflow_id}` (start-if-not-running via `start_command`'s update-with-start conflict policy `USE_EXISTING`); `GET /store/migrate` → the query, 404 when none runs.

- [ ] **Step 1: Write the failing tests**

```python
# tests/store/test_migrate.py (local -> s3 over LocalContentBackend and the moto fixture)
async def test_migrate_batch_moves_rows_and_bytes(pool, tmp_path, s3) -> None:
    src = local_content(tmp_path / "a", pool); ref = await src.put("piece", b"zip", name="p.zip", scope=SCOPE)
    tgt = S3ContentBackend(s3)
    result = await migrate_batch(BlobIndex(pool), src.backend, tgt, limit=10, heartbeat=lambda _: None)
    assert result == MigrationBatch(moved=1, remaining=0, failed=[])
    stat = BlobIndex(pool).get(ref_key)  # the key the put used
    assert stat.backend == "s3" and await tgt.exists(stat.backend_id)

async def test_a_missing_source_object_is_recorded_and_skipped(...) -> None: ...
async def test_a_row_touched_during_the_batch_is_left_for_the_next(...) -> None:  # CAS miss
async def test_a_row_left_on_the_old_backend_is_missing_not_an_error(pool, tmp_path, s3) -> None:
    # a ContentStore over S3 asked for a ref whose backend is "local" raises BlobMissingError
```

```python
# tests/api/test_store_migrate.py (requires_temporal; the api `client` fixture)
async def test_post_starts_once_and_get_reports_progress(client) -> None: ...
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd backend && uv run --frozen pytest tests/store/test_migrate.py tests/api/test_store_migrate.py -v`
Expected: FAIL with import errors.

- [ ] **Step 3: Implement `migrate.py`, the workflow, the activity and the routes**

`ContentStore.get` of a ref on another backend: add the check at the top of `_download` (`content.py:387`): `if ref.backend != self.name: raise BlobMissingError(...)` with a comment citing spec §4.3. The activity builds both backends with `build_backend` from `load_render_store_settings` at run time (the target's settings must be saved before `store_backend` is flipped, which Task 1's readiness allows). Register the activity in `main.py` beside the housekeeping activities so it runs in the API process (`library` queue, the data volume is not needed but the settings pool is).

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd backend && uv run --frozen pytest tests/store/test_migrate.py tests/api/test_store_migrate.py tests/store/test_content_store.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/scadbuddy/store/migrate.py backend/scadbuddy/workflows/store_migration.py backend/scadbuddy/store/content.py backend/scadbuddy/api/store.py backend/scadbuddy/main.py backend/tests/store/test_migrate.py backend/tests/api/test_store_migrate.py
git commit -m "feat(store): StoreMigration moves recorded blobs between backends"
```

---

### Task 6: Agent tool coverage and the activity record

**Files:**
- Modify: `agent/src/tools/coverage.ts` (or a new `agent/src/tools/store.ts` with `defineTool` for `store_migrate_start` / `store_migrate_status`, both `outward`)
- Test: `agent/test/coverage.test.ts` (existing; fails without the entry)

- [ ] **Step 1: Run the coverage test to see it fail**

Run: `cd agent && pnpm test -- coverage`
Expected: FAIL naming `POST /api/v1/store/migrate` and `GET /api/v1/store/migrate`.

- [ ] **Step 2: Add the tools**

`agent/src/tools/store.ts`: `store_migrate` (outward; starts) and `store_migrate_status` (read). Follow `tools/command.ts` for the `Idempotency-Key` on the POST.

- [ ] **Step 3: Run and commit**

Run: `cd agent && pnpm lint && pnpm typecheck && pnpm test`
Expected: PASS.

```bash
git add agent/src/tools/store.ts agent/test
git commit -m "feat(agent): store migration tools"
```

---

### Task 7: Settings page and mocks

**Files:**
- Modify: `frontend/src/pages/SettingsPage.tsx:86` (`SECRETS` gains `store_s3_secret_access_key`), `:100` (the `projects` group gains the S3 fields), `:346-350` (`chosenBackend` logic: `s3` is choosable when `s3Ready`), `:1171-1192` (the `ChoiceSelect` gains `{ value: 's3', label: 'S3-compatible object store (any number of render workers)', disabled: !s3Ready }`), `:1205` (`Where` row label for `s3`)
- Modify: `frontend/src/mocks/features/settings.ts:48` (`STORE_READINESS` gains the S3 fields and the 422 for `s3`), `frontend/src/mocks/fixtures.ts:943-956` (applies and defaults)
- Test: `frontend/src/pages/SettingsPage.sections.test.tsx`, `frontend/src/mocks/handlers.test.ts`

- [ ] **Step 1: Write the failing tests**

```tsx
it('offers the S3 store once its endpoint, bucket and keys are saved', async () => { ... })
it('refuses to switch to S3 without its fields', async () => { ...expect 422 text... })
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd frontend && pnpm test -- SettingsPage.sections`
Expected: FAIL.

- [ ] **Step 3: Implement**

A new `FieldRow` group "S3 store" under the Blob store select with the six text fields and the path-style switch; the secret field follows the `bambuddy_render_api_key` pattern (`:871-895`: badge, clear button, `has_*` flag). The `Where` row shows "S3 object store" for `s3`.

- [ ] **Step 4: Run and commit**

Run: `cd frontend && pnpm lint && pnpm typecheck && pnpm test && pnpm build`
Expected: PASS.

```bash
git add frontend/src/pages/SettingsPage.tsx frontend/src/mocks frontend/src/pages/SettingsPage.sections.test.tsx
git commit -m "feat(frontend): S3 blob store settings"
```

---

### Task 8: README and the deploy notes

**Files:**
- Modify: `README.md` "Deploying" (`:346`) and "Recovering an unready blob store" (`:761-775`)
- Modify: `docs/superpowers/specs/2026-09-27-template-pipelines-design.md` §6 (one sentence: `s3` is the second multi-worker backend, see the 2026-10-16 spec)

- [ ] **Step 1: Write**

"Deploying" gains a "Blob store on S3" subsection: the seven settings and their env seeds, path-style, the bucket's prefix, Garage/MinIO as examples (link each project's docs), and the switch procedure from spec §4.3 (save S3 settings, `POST /store/migrate`, wait for `remaining: 0`, flip `store_backend`, restart). "Recovering" says the SQL applies to `s3` too.

- [ ] **Step 2: Commit**

```bash
git add README.md docs/superpowers/specs/2026-09-27-template-pipelines-design.md
git commit -m "docs: deploying the blob store on S3"
```

---

## Self-review

- Spec coverage: §4.1 protocol, key, idempotency, multipart (Task 2); §4.2 settings, readiness, view (Task 1); §4.3 migration, cross-backend miss (Task 5); §4.4 docs (Task 8); §4.6 local unchanged (Task 3 parametrisation shows it). §4.5, the native library over the store that `bambuddy-fs` mounts, is its own plan (#3827) and depends on this one's `S3ContentBackend` and `BlobKind` only. Streaming `read` callers are deferred to the Rust transfer work per §4.1, not this plan.
- Types: `S3Settings` (Task 1) is what `S3ContentBackend` and `build_backend` take; `UnrecordedSweeper.sweep_unrecorded(*, cutoff, recorded)` is the signature both backends implement (Task 4 changes the Bambuddy one's signature by a keyword with a default, so its existing tests pass unchanged).
- Review Focus 1 to 5 each name their task and test.
