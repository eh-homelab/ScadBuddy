# Storefront phase 3: the public site (listings, visitors, customize, render limits, estimate, drafts) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A visitor with no account browses the shop's published listings, customizes one within the bounds the shop set, renders it under per-visitor limits, sees a rough price, and adds it (or a design request) to a draft order. The shop manages listings on a back-office Storefront page (#5282).

**Architecture:** The public site is an edge process, `python -m scadbuddy.storefront`, on port 8083 from the same image. It holds no database secret, no KEK, no S3 key and no Temporal client. It serves the page, signs and checks the visitor cookie with a key of its own, limits requests per client and checks `Origin`. It then calls the CRUD service (the API process) at `/api/v1/internal/crud/storefront/*` with a #5325 service credential of caller `storefront`: a projected ServiceAccount token exchanged at `POST /api/v1/auth/service-token` for a 15-minute credential scoped `crud.storefront`. This is the pattern the render and projects workers use since #5298 (`crud/guard.py`, `crud/client.py`, `auth/service_credentials.py` `exchange_source`). The CRUD routes are the boundary. Each does only what the public may do: it reads published listings only, scopes every draft, render and estimate to the visitor id the edge sends, never trusts the edge to have checked ownership, and counts the render limits in Postgres. The listing, limit, render, estimate and draft code runs in the API, in `backend/scadbuddy/orders/storefront/`. The page is a second Vite entry, `frontend/storefront.html` and `frontend/src/storefront/`.

**Tech Stack:** Python 3.12, FastAPI, uvicorn, httpx, pydantic-settings, psycopg 3 (API side only), Postgres 18, Temporal (API side only), S3 content store, pytest, respx; React 19, Vite (rolldown), msw, vitest, Playwright; ESLint `no-restricted-imports`.

**Spec:** `docs/superpowers/specs/2026-11-02-storefront-design.md` §5.1, §5.2 (the visitor part only), §5.3, §5.4, §5.8 (the per-client limits phase 3 needs), §6.1 (`orders.source`, the list filter), §10, §11 item 3. Issue #5282. The owner's decision of 2026-11-07 ("No direct SQL", recorded on #5282 and #4962) is in the spec already: §2 item 5, §3, §5.1 and §10 as amended by PR #5642 (branch `docs/storefront-no-sql`). This plan follows that text and does not amend the spec. The credential: `docs/superpowers/specs/2026-11-05-service-credentials-without-db-design.md` (#5325); its exchange (`api/service_token.py`, `auth/exchange.py` TokenReview and bootstrap verifiers, `ExchangeReader`) is on `main` today, so nothing here waits for it. The CRUD pattern: #5298 (PR #5561), the service credential: PR #5211, the internal routes' authentication: #5297, the rule: root `CLAUDE.md` "Database access (#5295)". The next phase is #5283; the payments seam is #4957 (phase 2, plan `docs/superpowers/plans/2026-11-07-storefront-phase-2-payments-deposits.md`, written in parallel).

Not in this phase (#5283): submit, `submitted_at` being written, order numbers for storefront orders, accounts, order links, mail, the customer's order page, revision answers by the customer, the `customer` actor kind, Turnstile, payment UI. Not in this phase (#5284): the clusters Deployment, the public host, the README section.

## Global Constraints

- **The edge holds no database secret.** `python -m scadbuddy.storefront` reads only its own settings class, `StorefrontSettings`. That class has no `database_url`, `secret_key_file`, `temporal_address` or S3 field. The process refuses to start (exit code 2) when `SCADBUDDY_DATABASE_URL`, `PGPASSWORD`, `PGPASSFILE` or `SCADBUDDY_SECRET_KEY_FILE` is set in its environment. It never imports `psycopg_pool`, `scadbuddy.orders`, `scadbuddy.render`, `scadbuddy.workflows`, `scadbuddy.store` or `scadbuddy.library`; `backend/tests/storefront/test_edge.py` checks both.
- **The CRUD routes are the boundary.** Every route under `/api/v1/internal/crud/storefront/` (the standard CRUD path; spec §5.1 names `/api/v1/internal/storefront/`) is guarded by `crud/guard.py` `require_crud("storefront")` (a live `storefront` credential with scope `crud.storefront`, in every `SCADBUDDY_AUTH_MODE`). It is hidden from the OpenAPI schema (`include_in_schema=False`, as every CRUD router is). It never trusts the edge for ownership: see "Trust model".
- New service caller `storefront`: `ServiceCaller` gains it; `SCOPES["storefront"] = frozenset({"crud.storefront"})`; `auth/exchange.py` `SERVICE_ACCOUNTS["scadbuddy-storefront"] = "storefront"`; `crud/guard.py` `CrudCaller` and `auth/routes.py` `CRUD_CLASSES` gain `"storefront"`. It is an exchange-only caller (`exchange_source`, the #5325 credential); it has no `ROLES` or `VIEWS` entry and no sealed credential. Its CRUD routes take the standard path, `/internal/crud/storefront/`, as every caller's do: `crud_router`, `CrudClient`, `wait_for_areas` and `auth/routes.py` `CRUD_PATH` (`/internal/crud/<caller>/…` → class `crud.<caller>`) cover it with no special case, and the CRUD port (`crud/listener.py`) serves it.
- The storefront edge never runs openscad, `openscad-lsp`, git or template Python. The CRUD side reaches them only as the API does today: a render starts a Temporal workflow that the render workers run.
- The edge mounts no `/api/v1` route and never proxies `/api/v1` (spec §3 anti-goal). Its routes are `/api/store/v1/*`, `/healthz` and the page.
- The CRUD side's storefront routes refuse with 409 `storefront_needs_s3` unless `store_backend` is `s3` (spec §5.1). The edge mounts no data volume and stores nothing on disk.
- Port `8083`. The edge's environment (spec §10): `SCADBUDDY_API_INTERNAL_URL` (the CRUD service), `SCADBUDDY_SERVICE_TOKEN_FILE` (a projected token of ServiceAccount `scadbuddy-storefront`; `SCADBUDDY_SERVICE_BOOTSTRAP_FILE` for a run outside Kubernetes), `SCADBUDDY_STOREFRONT_COOKIE_KEY_FILE` (the cookie's signing key, at least 32 bytes, its own and never the KEK), `SCADBUDDY_STOREFRONT_PUBLIC_URL` (prod: `https://michaels-workshop.nullreference.io`), `SCADBUDDY_TRUSTED_PROXIES`, `SCADBUDDY_STOREFRONT_DIST` (the built page, default `/app/frontend/dist`). No database URL, Temporal address, store credential or `SCADBUDDY_SECRET_KEY_FILE`.
- Stored settings, each with its default: `storefront_renders_per_minute` 3, `storefront_renders_per_day` 30, `storefront_render_concurrency` 2, `storefront_draft_ttl_days` 30, `storefront_requests_enabled` false. All `live`, read by the CRUD side only.
- Over a render limit is `429` with `Retry-After` (whole seconds, at least 1) and the problem type `https://scadbuddy.dev/problems/storefront/render_limited`; the edge passes the CRUD side's 429, header and problem through. A reused render costs nothing.
- Visitor cookie: name `sb_visitor`, value `v1.<visitor id>.<mac>`, where the visitor id is 32 random bytes as URL-safe base64 (43 characters) and the mac is HMAC-SHA256 over `v1.<visitor id>` under the cookie key, URL-safe base64. `Secure`, `HttpOnly`, `SameSite=Lax`, `Path=/`, `Max-Age` 400 days. The CRUD side stores only the visitor id's sha256 (hex), as §5.2 says.
- Request uploads: at most 25 MB a file and 10 files a request; only `image`, `video` and `pdf` kinds as `orders/attachments.py` `sniff` types them (spec §5.4). The edge and the CRUD side each enforce these.
- The estimate is shown as "about $X, confirmed after we check it", never as a quote (spec §5.4).
- Customer text (a request's text, upload names) is rendered as text, never as HTML (spec §5.8).
- `orders.customer_id` and `orders.number` may be null only when `source = 'storefront' AND submitted_at IS NULL` (spec §5.4). Phase 3 never writes `submitted_at`.
- New migrations are new files only, named `$(date -u +%Y%m%dT%H%MZ)_<slug>.sql` in `backend/scadbuddy/migrations/` (root `CLAUDE.md` "Migrations").
- `backend/openapi.json`, `frontend/src/api/schema.d.ts`, `agent/src/api/schema.d.ts` and the new `frontend/src/storefront/api/schema.d.ts` are never committed.
- Every new public `/api/v1` operation (the back office's listing routes) gets an agent tool or a `NOT_A_TOOL` entry in `agent/src/tools/coverage.ts`. The internal CRUD routes get none: they are not in `backend/openapi.json`, and `agent/test/coverage.test.ts` fails on an entry for an operation the spec lacks.
- PR titles are conventional commits with scope `storefront`; each PR body says `Part of #5282` (the last says `Fixes #5282`).

### Trust model

- **Who the visitor is.** The edge mints the visitor id (32 random bytes) and signs it into the cookie with its own key. On each request it checks the mac and drops a cookie that fails (a new visitor gets a new id). It sends the verified visitor id on each CRUD call, in the header `X-ScadBuddy-Visitor` (spec §5.1: "the visitor id the storefront sends"). The CRUD side does not hold the cookie key and cannot check the mac. It stores and looks up the id only by its sha256 (`storefront_visitors.token_sha256`, §5.2). It creates the row on a write, and on a read it answers as for a visitor with nothing. The id is a 256-bit random secret, never a row id: the edge never sees `storefront_visitors.id`, and no route takes or returns one. So knowing one visitor's row, order or job id never lets a caller act as that visitor. An id that is not 43 URL-safe characters is 400.
- **Ownership checks live on the CRUD side.** Every draft read and write locks the visitor's own draft (`orders.storefront_visitor_id`) and checks that each item id it is given belongs to that draft. Every render, preview and estimate read checks for a `storefront_renders` row of that visitor and job. Listings are read only when `published`. The CRUD side checks inputs against the listing, enforces the upload caps and sniffs the bytes. It counts the render limits and the global concurrency limit in Postgres. It never relies on the edge having done any of this.
- **What the CRUD side takes on trust from the edge.** The client address, in the header `X-ScadBuddy-Store-Client`, is accepted only from a `storefront` credential and keys the per-client render limit. The CRUD side cannot see the browser, so the `Origin` check and the per-request rate limit live only at the edge.
- **If the edge is compromised,** it can act as any visitor whose cookie passes through it while it is compromised, and it can forge cookies for new visitors, each held to the per-visitor and global limits. It can lie about client addresses. It cannot read or change any back-office record, any submitted order (phase 3's routes reach unsubmitted storefront drafts only), any other caller's CRUD route, the settings, the KEK, the store, Temporal, or a visitor whose id it never saw. Its secrets are the cookie key and its exchanged 15-minute credential.
- **The cookie key** is the edge's own (`SCADBUDDY_STOREFRONT_COOKIE_KEY_FILE`, spec §10: "a signing key of its own … never the KEK"). It lets the edge drop forged or garbled cookies before any CRUD call. Rotating it starts every visitor afresh; their old drafts age out through the sweep.

### Spec points this plan settles (each a call the owner may overturn)

1. **Uploads go through a CRUD route, not a presigned PUT.** Spec §5.1 allows "URLs or routes the CRUD service gives it". A presigned PUT would let a stranger's bytes reach the store before anything sniffed or counted them. So the edge streams each file to the CRUD route and counts its bytes as they stream. The CRUD side counts again, sniffs, puts `attachment-<sha256>` into the content store, then calls `Lifecycle.add_attachment`, which writes the row and its `blob_refs` holder in one transaction. There is no claim store and no `library`-queue operation.
2. **Publish captures everything that needs git or openscad.** Publishing (an API route) stores on the listing the commit's customizer schema, whether the template is a pipeline, its name and its library pins. It holds the commit's source snapshot (`store/snapshots.py` `SnapshotStore.ensure`) under `blob_refs (storefront_listing, <id>)`, and copies each chosen media file into the content store as `listing-media-<sha256>` under the same holder. A visitor's render then needs no git read on the hot path: the CRUD side builds the `Reference` from the snapshot (`orders/storefront/inputs.py` `listing_reference`), and a parity test pins it to `orders/reference.py` `resolve_reference`.
3. **The render preview is streamed by the CRUD side.** A render's `preview.glb` lives inside a directory blob (`store/cache.py` `materialize_result`), which no presigned GET can address. The CRUD route streams it, and the edge streams it on. Listing media and request attachments are flat blobs, served as presigned GETs (`store/s3.py` `presign_get`) that the CRUD side mints.
4. **The estimate is a price, without machine time.** `orders/pricing.py` `price` gains `estimated=True`: a plate with no seconds gets no machine line instead of a `PricingError`. Every other line (filament, setup fee per plate, labour, markup, rounding) is the quote's formula. Each part's filament is the listing filament the visitor chose for its colour.
5. **Estimate volumes are cached per render job** in `storefront_job_parts`, so two visitors on one render analyse its 3MF once.
6. **A visitor row is written on the first write, not the first request.** The edge sets the cookie on the first request; the CRUD side writes `storefront_visitors` on the first render or add. Its `token_sha256` column holds the sha256 of the visitor id the edge minted (§5.2's "its sha256").
7. **Phase 3's actor.** `Actor.kind` gains `visitor`, with `principal` `storefront_visitor:<visitor id>`, which only the CRUD side builds, from the row it found by the visitor id's hash. Phase 4 adds `customer` (§6.4).
8. **One draft per visitor**, by a unique partial index on `orders (storefront_visitor_id) WHERE submitted_at IS NULL`.
9. **The shop cannot act on an unsubmitted draft.** `order_not_submitted` (§6.3) lands now on `accept_order`, `record_manual_quote`, the `order_item_quote` check and the `order_item_print` check.
10. **"No activity"** for the draft sweep is the later of `orders.updated_at` and the visitor's `last_seen_at`.
11. **The draft sweep runs on the prune Schedule too** (`PRUNE_SWEEPS`), as the auth-session sweep does. It also deletes visitors with no order idle past the TTL, and `storefront_renders` rows older than a day.
12. **Listing colours** are price-book filament ids. A `// color` parameter a visitor may change takes only those filaments' colours; each item colour slot names the chosen filament.
13. **Listing parameters not named** in `params` or `fixed` are fixed at the schema default when published.
14. **Limits per visitor and per client** use the same three numbers; the concurrency limit is global (spec: "across all visitors").
15. **The edge's per-client request limit** (§5.8): 120 requests a minute per client on reads, 30 on writes, in memory per replica. The render limits, which cost money, are counted in Postgres by the CRUD side.
16. **Writes check `Origin`** at the edge against `SCADBUDDY_STOREFRONT_PUBLIC_URL` (`api/cross_site.py` `CrossSiteGate`), beside `SameSite=Lax`.
17. **The edge serves only its own page's files**: `storefront.html` and the chunks in its import closure, read from Vite's `.vite/manifest.json`.
18. **Back-office listing routes are not agent tools in phase 3.** They get `NOT_A_TOOL` entries ("the shop edits listings on the Storefront page; an agent tool follows with #5283").
19. **The shop's live preview** mounts the storefront's own `ListingView` component with the unsaved listing. The back office may import from `src/storefront/`; the reverse is the lint rule.
20. **The storefront's CRUD routes are at the standard path `/api/v1/internal/crud/storefront/*`** (controller decision, 2026-11-07; spec §5.1 names `/api/v1/internal/storefront/*`), so `require_crud`, the CRUD headers, the idempotency ledger, `CrudClient`'s retries and `GET …/areas` apply unchanged.
21. **Every edge write sends an `Idempotency-Key`** (a UUID per browser request, kept across the CRUD client's retries), and the CRUD side records it through `crud/idempotency.py` `idempotent_write`. A retried render is therefore counted once against the limits, and a retried add makes one item.

## Review Focus

1. **Two tabs of one visitor add their first item at once.** Expected: one draft, both items on it (the unique index refuses the second insert; `ensure_draft` reads the winner). Test in Task 8.
2. **A back-office reader meets a draft with no customer or number** (`GET /orders?source=storefront&unsubmitted=true`, `GET /orders/{id}`, the agent's `order_get`, the profit report, the costs read). Expected: each answers, with `customer` and `number` null, never a 500. Test in Task 4.
3. **A visitor asks for a render whose job is already open for another visitor** (same inputs, still running). Expected: joined, not counted, and the second visitor can poll it. Test in Task 7.
4. **The edge retries a render or an add after a lost answer** (the CRUD client retries 5xx and transport failures for 30 s with the same key). Expected: one counted render, one item. Test in Tasks 7 and 9.
5. **The shop unpublishes or republishes a listing** while a visitor customizes it. Expected: a render or add against the old state answers 409 `listing_changed`, and an item already in a draft keeps its revision 1. Test in Task 8.

---

## File structure

Backend, CRUD side (runs in the API, `backend/scadbuddy/`):
- `orders/storefront/__init__.py`, `listings.py` (`ListingStore`, models, `publish`), `inputs.py` (bounds, `listing_reference`), `visitors.py` (`VisitorStore`, `visitor_actor`), `limits.py` (`RenderLimits`), `renders.py` (render, status, preview, estimate), `drafts.py` (the draft and its items), `wire.py` (the shapes both sides share: `PublicListing`, `StoreRender`, `StoreEstimate`, `DraftView`, the request bodies) (create).
- `api/crud_storefront.py` (create): the CRUD routes; `api/crud_areas.py` (modify): `CRUD_AREAS["storefront"]`.
- `api/storefront.py` (create): the back office's `/api/v1/storefront/listings` routes.
- `orders/estimate.py` (create); `orders/models.py`, `orders/store.py`, `orders/lifecycle.py`, `orders/pricing.py`, `orders/quote.py`, `orders/printing.py`, `orders/costs.py`, `orders/report.py`, `api/orders.py` (modify).
- `auth/service_credentials.py`, `auth/exchange.py`, `auth/routes.py`, `crud/guard.py` (modify): the `storefront` caller.
- `core/settings.py`, `library/settings_store.py` (modify): the stored settings.
- `workflows/housekeeping.py`, `main.py` (modify): the draft sweep; mount `api/crud_storefront.py`.
- `migrations/<ts>_storefront_drafts.sql`, `<ts>_storefront_listings.sql`, `<ts>_storefront_renders.sql` (create).

Backend, edge (`backend/scadbuddy/storefront/`): `__init__.py`, `__main__.py`, `settings.py` (`StorefrontSettings`), `app.py`, `visitors.py` (the cookie), `ratelimit.py`, `routes.py`, `static.py`; `backend/scadbuddy/crud/storefront.py` (`StorefrontCrud`, the edge's client) (create). `backend/scadbuddy/tools/export_openapi.py` (modify): `--storefront`.

Backend tests (`backend/tests/`): `storefront/test_edge.py`, `storefront/test_static.py`, `storefront/test_edge_routes.py`, `crud/test_storefront_routes.py`, `orders/storefront/test_listings.py`, `orders/storefront/test_inputs.py`, `orders/storefront/test_limits.py`, `orders/storefront/test_renders.py`, `orders/storefront/test_drafts.py`, `orders/test_estimate.py`, `orders/test_storefront_drafts.py`, `api/test_orders_storefront.py`, `api/test_storefront_listings.py`, `auth/test_exchange.py`, `test_housekeeping.py`, `test_pg_migrations.py`.

Frontend (`frontend/`): `storefront.html`, `vite.config.ts`, `eslint.config.js`, `scripts/gen-api.mjs`, `src/storefront/` (`main.tsx`, `App.tsx`, `api/client.ts`, `pages/ListingsPage.tsx`, `pages/CustomizePage.tsx`, `pages/DraftPage.tsx`, `components/ListingView.tsx`, `components/Estimate.tsx`, `components/RequestForm.tsx`), `src/components/Preview.tsx` (modify: `PreviewJob`), `src/pages/StorefrontPage.tsx`, `src/components/storefront/ListingEditor.tsx`, `src/mocks/features/storefront.ts`, `src/mocks/features/storefrontPublic.ts`, `e2e/storefront.spec.ts`, `e2e/storefront-admin.spec.ts`. Root `.gitignore`: `frontend/src/storefront/api/schema.d.ts`. `Dockerfile`: the `api-spec` stage also exports the storefront spec.

Docs: none (the spec and `docs/security/trust-boundaries.md` were amended in PR #5642). Agent: `agent/src/tools/coverage.ts`.

## PRs

| PR | Tasks | Title |
|---|---|---|
| 1 | 1, 2 | `feat(storefront): the storefront edge process and its service caller` |
| 2 | 3, 4 | `feat(storefront): storefront draft orders and the settings` |
| 3 | 5 | `feat(storefront): listings and the back office's Storefront page` |
| 4 | 6, 7 | `feat(storefront): visitors, customize, render limits and the estimate` |
| 5 | 8, 9, 10 | `feat(storefront): add to the draft, design requests and the draft sweep` |
| 6 | 11 | `feat(storefront): the public page` |
| 7 | 12 | `feat(storefront): the visitor's payment requests` (after phase 2 is on main) |

Each PR is based on `main` after the one before it merged (no stacked auto-merge).

---

### Task 1: The edge process, with no database

**Files:**
- Create: `backend/scadbuddy/storefront/__init__.py`, `__main__.py`, `settings.py`, `app.py`, `routes.py`
- Test: `backend/tests/storefront/test_edge.py`

**Interfaces:**
- Consumes: `core/proxies.py` `parse_cidr_list`; `core/problems.py` `ApiError` and the app's problem handler.
- Produces:
  - `class StorefrontSettings(BaseSettings)` (`env_prefix="SCADBUDDY_"`): `api_internal_url: str`, `service_token_file: Path | None = None`, `service_bootstrap_file: Path | None = None` (exactly one required), `storefront_cookie_key_file: Path` (refused when the file holds fewer than 32 bytes), `storefront_public_url: str` (an `https://` origin with no path), `trusted_proxies: str = ""` (parsed once, into `app.state.trusted_proxies`), `log_level: str = "INFO"`. Task 11 (the public page) adds `storefront_dist: Path = Path("/app/frontend/dist")`, the one that serves it.
  - `FORBIDDEN_ENV: Final = ("SCADBUDDY_DATABASE_URL", "PGPASSWORD", "PGPASSFILE", "SCADBUDDY_SECRET_KEY_FILE")`.
  - `class EdgeRefusal(Exception)` with `reasons: list[str]`; `check_environment(environ: Mapping[str, str]) -> None` raises it naming each `FORBIDDEN_ENV` variable set.
  - `STOREFRONT_PORT: Final = 8083`; `STOREFRONT_PREFIX: Final = "/api/store/v1"`.
  - `create_app(settings: StorefrontSettings, *, crud: StorefrontCrud | None = None) -> FastAPI`: `None` builds the real client (Task 2); `GET /healthz` answers `{"status": "ok"}`.
  - `main(argv: Sequence[str] | None = None) -> int`: `check_environment(os.environ)`; on a refusal, logs each reason and returns 2; else runs uvicorn on `0.0.0.0:8083` with `proxy_headers=False`.

- [ ] **Step 1: Write the failing tests** in `backend/tests/storefront/test_edge.py`

```python
def test_no_back_office_route_is_mounted(edge_app: FastAPI) -> None:
    paths = [r.path for r in edge_app.routes]  # type: ignore[attr-defined]
    assert [p for p in paths if p.startswith("/api/") and not p.startswith("/api/store/v1")] == []
    with TestClient(edge_app) as c:
        assert c.get("/api/v1/orders").status_code == 404
        assert c.get("/healthz").json() == {"status": "ok"}

@pytest.mark.parametrize("name", FORBIDDEN_ENV)
def test_a_database_or_key_variable_refuses_the_start(name: str) -> None:
    with pytest.raises(EdgeRefusal) as refused:
        check_environment({name: "x"})
    assert refused.value.reasons == [f"{name} is set; the storefront holds no database secret or key"]

def test_main_exits_2_with_a_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCADBUDDY_DATABASE_URL", "postgresql://x")
    assert main([]) == 2

def test_the_edge_starts_and_opens_no_database_connection(edge_env: None, monkeypatch: pytest.MonkeyPatch, crud_mock: respx.Router) -> None:
    def refuse(*_a: object, **_k: object) -> NoReturn:
        raise AssertionError("the storefront opened a database connection")
    monkeypatch.setattr(psycopg.Connection, "connect", refuse)
    monkeypatch.setattr(psycopg_pool.ConnectionPool, "__init__", refuse)
    with TestClient(create_app(StorefrontSettings())) as c:
        assert c.get("/healthz").status_code == 200
        assert c.get("/api/store/v1/listings").status_code == 200

def test_the_edge_imports_nothing_that_touches_the_database() -> None:
    out = subprocess.run([sys.executable, "-I", "-c",
        "import scadbuddy.storefront.app, sys; print(sorted({m.split('.')[0] if m.startswith('psycopg') else '.'.join(m.split('.')[:2])"
        " for m in sys.modules if m.startswith(('psycopg_pool', 'scadbuddy.orders', 'scadbuddy.render', 'scadbuddy.workflows',"
        " 'scadbuddy.store', 'scadbuddy.library', 'scadbuddy.main'))}))"],
        capture_output=True, text=True, check=True, cwd=BACKEND)
    assert out.stdout.strip() == "[]"
```

`edge_env` sets `SCADBUDDY_API_INTERNAL_URL`, `SCADBUDDY_SERVICE_BOOTSTRAP_FILE` (a temp file), `SCADBUDDY_STOREFRONT_COOKIE_KEY_FILE` (32 random bytes in a temp file) and `SCADBUDDY_STOREFRONT_PUBLIC_URL`, and deletes every `FORBIDDEN_ENV` variable. `crud_mock` is a `respx` router for the CRUD service: it answers the exchange and `GET …/storefront/listings` with the `X-ScadBuddy-Crud: 1` header. If `auth/service_credentials.py` imports `psycopg` at module level, `exchange_source` and `ServiceAuth` move to a module that does not (`auth/service_auth.py`, re-exported from the old path), so that the import test holds.

- [ ] **Step 2: Run them to verify they fail**

Run: `cd backend && uv run --frozen pytest tests/storefront/test_edge.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'scadbuddy.storefront'`

- [ ] **Step 3: Implement** the Interfaces block.

- [ ] **Step 4: Run them to verify they pass**

Run: `cd backend && uv run --frozen pytest tests/storefront/test_edge.py -v`
Expected: PASS (the listings call answers once Task 2 adds the client; until then that line is in Task 2's test)

- [ ] **Step 5: Commit**

```bash
git add backend/scadbuddy/storefront backend/tests/storefront
git commit -m "feat(storefront): the storefront edge process, with no database secret"
```

### Task 2: The `storefront` service caller, its CRUD router and the edge's client

**Files:**
- Create: `backend/scadbuddy/api/crud_storefront.py`, `backend/scadbuddy/crud/storefront.py`
- Modify: `backend/scadbuddy/auth/service_credentials.py` (`ServiceCaller`, `SCOPES` at `:69`), `backend/scadbuddy/auth/exchange.py` (`SERVICE_ACCOUNTS` at `:112`), `backend/scadbuddy/auth/routes.py` (`CRUD_CLASSES` at `:180`, `CRUD_PATH` at `:179` matches `/internal/crud/storefront` unchanged), `backend/scadbuddy/crud/guard.py` (`CrudCaller`; `crud_router` at `:148` is unchanged), `backend/scadbuddy/crud/client.py` (the path at `:116` and `wait_for_areas` at `:391` are unchanged), `backend/scadbuddy/api/crud_areas.py` (`CRUD_AREAS`), `backend/scadbuddy/main.py` (mount the router), `backend/scadbuddy/storefront/app.py` (build the client, wait for the areas)
- Test: `backend/tests/crud/test_storefront_routes.py`, `backend/tests/auth/test_exchange.py`, `backend/tests/storefront/test_edge.py`

**Interfaces:**
- Consumes: `crud/guard.py` `crud_router(caller)`, `require_crud(caller)`; `crud/client.py` `CrudClient(base_url, caller, auth, *, metrics)`, `wait_for_areas`; `auth/service_credentials.py` `exchange_source(caller, settings)`, `ServiceAuth`.
- Produces:
  - `ServiceCaller = Literal["agent", "print", "projects", "render", "storefront"]`; `SCOPES["storefront"] = frozenset({"crud.storefront"})`; `SERVICE_ACCOUNTS["scadbuddy-storefront"] = "storefront"`; `CRUD_CLASSES["storefront"] = "crud.storefront"`; `CRUD_AREAS["storefront"] = []` (each later task appends its area).
  - `router = crud_router("storefront")` in `api/crud_storefront.py`, mounted in `main.py` beside `crud_render`.
  - `VISITOR_HEADER: Final = "X-ScadBuddy-Visitor"`, `CLIENT_HEADER: Final = "X-ScadBuddy-Store-Client"` in `orders/storefront/wire.py` (no database import, so the edge may import it).
  - `def visitor_id(request: Request) -> str` (a dependency in `api/crud_storefront.py`): the header's value; 400 `visitor_id` unless it matches `^[A-Za-z0-9_-]{43}$`.
  - `def store_client(request: Request) -> str`: the header's value, an IP address; 400 otherwise.
  - `class StorefrontCrud` in `crud/storefront.py`: one async method per CRUD route, each taking `visitor_id: str`, `client: str`, and for a write `key: str`; it sends the two headers and `Idempotency-Key`. A `CrudRefusedError` carries the problem the edge answers with, status and `Retry-After` included.
  - `StorefrontSettings` gains `exchange_source`-compatible fields; `exchange_source` takes any settings object that has `api_internal_url`, `service_token_file` and `service_bootstrap_file` (a `Protocol`, `CredentialSettings`).
  - The edge's lifespan: `wait_for_areas` until the API lists every area the edge needs (`EDGE_AREAS`, the union of the areas Tasks 5-9 add), then serves.

- [ ] **Step 1: Write the failing tests**

```python
def test_a_storefront_route_needs_the_storefront_credential(client: TestClient, render_token: str, storefront_token: str) -> None:
    url = "/api/v1/internal/crud/storefront/areas"
    assert client.get(url).status_code == 401
    assert client.get(url, headers=bearer(render_token)).status_code == 403
    r = client.get(url, headers=bearer(storefront_token))
    assert r.status_code == 200 and r.headers["X-ScadBuddy-Crud"] == "1"

def test_the_storefront_routes_are_not_in_the_schema(client: TestClient) -> None:
    assert not [p for p in client.get("/openapi.json").json()["paths"] if "/internal/crud/storefront" in p]

@pytest.mark.parametrize("token", ["", "short", "x" * 42 + "!", "x" * 44])
def test_a_malformed_visitor_token_is_400(client: TestClient, storefront_token: str, token: str) -> None:
    r = client.get("/api/v1/internal/crud/storefront/draft", headers={**bearer(storefront_token), "X-ScadBuddy-Visitor": token, "X-ScadBuddy-Store-Client": "203.0.113.1"})
    assert r.status_code == 400

def test_the_other_callers_keep_their_paths(client: TestClient, render_token: str) -> None:
    assert client.get("/api/v1/internal/crud/render/areas", headers=bearer(render_token)).status_code == 200

def test_the_storefront_service_account_maps_to_its_caller() -> None:
    assert caller_for_service_account("system:serviceaccount:bambuddy:scadbuddy-storefront", "bambuddy") == "storefront"
```

In `tests/storefront/test_edge.py`, `test_the_edge_starts_and_opens_no_database_connection` now also asserts that the request to `…/storefront/listings` carried `Authorization: Bearer sbsvc_…` and `X-ScadBuddy-Visitor` equal to the visitor id signed into the cookie the edge set.

- [ ] **Step 2: Run them to verify they fail** — `cd backend && uv run --frozen pytest tests/crud/test_storefront_routes.py tests/auth/test_exchange.py tests/storefront/test_edge.py -v` — FAIL.
- [ ] **Step 3: Implement** the Interfaces block.
- [ ] **Step 4: Run them, and the other callers' CRUD tests, to verify they pass** — same command plus `tests/crud tests/auth` — PASS.
- [ ] **Step 5: Lint, type-check, commit, and open PR 1**

```bash
cd backend && uv run --frozen ruff check . && uv run --frozen ruff format --check . && uv run --frozen mypy
git add backend
git commit -m "feat(storefront): the storefront service caller and its CRUD router"
```

### Task 3: The storefront settings

**Files:**
- Modify: `backend/scadbuddy/core/settings.py` (fields beside `rough_estimate_factor` at `:201`, the `live` table at `:923`, the env docs), `backend/scadbuddy/library/settings_store.py`
- Test: `backend/tests/test_settings_store.py`, `backend/tests/api/test_settings.py`

**Interfaces:**
- Produces: `Settings.storefront_renders_per_minute: int = Field(3, ge=1, le=60)`, `storefront_renders_per_day: int = Field(30, ge=1, le=10000)`, `storefront_render_concurrency: int = Field(2, ge=1, le=50)`, `storefront_draft_ttl_days: int = Field(30, ge=1, le=365)`, `storefront_requests_enabled: bool = False`; each `"live"`; each writable through the existing `PUT /settings`.

- [ ] **Step 1: Write the failing tests**

```python
def test_the_storefront_settings_have_their_defaults(settings_store: SettingsStore) -> None:
    s = settings_store.snapshot()
    assert (s.storefront_renders_per_minute, s.storefront_renders_per_day, s.storefront_render_concurrency,
            s.storefront_draft_ttl_days, s.storefront_requests_enabled) == (3, 30, 2, 30, False)

@pytest.mark.parametrize(("name", "value"), [("storefront_renders_per_minute", 0),
    ("storefront_render_concurrency", 0), ("storefront_draft_ttl_days", 0)])
def test_a_storefront_limit_below_one_is_refused(client: TestClient, name: str, value: int) -> None:
    assert client.put("/api/v1/settings", json={name: value}).status_code == 422
```

- [ ] **Step 2: Run to verify they fail** — `cd backend && uv run --frozen pytest tests/test_settings_store.py tests/api/test_settings.py -k storefront -v` — FAIL with `AttributeError`.
- [ ] **Step 3: Add the five fields.**
- [ ] **Step 4: Run to verify they pass** — same command — PASS.
- [ ] **Step 5: Commit** — `git commit -m "feat(storefront): the storefront's render, draft and request settings"`

### Task 4: Storefront drafts in the schema, the lifecycle and the back office

**Files:**
- Create: `backend/scadbuddy/migrations/<ts>_storefront_drafts.sql`, `backend/scadbuddy/orders/storefront/visitors.py`
- Modify: `backend/scadbuddy/orders/models.py` (`Actor` at `:115`, `Order` at `:229-230`, `OrderFilter` at `:290`), `backend/scadbuddy/orders/store.py`, `backend/scadbuddy/orders/lifecycle.py` (`accept_order` at `:810`, `record_manual_quote` at `:1924`), `backend/scadbuddy/orders/quote.py` (`quote_check`), `backend/scadbuddy/orders/printing.py` (`print_check`), `backend/scadbuddy/orders/costs.py`, `backend/scadbuddy/orders/report.py`, `backend/scadbuddy/api/orders.py`, `agent/src/tools/orders.ts` (`order_list`), `frontend/src/lib/ordersQuery.ts`, `frontend/src/pages/OrdersPage.tsx`, `frontend/src/components/orders/` (the row's number and customer cells), `frontend/src/mocks/features/orders/orders.ts`
- Test: `backend/tests/orders/test_storefront_drafts.py`, `backend/tests/api/test_orders_storefront.py`, `backend/tests/test_pg_migrations.py`, `agent/test/orders.test.ts`, `frontend/src/lib/ordersQuery.test.ts`, `frontend/src/pages/OrdersPage.test.tsx`

**Interfaces:**
- Consumes: `Lifecycle._lock`, `_event`, `_told`; `OrderStore.insert_order`.
- Produces:
  - Migration: `CREATE TABLE storefront_visitors (id text PRIMARY KEY DEFAULT gen_random_uuid()::text, token_sha256 text NOT NULL UNIQUE, created_at timestamptz NOT NULL DEFAULT now(), last_seen_at timestamptz NOT NULL DEFAULT now())`; `orders` gains `source text NOT NULL DEFAULT 'back_office' CHECK (source IN ('back_office','storefront'))`, `submitted_at timestamptz`, `storefront_visitor_id text REFERENCES storefront_visitors (id) ON DELETE SET NULL`; `orders.number` and `orders.customer_id` drop `NOT NULL`; `CHECK ((customer_id IS NOT NULL AND number IS NOT NULL) OR (source = 'storefront' AND submitted_at IS NULL))`; `CREATE UNIQUE INDEX orders_one_draft_per_visitor ON orders (storefront_visitor_id) WHERE submitted_at IS NULL AND source = 'storefront'`; `CREATE INDEX orders_source ON orders (source, submitted_at)`.
  - `OrderSource = Literal["back_office", "storefront"]`; `Actor.kind: Literal["browser", "agent", "visitor"]`.
  - `Order.number: str | None`, `Order.customer_id: str | None`, `Order.source: OrderSource = "back_office"`, `Order.submitted_at: datetime | None = None`, `Order.storefront_visitor_id: str | None = None`.
  - `OrderFilter.source: OrderSource | None = None`, `OrderFilter.unsubmitted: bool = False` (False hides `source = 'storefront' AND submitted_at IS NULL`; True lists only those).
  - `OrderStore.insert_storefront_draft(conn: Conn, *, visitor_id: str, currency: str, created_by: Actor) -> Order`; `OrderStore.draft_of(visitor_id: str, *, conn: Conn | None = None) -> Order | None`.
  - `Lifecycle.ensure_draft(visitor_id: str, by: Actor, *, currency: str) -> Order`: the visitor's draft, made if none (`order.created` audit row with `source: storefront`); a unique violation reads the winner.
  - `LifecycleError("order_not_submitted", …)` from `accept_order`, `record_manual_quote`, `quote_check` and `print_check` on an unsubmitted storefront draft.
  - `@dataclass(frozen=True) class Visitor`: `id: str`, `created_at: datetime`, `last_seen_at: datetime`.
  - `VisitorStore(pool)` in `orders/storefront/visitors.py`: `ensure(visitor_id: str) -> Visitor` (by `sha256(visitor_id)` in `token_sha256`, insert or touch `last_seen_at`), `find(visitor_id: str) -> Visitor | None` (touches `last_seen_at` at most once a minute); `visitor_actor(visitor: Visitor) -> Actor` (`Actor(kind="visitor", principal=f"storefront_visitor:{visitor.id}")`).
  - `GET /api/v1/orders?source=…&unsubmitted=true`; `OrderRow.source`, `OrderRow.submitted_at`; readers answer `number` and `customer` null for a draft. The orders page gets a "Source" filter (`source=` in the URL) and an "Unsubmitted carts" toggle (`unsubmitted=1`); a draft row shows "Cart" for its number and "Visitor" for its customer. The agent's `order_list` passes `source` (`z.enum(['back_office','storefront']).optional()`) and `unsubmitted` (`z.boolean().optional()`).

- [ ] **Step 1: Write the failing tests**

```python
def test_a_draft_needs_no_customer_and_no_number(lifecycle: Lifecycle, visitor: Visitor) -> None:
    order = lifecycle.ensure_draft(visitor.id, visitor_actor(visitor), currency="USD")
    assert (order.source, order.customer_id, order.number, order.submitted_at) == ("storefront", None, None, None)
    assert lifecycle.ensure_draft(visitor.id, visitor_actor(visitor), currency="USD").id == order.id
    assert order_counter(lifecycle) == counter_before

def test_a_back_office_order_still_needs_its_customer_and_number(owner_conn: Connection) -> None:
    with pytest.raises(psycopg.errors.CheckViolation):
        owner_conn.execute("INSERT INTO orders (currency, created_by) VALUES ('USD', '{}')")

def test_the_visitor_is_found_by_its_token_hash_only(visitors: VisitorStore, owner_conn: Connection) -> None:
    v = visitors.ensure("a" * 43)
    assert visitors.find("a" * 43) == v and visitors.find("b" * 43) is None
    assert owner_conn.execute("SELECT token_sha256 FROM storefront_visitors").fetchone()["token_sha256"] == sha256_hex("a" * 43)

@pytest.mark.parametrize("act", ["accept", "manual_quote"])
def test_the_shop_cannot_act_on_an_unsubmitted_draft(lifecycle: Lifecycle, draft_with_item: Order, act: str) -> None:
    with pytest.raises(LifecycleError) as refused:
        run(lifecycle, act, draft_with_item)
    assert refused.value.slug == "order_not_submitted"

def test_reading_a_draft_never_fails(client: TestClient, draft_with_item: str) -> None:
    rows = client.get("/api/v1/orders", params={"source": "storefront", "unsubmitted": "true"}).json()["items"]
    assert [(r["id"], r["number"], r["customer"]) for r in rows] == [(draft_with_item, None, None)]
    for path in (f"/api/v1/orders/{draft_with_item}", f"/api/v1/orders/{draft_with_item}/costs", "/api/v1/orders/reports/profit"):
        assert client.get(path).status_code == 200
    assert draft_with_item not in [r["id"] for r in client.get("/api/v1/orders").json()["items"]]
```

```ts
it('keeps source and unsubmitted in the URL', () => {
  expect(parseOrdersQuery('?source=storefront&unsubmitted=1')).toMatchObject({ source: 'storefront', unsubmitted: true })
  expect(ordersQueryString({ source: 'back_office', unsubmitted: false })).toBe('source=back_office')
})
it('shows a draft as a cart with no customer', async () => {
  expect(await screen.findByText('Cart')).toBeInTheDocument()
  expect(screen.getByText('Visitor')).toBeInTheDocument()
})
```

In `tests/test_pg_migrations.py`: an existing order migrates to `source = 'back_office'` with `submitted_at` null and passes the CHECK.

- [ ] **Step 2: Run to verify they fail** — `cd backend && uv run --frozen pytest tests/orders/test_storefront_drafts.py tests/api/test_orders_storefront.py tests/test_pg_migrations.py -v`; `cd frontend && pnpm exec vitest run src/lib/ordersQuery.test.ts src/pages/OrdersPage.test.tsx` — FAIL.
- [ ] **Step 3: Write the migration and implement** the Interfaces block. `OrderStore.orders` applies `unsubmitted` before any other filter.
- [ ] **Step 4: Run to verify they pass**, plus `tests/orders` and `cd agent && pnpm test -- orders` — PASS.
- [ ] **Step 5: Commit, then open PR 2** — `git commit -m "feat(storefront): storefront draft orders without a customer or number"`

### Task 5: Listings, publish, and the back office's Storefront page

**Files:**
- Create: `backend/scadbuddy/migrations/<ts>_storefront_listings.sql`, `backend/scadbuddy/orders/storefront/listings.py`, `backend/scadbuddy/orders/storefront/wire.py` (the listing shapes), `backend/scadbuddy/api/storefront.py`, `frontend/src/pages/StorefrontPage.tsx`, `frontend/src/components/storefront/ListingEditor.tsx`, `frontend/src/storefront/components/ListingView.tsx` (props only, no fetch), `frontend/src/mocks/features/storefront.ts`, `frontend/e2e/storefront-admin.spec.ts`
- Modify: `backend/scadbuddy/main.py` (`_api_router`), `agent/src/tools/coverage.ts`, `frontend/src/App.tsx` (route `/storefront`), `frontend/src/components/AppShell.tsx`, `frontend/src/api/client.ts`
- Test: `backend/tests/orders/storefront/test_listings.py`, `backend/tests/api/test_storefront_listings.py`, `frontend/src/pages/StorefrontPage.test.tsx`, `frontend/src/components/storefront/ListingEditor.test.tsx`

**Interfaces:**
- Consumes: `orders/operations.py` `reference_schema(core, reference)`; `render/inputs.py` (normalisation); `store/snapshots.py` `SnapshotStore.ensure(slug, revision) -> str`; `store/refs.py` `BlobRefs.add`/`remove`; `library/media_store.py`; `orders/pricebook.py`.
- Produces:
  - Migration: `storefront_listings (id text PK DEFAULT gen_random_uuid()::text, slug text NOT NULL, commit text NOT NULL CHECK (commit ~ '^[0-9a-f]{40}$'), title text NOT NULL CHECK (char_length(title) BETWEEN 1 AND 200), description text NOT NULL DEFAULT '' CHECK (char_length(description) <= 5000), position integer NOT NULL, published boolean NOT NULL DEFAULT false, params jsonb NOT NULL DEFAULT '{}', fixed jsonb NOT NULL DEFAULT '{}', colours text[] NOT NULL DEFAULT '{}', media_ids text[] NOT NULL DEFAULT '{}', snapshot jsonb, media jsonb NOT NULL DEFAULT '[]', published_at timestamptz, created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(), CHECK (NOT published OR snapshot IS NOT NULL))`; index `(published, position)`.
  - `ParamBound = NumberBound | ChoiceBound | TextBound` (discriminated by `kind`): `NumberBound(kind="number", min: float, max: float, step: float | None)`, `ChoiceBound(kind="choice", choices: list[ParamValue])`, `TextBound(kind="text", max_length: int = Field(le=200))`.
  - `ListingSnapshot`: `schema_: CustomizerSchema` (alias `schema`), `pipeline: bool`, `template_name: str`, `libraries: list[ModelLibrary]`, `snapshot_key: str`. `ListingMedia`: `blob_key`, `kind: Literal["image", "video"]`, `media_type`, `caption`.
  - `Listing` (every column); `ListingInput` (`slug`, `commit`, `title`, `description`, `params: dict[str, ParamBound]`, `fixed: dict[str, ParamValue]`, `colours: list[str]`, `media_ids: list[str]`).
  - `ListingStore(pool)`: `create(body) -> Listing`, `update(listing_id, body, *, expected_updated_at: datetime) -> Listing` (409 `listing_changed` on a mismatch; editing a published listing unpublishes it), `get(listing_id) -> Listing | None`, `published() -> list[Listing]`, `all() -> list[Listing]`, `reorder(ids: list[str]) -> list[Listing]`, `delete(listing_id) -> None` (409 `listing_published` unless unpublished).
  - `async def publish(core: Core, store: ListingStore, listing_id: str) -> Listing`: refuses 422 `listing_param_unknown`, `listing_bounds_outside_schema`, `listing_fixed_invalid`, `listing_colour_unknown`, `listing_media_unknown`; fixes unnamed parameters at their defaults; holds the snapshot and each media blob under `blob_refs ("storefront_listing", listing_id)`; sets `snapshot`, `media`, `published`, `published_at`. `async def unpublish(store, refs, listing_id) -> Listing` drops the holder.
  - Back-office routes under `/api/v1/storefront`: `GET /listings`, `POST /listings` (201), `PUT /listings/{id}` (`{listing, updated_at}`), `POST /listings/{id}/publish`, `POST /listings/{id}/unpublish`, `PUT /listings/order` (`{ids}`), `DELETE /listings/{id}` (204). Each gets a `NOT_A_TOOL` entry with the reason in call 18.
  - Frontend: `ListingView(props: { listing: PublicListing; values: ParamValues; onChange?: (v: ParamValues) => void; job?: PreviewJob; estimate?: EstimateView | null })`; `ListingEditor` (model and commit, each parameter "Locked at …" or "Customer may change" with bounds from the schema, filaments, media, title, description); the page: drag order, Publish/Unpublish, an "Ask for a design" toggle bound to `storefront_requests_enabled`, and a live preview (`ListingView` with the unsaved listing).

- [ ] **Step 1: Write the failing tests** (`demo` template: `width` 10..100, `colour` a `// color` parameter)

```python
async def test_publish_snapshots_the_schema_and_holds_the_source(core: Core, store: ListingStore, refs: BlobRefs) -> None:
    listing = store.create(ListingInput(slug="demo", commit=HEAD, title="Demo", params={"width": NumberBound(kind="number", min=20, max=80, step=1)}, colours=[PLA_RED]))
    published = await publish(core, store, listing.id)
    assert published.published and published.snapshot.template_name == "Demo" and published.snapshot.pipeline is False
    assert published.fixed["height"] == SCHEMA_DEFAULTS["height"]
    assert refs.holders(published.snapshot.snapshot_key) == [("storefront_listing", listing.id)]

async def test_a_bound_wider_than_the_schema_is_refused(core: Core, store: ListingStore) -> None:
    listing = store.create(body(params={"width": NumberBound(kind="number", min=0, max=500, step=None)}))
    with pytest.raises(ApiError) as refused:
        await publish(core, store, listing.id)
    assert refused.value.type.endswith("listing_bounds_outside_schema")

async def test_the_held_snapshot_survives_the_blob_sweep(core: Core, store: ListingStore, state: AppState) -> None:
    published = await publish(core, store, store.create(body()).id)
    await sweep_blobs_past_grace(state)
    assert await state.store.content.exists(published.snapshot.snapshot_key)

def test_editing_a_published_listing_unpublishes_it(store: ListingStore, published: Listing) -> None:
    assert store.update(published.id, body(title="New"), expected_updated_at=published.updated_at).published is False
    with pytest.raises(ApiError, match="listing_changed"):
        store.update(published.id, body(), expected_updated_at=published.updated_at)

def test_the_listing_routes_round_trip(client: TestClient) -> None:
    made = client.post("/api/v1/storefront/listings", json=BODY).json()
    assert client.post(f"/api/v1/storefront/listings/{made['id']}/publish").json()["published"] is True
    assert client.delete(f"/api/v1/storefront/listings/{made['id']}").status_code == 409
```

```ts
it('locks a parameter and previews the customer view', async () => {
  render(<StorefrontPage />)
  await user.click(await screen.findByRole('button', { name: 'New listing' }))
  await user.click(screen.getByRole('radio', { name: 'Locked at 50' }))
  expect(within(screen.getByTestId('listing-preview')).queryByLabelText('width')).toBeNull()
  await user.click(screen.getByRole('button', { name: 'Publish' }))
  expect(await screen.findByText('Published')).toBeInTheDocument()
})
```

`e2e/storefront-admin.spec.ts`: create, publish and reorder two listings on the mocked API; the order survives a reload.

- [ ] **Step 2: Run to verify they fail** — `cd backend && uv run --frozen pytest tests/orders/storefront/test_listings.py tests/api/test_storefront_listings.py -v`; `cd frontend && pnpm gen:api && pnpm exec vitest run src/pages/StorefrontPage.test.tsx src/components/storefront` — FAIL.
- [ ] **Step 3: Write the migration and implement** the Interfaces block.
- [ ] **Step 4: Run to verify they pass**, plus `cd agent && pnpm test -- coverage` and `cd frontend && pnpm exec playwright test e2e/storefront-admin.spec.ts` — PASS.
- [ ] **Step 5: Commit, then open PR 3** — `git commit -m "feat(storefront): listings, publish and the back office's Storefront page"`

### Task 6: Public listings, the visitor cookie and the edge's limits

**Files:**
- Create: `backend/scadbuddy/orders/storefront/inputs.py`, `backend/scadbuddy/storefront/visitors.py`, `backend/scadbuddy/storefront/ratelimit.py`
- Modify: `backend/scadbuddy/orders/storefront/wire.py` (`PublicListing`, `PublicListings`), `backend/scadbuddy/api/crud_storefront.py` (area `listings`), `backend/scadbuddy/crud/storefront.py`, `backend/scadbuddy/storefront/app.py` (middleware: `VisitorCookie`, `RequestLimit`, `CrossSiteGate`), `backend/scadbuddy/storefront/routes.py`
- Test: `backend/tests/orders/storefront/test_inputs.py`, `backend/tests/crud/test_storefront_routes.py`, `backend/tests/storefront/test_edge_routes.py`

**Interfaces:**
- Consumes: `core/proxies.py` `client_address(peer, forwarded_for, trusted)`; `api/cross_site.py` `CrossSiteGate(app, *, public_url, allowed_origins)`; `store/s3.py` `presign_get(backend_id, expires)`; Task 5's `Listing`, `ParamBound`.
- Produces (CRUD side):
  - `class InputOutsideListingError(ValueError)` with `name: str`, `reason: Literal["unknown", "locked", "range", "step", "choice", "length", "colour"]`.
  - `listing_values(listing: Listing, asked: Mapping[str, ParamValue], filaments: Mapping[str, Filament]) -> dict[str, ParamValue]`: `fixed` ∪ the asked values of `params`, each checked against its bound; a `// color` value must be the colour of one of `listing.colours`.
  - `listing_reference(listing: Listing, values: Mapping[str, ParamValue], chosen: Mapping[int, str]) -> ResolvedReference`: from the listing's snapshot, no git.
  - `PublicListing`: `id`, `title`, `description`, `params` (bounds with each parameter's schema label, help and group), `colours: list[PublicFilament]` (`id`, `colour`, `colour_name`, `material`), `media: list[{url, kind, caption}]` (presigned, 1 h), `updated_at`. `PublicListings`: `listings: list[PublicListing]`, `requests_enabled: bool`.
  - CRUD routes: `GET …/storefront/listings -> PublicListings` (published only, 409 `storefront_needs_s3` unless `s3`); `GET …/storefront/listings/{id} -> PublicListing` (404 for an unpublished or unknown one). `CRUD_AREAS["storefront"]` gains `"listings"`.
- Produces (edge):
  - `VISITOR_COOKIE: Final = "sb_visitor"`, `VISITOR_MAX_AGE: Final = 400 * 24 * 3600`; `class VisitorCookie` (ASGI): a request without a cookie whose mac verifies gets a new visitor id (`secrets.token_urlsafe(32)`), signed (`sign(visitor_id: str, key: bytes) -> str`, `verify(cookie: str, key: bytes) -> str | None`, `hmac.compare_digest`) and set on the response; the verified id is `request.state.visitor_id`.
  - `class RequestLimit` (ASGI): per client, a 60 s sliding window, `READ_LIMIT = 120`, `WRITE_LIMIT = 30`; over is 429 with `Retry-After` and type `…/storefront/rate_limited`.
  - Routes `GET /api/store/v1/listings`, `GET /api/store/v1/listings/{id}`: forward to the CRUD client with the verified visitor id and `client_address(...)`, and answer its body; a CRUD refusal's status, problem and `Retry-After` pass through; `CrudUnavailableError` is 503.

- [ ] **Step 1: Write the failing tests**

```python
@pytest.mark.parametrize(("asked", "reason"), [({"nope": 1}, "unknown"), ({"height": 9}, "locked"),
    ({"width": 81}, "range"), ({"width": 20.5}, "step"), ({"colour": "#00ff00"}, "colour")])
def test_inputs_outside_the_listing_are_refused(listing: Listing, asked: dict, reason: str) -> None:
    with pytest.raises(InputOutsideListingError) as refused:
        listing_values(listing, asked, FILAMENTS)
    assert refused.value.reason == reason

async def test_the_reference_matches_the_back_offices(core: Core, listing: Listing) -> None:
    values = listing_values(listing, {"width": 40}, FILAMENTS)
    ours = listing_reference(listing, values, {0: PLA_RED})
    theirs = await resolve_reference(core.catalogue, core.history, core.outputs, paths=core.paths,
        config=core.config, fetcher=None, slug=listing.slug, commit=listing.commit, inputs=values, output_id=None)
    assert ours.reference == theirs.reference

def test_the_crud_side_lists_only_published_listings(crud: CrudCaller, published: Listing, unpublished: Listing) -> None:
    body = crud.get("/storefront/listings", token=TOKEN_A).json()
    assert [l["id"] for l in body["listings"]] == [published.id]
    assert crud.get(f"/storefront/listings/{unpublished.id}", token=TOKEN_A).status_code == 404

def test_the_crud_side_refuses_a_store_that_is_not_s3(crud: CrudCaller, store_backend_local: None) -> None:
    assert crud.get("/storefront/listings", token=TOKEN_A).json()["type"].endswith("storefront_needs_s3")

def test_the_first_request_sets_a_visitor_cookie(edge: TestClient) -> None:
    cookie = edge.get("/api/store/v1/listings").headers["set-cookie"]
    assert cookie.startswith("sb_visitor=") and "HttpOnly" in cookie and "Secure" in cookie and "samesite=lax" in cookie.lower()
    assert "set-cookie" not in edge.get("/api/store/v1/listings").headers

def test_a_forged_cookie_gets_a_new_visitor(edge: TestClient, crud_mock: respx.Router) -> None:
    edge.cookies.set("sb_visitor", "v1." + "a" * 43 + ".forged")
    r = edge.get("/api/store/v1/listings")
    assert "sb_visitor=v1." in r.headers["set-cookie"]
    assert crud_mock.calls.last.request.headers["X-ScadBuddy-Visitor"] != "a" * 43

def test_a_write_from_another_origin_is_refused_at_the_edge(edge: TestClient, crud_mock: respx.Router) -> None:
    assert edge.post("/api/store/v1/renders", json={}, headers={"Origin": "https://evil.example"}).status_code == 403
    assert not [c for c in crud_mock.calls if "/renders" in str(c.request.url)]

def test_the_request_limit_answers_429(edge: TestClient) -> None:
    codes = [edge.get("/api/store/v1/listings").status_code for _ in range(121)]
    assert codes[:120] == [200] * 120 and codes[-1] == 429
```

`crud` is a fixture: a `TestClient` of the API that sends a `storefront` credential, `X-ScadBuddy-Store-Client: 203.0.113.1` and the given `token` (a visitor id) as `X-ScadBuddy-Visitor`, with paths under `/api/v1/internal`. `test_the_reference_matches_the_back_offices` runs in the API's test environment (it needs git).

- [ ] **Step 2: Run to verify they fail** — `cd backend && uv run --frozen pytest tests/orders/storefront/test_inputs.py tests/crud/test_storefront_routes.py tests/storefront/test_edge_routes.py -v` — FAIL.
- [ ] **Step 3: Implement** the Interfaces block.
- [ ] **Step 4: Run to verify they pass**, plus `tests/storefront/test_edge.py` — PASS.
- [ ] **Step 5: Commit** — `git commit -m "feat(storefront): public listings, visitor cookies and request limits"`

### Task 7: Customize: renders, render limits, the preview and the estimate

**Files:**
- Create: `backend/scadbuddy/migrations/<ts>_storefront_renders.sql`, `backend/scadbuddy/orders/storefront/limits.py`, `backend/scadbuddy/orders/storefront/renders.py`, `backend/scadbuddy/orders/estimate.py`
- Modify: `backend/scadbuddy/orders/pricing.py` (`price` at `:95`), `backend/scadbuddy/api/orders.py` (`estimate` at `:819-882` calls `orders/estimate.py`), `backend/scadbuddy/orders/storefront/wire.py` (`RenderBody`, `StoreRender`, `StoreEstimate`), `backend/scadbuddy/api/crud_storefront.py` (area `renders`), `backend/scadbuddy/crud/storefront.py`, `backend/scadbuddy/storefront/routes.py`
- Test: `backend/tests/orders/storefront/test_limits.py`, `backend/tests/orders/storefront/test_renders.py`, `backend/tests/orders/test_estimate.py`, `backend/tests/api/test_orders_estimate.py` (unchanged answers), `backend/tests/storefront/test_edge_routes.py`

**Interfaces:**
- Consumes: `render/submit.py` `RenderService.submit(slug, params, *, model_version, inputs, whole_inputs, reuse_finished=True) -> Job`; `render/projection.py` `JobProjection.reuse_done(key)`, `.read(job_id)`; `render/job_models.py` `render_key`, `inputs_key`, `QueueFullError`; `store/cache.py` `materialize_result`; `render/geometry.py` `analyze_3mf(path, *, warnings, plate)`; `orders/pricing.py` `estimate_grams`, `price`; `crud/idempotency.py` `idempotent_write(conn, caller, key, route, body, write)`; Task 6's `listing_values`, `listing_reference`; Task 4's `VisitorStore`.
- Produces:
  - Migration: `storefront_renders (id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, visitor_id text NOT NULL REFERENCES storefront_visitors (id) ON DELETE CASCADE, client text NOT NULL, listing_id text NOT NULL, render_key text NOT NULL, job_id text, counted boolean NOT NULL, created_at timestamptz NOT NULL DEFAULT clock_timestamp())`, indexes `(visitor_id, created_at)`, `(client, created_at)`, `(counted, created_at)`, `(job_id)`; `storefront_job_parts (job_id text PRIMARY KEY, parts jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now())`.
  - `price(plates, filaments, policy, *, currency, quantity, estimated: bool = False) -> Priced` (call 4).
  - `orders/estimate.py`: `EstimatedPart`, `OrderEstimate` move here (re-exported from `api/orders.py`); `part_volumes(model_3mf: Path, plates: int, warnings: Sequence[str]) -> list[EstimatedPart]`; `rough_estimate(parts: Sequence[EstimatedPart], filament_for: Callable[[str], Filament], policy: PricingPolicy, *, currency: str, factor: str, quantity: int = 1) -> StoreEstimate` (`StoreEstimate(OrderEstimate)` adds `price_minor: Money` and `lines: list[QuoteLine]`). `GET /api/v1/orders/estimate` answers as before.
  - `class RenderLimited(Exception)`: `retry_after: int`, `limit: Literal["per_minute", "per_day", "concurrency"]`.
  - `RenderLimits(pool)`: `admit(conn: Conn, visitor_id: str, client: str, *, listing_id: str, render_key: str, settings: Settings, now: datetime | None = None) -> int` (under `pg_advisory_xact_lock` on the visitor and on the client: counted rows of the visitor or of the client in the last 60 s against `storefront_renders_per_minute` and the last 24 h against `storefront_renders_per_day`; counted rows of the last `render_timeout` whose `job_id` is null or whose job is not `done`, `failed` or `cancelled` against `storefront_render_concurrency`; raises `RenderLimited` with the seconds until the oldest counted row leaves its window, or 5 for concurrency; else inserts a counted row and returns its id), `free(conn, visitor_id, client, *, listing_id, render_key) -> int`, `attach(row_id: int, job_id: str) -> None`, `owns(visitor_id: str, job_id: str) -> bool`.
  - `async def start_render(state: AppState, visitor_id: str, client: str, key: str, body: RenderBody) -> StoreRender`: the visitor (`VisitorStore.ensure`); the listing (published, `listing_updated_at` equal, else 409 `listing_changed`); `listing_values` (422 `input_outside_listing` with `name` and `reason`); the key; a done or open job of the key is `free`, else `admit`, both inside `idempotent_write` keyed by `key` (call 21); `RenderService.submit(..., reuse_finished=True)`; `attach`. `QueueFullError` is 429 with its `retry_after`.
  - `RenderBody`: `listing_id`, `listing_updated_at`, `values: dict[str, ParamValue]`, `colours: dict[int, str]`. `StoreRender`: `id`, `status`, `stage`, `plates`, `bbox_mm`, `colors`, `preview_url` (`/api/store/v1/renders/{id}/preview.glb`, the edge's path), `error` (a fixed text, never the log tail); the field names are the subset of the back office's `Job` that `Preview.tsx` reads.
  - CRUD routes (area `renders`): `POST …/storefront/renders` (`Idempotency-Key` required, 428 without), `GET …/storefront/renders/{job_id}` (404 unless `owns`), `GET …/storefront/renders/{job_id}/preview.glb?plate=` (404 unless `owns`; streams from the cached piece), `GET …/storefront/renders/{job_id}/estimate -> StoreEstimate` (404 unless `owns`; 422 `estimate_unavailable` for a job not `done`, a 3MF that cannot be analysed, or past `render_timeout`; written once to `storefront_job_parts`).
  - Edge routes: `POST /api/store/v1/renders` (sends `Idempotency-Key: <uuid4>`), `GET /api/store/v1/renders/{job_id}`, `GET /api/store/v1/renders/{job_id}/preview.glb` (streamed, `Content-Type: model/gltf-binary`), `GET /api/store/v1/renders/{job_id}/estimate`.

- [ ] **Step 1: Write the failing tests**

```python
def test_the_fourth_render_in_a_minute_is_limited(limits: RenderLimits, conn: Conn, visitor: str) -> None:
    for n in range(3):
        limits.admit(conn, visitor, "203.0.113.1", listing_id="l", render_key=f"k{n}", settings=SETTINGS, now=T0)
    with pytest.raises(RenderLimited) as refused:
        limits.admit(conn, visitor, "203.0.113.1", listing_id="l", render_key="k3", settings=SETTINGS, now=T0 + timedelta(seconds=10))
    assert (refused.value.limit, refused.value.retry_after) == ("per_minute", 50)

def test_a_new_cookie_on_the_same_client_is_still_limited(limits: RenderLimits, conn: Conn, visitors: list[str]) -> None:
    for n, v in enumerate(visitors[:3]):
        limits.admit(conn, v, "203.0.113.1", listing_id="l", render_key=f"k{n}", settings=SETTINGS, now=T0)
    with pytest.raises(RenderLimited):
        limits.admit(conn, visitors[3], "203.0.113.1", listing_id="l", render_key="k9", settings=SETTINGS, now=T0)

def test_concurrency_counts_every_visitor(limits: RenderLimits, conn: Conn, visitors: list[str]) -> None:
    limits.admit(conn, visitors[0], "198.51.100.1", listing_id="l", render_key="a", settings=SETTINGS, now=T0)
    limits.admit(conn, visitors[1], "198.51.100.2", listing_id="l", render_key="b", settings=SETTINGS, now=T0)
    with pytest.raises(RenderLimited) as refused:
        limits.admit(conn, visitors[2], "198.51.100.3", listing_id="l", render_key="c", settings=SETTINGS, now=T0)
    assert (refused.value.limit, refused.value.retry_after) == ("concurrency", 5)

def test_two_visitors_with_the_same_inputs_share_one_render(crud: CrudCaller) -> None:
    ja = render_until_done(crud, TOKEN_A, LISTING, {"width": 40})
    jb = a_render(crud, TOKEN_B, LISTING, {"width": 40})
    assert jb["id"] == ja["id"] and counted_rows(TOKEN_B) == 0

def test_joining_an_open_render_is_not_counted(crud: CrudCaller) -> None:
    ja = a_render(crud, TOKEN_A, LISTING, {"width": 41})
    jb = a_render(crud, TOKEN_B, LISTING, {"width": 41})
    assert jb["id"] == ja["id"] and counted_rows(TOKEN_B) == 0
    assert crud.get(f"/storefront/renders/{jb['id']}", token=TOKEN_B).status_code == 200

def test_a_retried_render_is_counted_once(crud: CrudCaller) -> None:
    first = a_render(crud, TOKEN_A, LISTING, {"width": 42}, key="k-1")
    again = a_render(crud, TOKEN_A, LISTING, {"width": 42}, key="k-1")
    assert again["id"] == first["id"] and counted_rows(TOKEN_A) == 1

def test_over_the_limit_is_429_with_retry_after(crud: CrudCaller) -> None:
    for w in (30, 31, 32):
        a_render(crud, TOKEN_A, LISTING, {"width": w})
    r = crud.post("/storefront/renders", token=TOKEN_A, json=render_body(LISTING, {"width": 33}), key="k-4")
    assert r.status_code == 429 and int(r.headers["retry-after"]) >= 1
    assert r.json()["type"] == "https://scadbuddy.dev/problems/storefront/render_limited"

def test_another_visitors_render_is_404(crud: CrudCaller) -> None:
    job = a_render(crud, TOKEN_A, LISTING, {"width": 50})
    for path in ("", "/preview.glb", "/estimate"):
        assert crud.get(f"/storefront/renders/{job['id']}{path}", token=TOKEN_B).status_code == 404

def test_the_estimate_is_a_price_without_machine_time(crud: CrudCaller) -> None:
    job = render_until_done(crud, TOKEN_A, LISTING, {"width": 40})
    est = crud.get(f"/storefront/renders/{job['id']}/estimate", token=TOKEN_A).json()
    assert est["estimate"] is True and est["price_minor"] > est["filament_minor"] > 0
    assert "machine" not in [line["kind"] for line in est["lines"]]

def test_a_3mf_that_cannot_be_analysed_is_422(crud: CrudCaller, corrupt_done_job: str) -> None:
    r = crud.get(f"/storefront/renders/{corrupt_done_job}/estimate", token=TOKEN_A)
    assert r.status_code == 422 and r.json()["type"].endswith("estimate_unavailable")

def test_the_edge_passes_a_429_through(edge: TestClient, crud_mock: respx.Router) -> None:
    crud_mock.post(url__regex=r".*/storefront/renders$").respond(429, headers={"Retry-After": "42", "X-ScadBuddy-Crud": "1"}, json=LIMITED)
    r = edge.post("/api/store/v1/renders", json=BODY, headers={"Origin": PUBLIC_URL})
    assert (r.status_code, r.headers["retry-after"], r.json()["type"]) == (429, "42", LIMITED["type"])
```

In `tests/orders/test_estimate.py`: `price(..., estimated=True)` on a plate with 0 seconds has lines `["filament", "setup", "labour", "markup"]` for a policy with all four set, and `price(...)` without it still raises `PricingError`.

- [ ] **Step 2: Run to verify they fail** — `cd backend && uv run --frozen pytest tests/orders/storefront/test_limits.py tests/orders/storefront/test_renders.py tests/orders/test_estimate.py tests/storefront/test_edge_routes.py -v` — FAIL.
- [ ] **Step 3: Write the migration and implement** the Interfaces block. The analysis runs in a thread under `asyncio.wait_for(..., config.render_timeout)`.
- [ ] **Step 4: Run to verify they pass**, plus `tests/api/test_orders_estimate.py tests/crud` — PASS.
- [ ] **Step 5: Commit, then open PR 4** — `git commit -m "feat(storefront): customize within a listing, render limits and the rough price"`

### Task 8: Add a listing to the draft

**Files:**
- Create: `backend/scadbuddy/orders/storefront/drafts.py`
- Modify: `backend/scadbuddy/orders/storefront/wire.py` (`AddListingItem`, `DraftView`), `backend/scadbuddy/api/crud_storefront.py` (area `drafts`), `backend/scadbuddy/crud/storefront.py`, `backend/scadbuddy/storefront/routes.py`
- Test: `backend/tests/orders/storefront/test_drafts.py`

**Interfaces:**
- Consumes: Task 4's `Lifecycle.ensure_draft`, `VisitorStore`, `visitor_actor`; `Lifecycle.add_item(order_id, by, resolved, *, output_id=None, quantity, title, notes)`, `delete_item(order_id, item_id, by)`; Task 6's `listing_values`, `listing_reference`; `crud/idempotency.py` `idempotent_write`.
- Produces:
  - `AddListingItem`: `listing_id`, `listing_updated_at`, `values`, `colours: dict[int, str]`, `quantity: int = Field(1, ge=1, le=100)`.
  - `DraftView`: `id`, `currency`, `minor_exponent`, `items: list[DraftItem]` (`id`, `kind`, `title`, `quantity`, `inputs` for a model item, `notes` for a request, `attachments: list[{filename, kind, url}]` with presigned URLs).
  - `add_listing_item(state: AppState, visitor_id: str, key: str, body: AddListingItem) -> DraftView`: revision 1 from `listing_reference`, `title` the listing's; 409 `listing_changed`; 422 `input_outside_listing`.
  - `get_draft(state, visitor_id) -> DraftView | None` (`None` is 200 with `null`; an unknown visitor id creates no row); `remove_item(state, visitor_id, item_id) -> DraftView`: 404 unless the item is on the draft that visitor owns (checked under the order's lock).
  - CRUD routes (area `drafts`): `GET …/storefront/draft`, `POST …/storefront/draft/items` (`Idempotency-Key` required), `DELETE …/storefront/draft/items/{item_id}` (`Idempotency-Key` required). Edge routes: `GET /api/store/v1/draft`, `POST /api/store/v1/draft/items`, `DELETE /api/store/v1/draft/items/{item_id}`.

- [ ] **Step 1: Write the failing tests**

```python
def test_add_to_order_makes_revision_one(crud: CrudCaller, owner_lifecycle: Lifecycle) -> None:
    draft = crud.post("/storefront/draft/items", token=TOKEN_A, json=add_body(LISTING, {"width": 40}), key="a1").json()
    item = owner_lifecycle.store.items(draft["id"])[0]
    assert (item.kind, item.slug, item.commit) == ("model", "demo", LISTING.commit)
    assert [r.number for r in owner_lifecycle.store.revisions(item.id)] == [1]
    assert owner_lifecycle.store.order(draft["id"]).number is None

def test_two_first_adds_at_once_share_one_draft(crud: CrudCaller) -> None:
    with ThreadPoolExecutor(2) as pool:
        a, b = pool.map(lambda w: crud.post("/storefront/draft/items", token=TOKEN_A, json=add_body(LISTING, {"width": w}), key=f"k{w}").json(), (40, 41))
    assert a["id"] == b["id"]
    assert len(crud.get("/storefront/draft", token=TOKEN_A).json()["items"]) == 2

def test_a_retried_add_makes_one_item(crud: CrudCaller) -> None:
    for _ in range(2):
        crud.post("/storefront/draft/items", token=TOKEN_A, json=add_body(LISTING, {"width": 40}), key="same")
    assert len(crud.get("/storefront/draft", token=TOKEN_A).json()["items"]) == 1

def test_a_stale_listing_is_409_and_the_item_keeps_its_revision(crud: CrudCaller, republish: Callable[[], Listing]) -> None:
    crud.post("/storefront/draft/items", token=TOKEN_A, json=add_body(LISTING, {"width": 40}), key="b1")
    republish()
    r = crud.post("/storefront/draft/items", token=TOKEN_A, json=add_body(LISTING, {"width": 41}), key="b2")
    assert r.status_code == 409 and r.json()["type"].endswith("listing_changed")
    assert crud.get("/storefront/draft", token=TOKEN_A).json()["items"][0]["inputs"]["width"] == 40

def test_another_visitor_cannot_see_or_remove_my_item(crud: CrudCaller) -> None:
    item_id = crud.post("/storefront/draft/items", token=TOKEN_A, json=add_body(LISTING, {"width": 40}), key="c1").json()["items"][0]["id"]
    assert crud.get("/storefront/draft", token=TOKEN_B).json() is None
    assert crud.delete(f"/storefront/draft/items/{item_id}", token=TOKEN_B, key="c2").status_code == 404
```

- [ ] **Step 2: Run to verify they fail** — `cd backend && uv run --frozen pytest tests/orders/storefront/test_drafts.py -v` — FAIL.
- [ ] **Step 3: Implement** the Interfaces block.
- [ ] **Step 4: Run to verify they pass** — same command plus `tests/storefront/test_edge_routes.py` — PASS.
- [ ] **Step 5: Commit** — `git commit -m "feat(storefront): add a listing to the visitor's draft"`

### Task 9: Design requests with uploads

**Files:**
- Modify: `backend/scadbuddy/orders/storefront/drafts.py`, `backend/scadbuddy/api/crud_storefront.py` (area `requests`), `backend/scadbuddy/crud/storefront.py` (a streamed multipart body), `backend/scadbuddy/storefront/routes.py`
- Test: `backend/tests/orders/storefront/test_drafts.py`, `backend/tests/storefront/test_edge_routes.py`

**Interfaces:**
- Consumes: `Lifecycle.add_request_item(order_id, by, *, title, quantity, notes)`, `add_attachment(order_id, item_id, by, *, attachment_id, kind, role, filename, media_type, size_bytes, sha256, blob_key, caption)`; `orders/attachments.py` `sniff(head, filename)`; `store/content.py` `ContentStore` put.
- Produces:
  - `MAX_REQUEST_FILE_BYTES: Final = 25 * 1024 * 1024`, `MAX_REQUEST_FILES: Final = 10`, `REQUEST_KINDS: Final = frozenset({"image", "video", "pdf"})` in `orders/storefront/wire.py` (the edge imports them).
  - `async def add_request(state: AppState, visitor_id: str, key: str, *, text: str, files: Sequence[UploadFile]) -> DraftView`: 404 `requests_disabled` unless `storefront_requests_enabled`; `text` 1..5000 characters; 413 `file_too_large` over 25 MB (counted while reading, never from a header); 422 `too_many_files` over 10; 415 `file_type` for a sniffed kind outside `REQUEST_KINDS`; each file put as `attachment-<sha256>`, then `add_attachment` with role `reference` on the item; the whole request under `idempotent_write`.
  - CRUD route (area `requests`): `POST …/storefront/draft/requests` (multipart `text`, `files`; `Idempotency-Key` required). Edge route `POST /api/store/v1/draft/requests`: refuses the same caps while streaming, before the CRUD call, and streams each part on.

- [ ] **Step 1: Write the failing tests**

```python
def test_a_request_keeps_its_files(crud: CrudCaller, requests_on: None) -> None:
    files = [("files", ("a.png", PNG, "image/png")), ("files", ("b.pdf", PDF, "application/pdf"))]
    item = crud.post("/storefront/draft/requests", token=TOKEN_A, data={"text": "A hook"}, files=files, key="r1").json()["items"][0]
    assert item["kind"] == "request" and [a["kind"] for a in item["attachments"]] == ["image", "pdf"]

@pytest.mark.parametrize(("files", "status"), [
    ([("files", ("x.exe", b"MZ" + b"0" * 100, "image/png"))], 415),
    ([("files", (f"{n}.png", PNG, "image/png")) for n in range(11)], 422),
    ([("files", ("big.png", PNG + b"0" * (25 * 1024 * 1024), "image/png"))], 413)])
def test_the_crud_side_caps_uploads_itself(crud: CrudCaller, requests_on: None, files: list, status: int) -> None:
    assert crud.post("/storefront/draft/requests", token=TOKEN_A, data={"text": "x"}, files=files, key="r2").status_code == status

def test_requests_are_off_by_default(crud: CrudCaller) -> None:
    assert crud.post("/storefront/draft/requests", token=TOKEN_A, data={"text": "x"}, key="r3").status_code == 404

def test_the_edge_refuses_an_oversized_file_before_the_crud_call(edge: TestClient, crud_mock: respx.Router) -> None:
    files = [("files", ("big.png", PNG + b"0" * (25 * 1024 * 1024), "image/png"))]
    assert edge.post("/api/store/v1/draft/requests", data={"text": "x"}, files=files, headers={"Origin": PUBLIC_URL}).status_code == 413
    assert not [c for c in crud_mock.calls if "/draft/requests" in str(c.request.url)]
```

- [ ] **Step 2: Run to verify they fail** — `cd backend && uv run --frozen pytest tests/orders/storefront/test_drafts.py tests/storefront/test_edge_routes.py -k request -v` — FAIL.
- [ ] **Step 3: Implement** the Interfaces block. The blob is put before the row; a refused row leaves an unrecorded object for `UnrecordedSweeper`.
- [ ] **Step 4: Run to verify they pass** — same command — PASS.
- [ ] **Step 5: Commit** — `git commit -m "feat(storefront): design requests with capped uploads"`

### Task 10: The draft sweep

**Files:**
- Modify: `backend/scadbuddy/workflows/housekeeping.py` (`STOREFRONT_DRAFTS_SWEEP`, its patch, `SWEEPS`, `PRUNE_SWEEPS`, `sweep_timeout`, the `run` guard), `backend/scadbuddy/main.py` (`_housekeeping_activities`), `backend/scadbuddy/orders/lifecycle.py`, `backend/scadbuddy/orders/store.py`, `backend/scadbuddy/orders/storefront/visitors.py`
- Test: `backend/tests/orders/test_storefront_drafts.py`, `backend/tests/test_housekeeping.py`, `backend/tests/test_housekeeping_replay.py`

**Interfaces:**
- Consumes: `store/refs.py` `BlobRefs.remove`; the stored `storefront_draft_ttl_days`.
- Produces:
  - `STOREFRONT_DRAFTS_SWEEP = "housekeeping_sweep_storefront_drafts"`, `STOREFRONT_DRAFTS_PATCH = "housekeeping-sweep-storefront-drafts"`, `STOREFRONT_DRAFTS_TIMEOUT = timedelta(minutes=5)`; last in `SWEEPS` and in `PRUNE_SWEEPS` (call 11).
  - `OrderStore.idle_drafts(conn: Conn, *, before: datetime, limit: int) -> list[str]`: unsubmitted storefront drafts whose `GREATEST(orders.updated_at, storefront_visitors.last_seen_at)` is before `before`, `FOR UPDATE SKIP LOCKED`.
  - `Lifecycle.sweep_drafts(before: datetime, *, limit: int = 200) -> list[str]`: per draft, in its own transaction: the attachment rows, then their `blob_refs` holders, then the order; never an order with `submitted_at` set or `source = 'back_office'`.
  - `VisitorStore.sweep(conn: Conn, *, visitors_before: datetime, renders_before: datetime) -> tuple[int, int]`: visitors with no order idle past the TTL, and `storefront_renders` rows older than a day.

- [ ] **Step 1: Write the failing tests**

```python
def test_the_sweep_deletes_an_idle_draft_its_attachments_and_their_holds(lifecycle: Lifecycle, idle_draft_with_request: Order, refs: BlobRefs) -> None:
    key = attachment_key(idle_draft_with_request)
    assert lifecycle.sweep_drafts(NOW - timedelta(days=30)) == [idle_draft_with_request.id]
    assert lifecycle.store.order(idle_draft_with_request.id) is None and refs.holders(key) == []

def test_the_sweep_keeps_a_recent_draft_and_every_back_office_order(lifecycle: Lifecycle, fresh_draft: Order, old_placed: Order) -> None:
    assert lifecycle.sweep_drafts(NOW - timedelta(days=30)) == []

def test_a_visitor_seen_recently_keeps_an_old_cart(lifecycle: Lifecycle, old_draft_recent_visitor: Order) -> None:
    assert lifecycle.sweep_drafts(NOW - timedelta(days=30)) == []
```

In `tests/test_housekeeping.py`, the timeout table gains `STOREFRONT_DRAFTS_SWEEP: STOREFRONT_DRAFTS_TIMEOUT`; a run recorded before the patch replays unchanged (`tests/test_housekeeping_replay.py`).

- [ ] **Step 2: Run to verify they fail** — `cd backend && uv run --frozen pytest tests/orders/test_storefront_drafts.py tests/test_housekeeping.py tests/test_housekeeping_replay.py -v` — FAIL.
- [ ] **Step 3: Implement** the Interfaces block.
- [ ] **Step 4: Run to verify they pass** — same command — PASS.
- [ ] **Step 5: Commit, then open PR 5** — `git commit -m "feat(storefront): sweep idle carts after storefront_draft_ttl_days"`

### Task 11: The public page

**Files:**
- Create: `frontend/storefront.html`, `frontend/src/storefront/main.tsx`, `App.tsx`, `api/client.ts`, `pages/ListingsPage.tsx`, `pages/CustomizePage.tsx`, `pages/DraftPage.tsx`, `components/Estimate.tsx`, `components/RequestForm.tsx`, `frontend/src/mocks/features/storefrontPublic.ts`, `frontend/e2e/storefront.spec.ts`, `backend/scadbuddy/storefront/static.py`
- Modify: `frontend/vite.config.ts` (`build.manifest: true`, `build.rolldownOptions.input: {index: 'index.html', storefront: 'storefront.html'}`, proxy `^/api/store/v1` to `SCADBUDDY_STOREFRONT_URL ?? 'http://127.0.0.1:8083'` before `/api`), `frontend/eslint.config.js`, `frontend/scripts/gen-api.mjs`, `frontend/src/components/Preview.tsx`, `.gitignore`, `Dockerfile` (`api-spec` stage), `backend/scadbuddy/tools/export_openapi.py`, `backend/scadbuddy/storefront/app.py`
- Test: `frontend/src/storefront/pages/*.test.tsx`, `frontend/src/storefront/lint.test.ts`, `backend/tests/storefront/test_static.py`

**Interfaces:**
- Consumes: every `/api/store/v1` route; `components/Preview.tsx`; `components/widgets/ParamWidget.tsx`; `lib/money.ts` `formatMoney(minor, currency, exponent)`; Task 5's `ListingView`.
- Produces:
  - `export type PreviewJob = Pick<Job, 'id' | 'status' | 'preview_url' | 'plates' | 'bbox_mm' | 'colors' | 'error'>` in `Preview.tsx` (plus any other field it reads; `StoreRender` has the same names).
  - `export_openapi --storefront <path>` writes the edge app's spec; `gen-api.mjs` writes `src/storefront/api/schema.d.ts` from it (or from `SCADBUDDY_STOREFRONT_OPENAPI_JSON`).
  - The lint rule: for `src/storefront/**`, `no-restricted-imports` patterns `**/pages/**` (except `./pages/**` inside `src/storefront`), `**/api/client`, `**/components/{orders,settings,library,print,prints,activity,assistant,flows,auth,records,media,workspace,audits,capabilities,storefront}/**`, `**/agent/**`, each with the message "The storefront imports nothing from the back office (spec §5.1)".
  - `StorefrontStatic(dist: Path)`: `storefront.html` for `/` and any non-API path; `/assets/<file>` only for files in the `storefront.html` entry's import closure in `dist/.vite/manifest.json` (`file`, `css`, `imports`, `dynamicImports`, recursively); every other path 404. Mounted last.
  - Pages: `/` the listings grid (and "Ask for a design" when `requests_enabled`); `/l/:id` customize (`ListingView`, "Render", `Estimate` with "about {price}, confirmed after we check it", "Add to order", a 429 shown as "You have made a lot of previews. Try again in {n} seconds."); `/draft` (remove an item); `/request` the request form. Every text a visitor or the shop typed renders as a React text node.

- [ ] **Step 1: Write the failing tests**

```ts
it('refuses a back-office import from the storefront', async () => {
  const eslint = new ESLint({ cwd: process.cwd() })
  const [bad] = await eslint.lintText("import { OrdersPage } from '../pages/OrdersPage'\n", { filePath: 'src/storefront/App.tsx' })
  expect(bad.messages.map((m) => m.ruleId)).toContain('no-restricted-imports')
  const [ok] = await eslint.lintText("import { formatMoney } from '../lib/money'\n", { filePath: 'src/storefront/App.tsx' })
  expect(ok.messages.filter((m) => m.ruleId === 'no-restricted-imports')).toEqual([])
})
it('shows the rough price as an estimate', async () => {
  render(<CustomizePage id="l1" />)
  await user.click(await screen.findByRole('button', { name: 'Render' }))
  expect(await screen.findByText('about $12.40, confirmed after we check it')).toBeInTheDocument()
})
it('tells the visitor when to try again', async () => {
  server.use(http.post('/api/store/v1/renders', () => HttpResponse.json(LIMITED, { status: 429, headers: { 'Retry-After': '42' } })))
  render(<CustomizePage id="l1" />)
  await user.click(await screen.findByRole('button', { name: 'Render' }))
  expect(await screen.findByText('You have made a lot of previews. Try again in 42 seconds.')).toBeInTheDocument()
})
it('renders a request text as text', async () => {
  render(<DraftPage />) // msw: a request whose notes are '<img src=x onerror=alert(1)>'
  expect(await screen.findByText('<img src=x onerror=alert(1)>')).toBeInTheDocument()
  expect(document.querySelector('img[src="x"]')).toBeNull()
})
```

```python
def test_only_the_storefront_entry_is_served(dist: Path) -> None:
    client = TestClient(static_app(dist))
    assert client.get("/l/abc").text == (dist / "storefront.html").read_text()
    assert client.get(f"/assets/{STOREFRONT_CHUNK}").status_code == 200
    assert client.get(f"/assets/{BACK_OFFICE_ONLY_CHUNK}").status_code == 404
    assert client.get("/index.html").status_code == 404
```

`e2e/storefront.spec.ts` (mocked, viewport 390 × 844): browse, open a listing, change a free parameter, render, see the estimate, add to order, see one item on `/draft`, with `document.documentElement.scrollWidth <= 390`.

- [ ] **Step 2: Run to verify they fail** — `cd frontend && pnpm gen:api && pnpm exec vitest run src/storefront`; `cd backend && uv run --frozen pytest tests/storefront/test_static.py -v` — FAIL.
- [ ] **Step 3: Implement** the Interfaces block. Use the `frontend-design` skill for the page's look (spec §12).
- [ ] **Step 4: Run every frontend check and the e2e to verify they pass**

Run: `cd frontend && pnpm lint && pnpm typecheck && pnpm test && pnpm build && pnpm exec playwright test e2e/storefront.spec.ts e2e/storefront-admin.spec.ts`
Expected: PASS; `dist/storefront.html` and `dist/.vite/manifest.json` exist.

- [ ] **Step 5: Commit, then open PR 6** (`Part of #5282` while Task 12 waits, else `Fixes #5282`)

```bash
git add frontend backend/scadbuddy/storefront backend/scadbuddy/tools .gitignore Dockerfile backend/tests/storefront
git commit -m "feat(storefront): the public page as a second Vite entry"
```

### Task 12: The visitor's payment requests (after phase 2 is on main)

Start this task only when `git grep -n "def open_requests_for_order" origin/main -- backend/scadbuddy/payments/store.py` finds phase 2's function. Until then it waits; phase 3 ships without it.

**Files:**
- Modify: `backend/scadbuddy/api/crud_storefront.py` (area `payments`), `backend/scadbuddy/crud/storefront.py`, `backend/scadbuddy/storefront/routes.py`
- Test: `backend/tests/crud/test_storefront_routes.py`

**Interfaces:**
- Consumes: phase 2's `payments/store.py` `open_requests_for_order(conn, order_id) -> list[PaymentRequest]`.
- Produces: CRUD route `GET …/storefront/draft/payment-requests -> list[PaymentRequest]`: the visitor's own draft's open requests (the visitor id resolves the visitor, the visitor its draft; `[]` with no draft). It never takes an order id. Edge route `GET /api/store/v1/draft/payment-requests`. `CRUD_AREAS["storefront"]` gains `"payments"`.

- [ ] **Step 1: Write the failing test**

```python
def test_a_visitor_reads_only_its_own_drafts_payment_requests(crud: CrudCaller, draft_request_for: Callable[[str], str]) -> None:
    mine = draft_request_for(TOKEN_A)
    draft_request_for(TOKEN_B)
    assert [r["id"] for r in crud.get("/storefront/draft/payment-requests", token=TOKEN_A).json()] == [mine]
    assert crud.get("/storefront/draft/payment-requests", token=TOKEN_C).json() == []
```

- [ ] **Step 2: Run to verify it fails** — `cd backend && uv run --frozen pytest tests/crud/test_storefront_routes.py -k payment -v` — FAIL with 404.
- [ ] **Step 3: Add the routes.**
- [ ] **Step 4: Run to verify it passes** — same command — PASS.
- [ ] **Step 5: Commit, then open PR 7** (`Fixes #5282`) — `git commit -m "feat(storefront): the visitor's draft payment requests"`

---

## Self-review

1. **Spec coverage.** §5.1 (as amended by PR #5642): the edge process, port and routes (Tasks 1, 2); no back-office route (Task 1); `s3` only (Task 6); uploads with row and holder in one transaction (Task 9, call 1); presigned media and attachments (Tasks 6, 8); the second Vite entry and the lint rule (Task 11). §5.2 visitor: Tasks 4, 6. §5.3 listings, the Storefront page and its live preview; a mesh model is a listing with no `params`; "Ask for a design" behind `storefront_requests_enabled`: Tasks 3, 5, 9, 11. §5.4 validation against the listing, `reuse_finished`, the render limits with a 429 and a retry time, the estimate from the render job, `orders/estimate.py`, add to order (model and request), the upload caps, the nullable customer and number with the CHECK, numbers unused, drafts hidden in the back office, and the sweep with its delete order: Tasks 4, 6-10. §5.8 per-client limits: Task 6; text never HTML: Task 11. §6.1 `orders.source` and the filter: Task 4. §10 environment (the cookie key, no database URL, Temporal or KEK), start refusal and test: Task 1. The owner's decision: Tasks 1, 2 and the Trust model; no role, grants, view or role test remains.
2. **Step scan.** Every code step names its signature and file. The only bodies are the migration columns, which the spec fixes.
3. **Type consistency.** `Listing`, `ListingInput`, `ListingSnapshot`, `ParamBound` (Task 5) keep their names in Tasks 6-9. `listing_values`, `listing_reference` (Task 6) keep theirs in Tasks 7 and 8. `VisitorStore`, `Visitor`, `visitor_actor` (Task 4) keep theirs in Tasks 7-10. `RenderLimits.admit/free/attach/owns` take `conn` first (Task 7). `PreviewJob` (Task 11) matches `StoreRender` (Task 7). The headers `VISITOR_HEADER`, `CLIENT_HEADER` and the caps (`wire.py`) are shared by both sides. `CRUD_AREAS["storefront"]` grows `listings`, `renders`, `drafts`, `requests`, `payments`, and `EDGE_AREAS` is their union (Task 2).
4. **Review Focus.** Each of the five lines has its test: two tabs (Task 8), null readers (Task 4), a joined open render (Task 7), a retried render and add (Tasks 7, 8), a stale listing (Task 8).
5. **Proportion.** Twelve tasks over a 440-line spec; the code blocks are tests, and the migrations' columns.
