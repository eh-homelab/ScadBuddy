# Storefront phase 2: the payments seam, deposits, shipping options and auto tax Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The back office can take money through pluggable payment providers (a request with a hosted link, paid by a signed webhook) or by hand, and an order can carry a deposit, a balance rule, a shipping option and a tax charge that adds itself up (#4957, #5281).

**Architecture:** A new feature package `scadbuddy/payments/` holds the provider seam: a `PAYMENTS` component, a registry of provider modules (a fake one for tests and dev), sealed credentials, and the `payment_requests` and `payment_events` rows. Every write that touches an order (opening, closing or cancelling a request, a payment, a fee, a deposit, the balance waiver, the tax charge) stays a `Lifecycle` method under the order's row lock, with its audit row and bus event. A provider is called only after commit: the lifecycle writes the request row, then `payments/service.py` asks the provider for a hosted link and stores it.

**Tech Stack:** Python 3.12, FastAPI, psycopg 3, Postgres 18, pytest; the KEK sealing in `core/secrets.py`; TypeScript agent tools (zod, `defineTool`); React 19 + msw + vitest.

**Spec:** `docs/superpowers/specs/2026-11-02-storefront-design.md` §2 (decisions 9-11), §6.2 (`set_deposit`, `waive_balance`), §6.3 (`deposit_unpaid`, `balance_unpaid`), §7, §8, §9, §11 item 2. Issues #4957 (the seam, and its comments: spec §7's four additions; the `payment_fee` cost entry) and #5281. The fee entry is the job-cost spec `docs/superpowers/specs/2026-11-05-job-cost-design.md` §4.4.

Not in this phase (later plans): `orders.source`, `submit_order`, the `customer` actor kind, "Accept for the customer" (§6.1, §6.4); the storefront role and `STOREFRONT_GRANTS` (phase 3); the public site's "Pay" button and return page (phase 4); Stripe (#4958) and the other providers.

## PR boundaries

| PR | Issue | Tasks | Contents |
|----|-------|-------|----------|
| A1 | #4957 | 1-5 | Backend and agent: the seam, manual payments that close a request, the webhook, the routes, the agent tools, README. |
| A2 | #4957 | 6 | Frontend: Settings → Payments, "Request payment", the payment rows. `Fixes #4957`. |
| B1 | #5281 | 7-11 | Backend and agent: settings, auto tax, shipping options, deposits, the balance rule, routes, agent tools. |
| B2 | #5281 | 12 | Frontend: the order page's deposit, balance, shipping and tax; Settings → Orders editors. `Fixes #5281`. |

Each PR branches from `main` after the one before it merges (no stacked auto-merge). A1's routes and its agent tools ship together, because `agent/test/coverage.test.ts` fails on a route with neither a tool nor a `NOT_A_TOOL` entry.

## Global Constraints

- Only the CRUD service (the API process) gets SQL. Every new table is read and written by the API's own pools (`ORDERS`'s). No new database client, no new role, no grant, no `STOREFRONT_GRANTS` in this phase (CLAUDE.md "Database access (#5295)").
- `PAYMENTS` is a `Component` in `backend/scadbuddy/payments/component.py`, never an `AppState` field (`core/components.py`).
- Migrations: one NEW file per PR, `backend/scadbuddy/migrations/$(date -u +%Y%m%dT%H%MZ)_<slug>.sql`: `payments_seam` (A1), `order_deposits_tax` (B1). Edit no merged file.
- Never commit `backend/openapi.json`, `frontend/src/api/schema.d.ts` or `agent/src/api/schema.d.ts`; `pnpm gen:api` writes them.
- Money is integer minor units in the order's currency (`Money = int`); a percent is a decimal string (`Rate`), never a float; a computed amount is rounded once, half up (`orders/pricing.py`).
- The storefront's shared shape, exactly: `PaymentRequest = {id: str, order_id: str, kind: "deposit" | "balance", provider: str | None, amount_minor: int, currency: str, hosted_url: str | None, status: "open" | "paid" | "expired" | "cancelled", created_at: datetime, paid_at: datetime | None}`, and `open_requests_for_order(conn, order_id) -> list[PaymentRequest]` in `backend/scadbuddy/payments/store.py`. With no provider, `provider` and `hosted_url` are None. `provider_ref` is a column and is on the back office's `PaymentRequestView`, never on `PaymentRequest`.
- Payment credentials are sealed with `core/secrets.py` `seal_secret`, AAD `payment_credentials:<row id>:<provider>`. No route, log, event or audit row ever carries a secret. Without the KEK, connecting a provider is refused (409 `kek_missing`).
- The webhook route `POST /api/v1/payments/webhooks/{provider}` is the one back-office route the public reaches (route class `public`). A bad signature is 400 and records nothing. A replayed event (same provider and event id) records one payment.
- A provider's fee is a `payment_fee` cost entry, `source` `payment:<payment id>`, written in the transaction that records the payment, through the lifecycle (job-cost spec §4.4). A manual payment writes no fee entry.
- Waiver columns: `orders.deposit_waived_reason` ("Proceed without deposit") and `orders.balance_waived_reason` ("Ship without full payment"), each `text`, 1-500 characters after trimming, and each with its audit row (`deposit.waived`, `balance.waived`, `detail.reason`). An empty reason is 422 on `reason`.
- New refusal slugs, each a `LifecycleError` mapped by `orders/problems.py` `refused`: `deposit_unpaid`, `balance_unpaid` (spec §6.3), and this plan's `nothing_due`, `payment_request_not_open`, `payment_request_not_found` (404), `shipping_option_not_found` (422 `option_id`), `shipping_address_required`.
- Settings: `orders_tax_percent` (decimal string, default `"0"`, `0 <= x < 100`, env `SCADBUDDY_ORDERS_TAX_PERCENT`, live), `orders_tax_shipping` (bool, default false, env `SCADBUDDY_ORDERS_TAX_SHIPPING`, live); stored only (no env, like `asset_fetch_domains`): `orders_deposit_policy` (list of rules, at most 20), `storefront_shipping_options` (list `{id, label, amount_minor, ships}`, at most 20, ids unique), `storefront_payment_instructions` (text, at most 2000). Bootstrap only: `payments_fake_provider` (bool, default false, env `SCADBUDDY_PAYMENTS_FAKE_PROVIDER`).
- Agent tiers: `order_request_payment` and `order_deposit_set` and `order_balance_waive` are `outward`; `order_payment_requests` is `read`; `order_deposit_waive` and `order_shipping_option_set` are `write`. Every new `/api/v1` operation has a tool or a `NOT_A_TOOL` entry with its reason.
- Text a customer or a provider sends is never rendered as HTML.
- Each behaviour gets a test watched failing first. Backend Postgres tests need `SCADBUDDY_TEST_DATABASE_URL` (backend/CLAUDE.md "Commands"); a frontend `pnpm exec vitest` run needs `pnpm gen:api` first.

### Spec gaps this plan settles (each a call the owner may overturn)

1. **Which orders get a balance request at accept.** Spec §8 says acceptance opens it, and #5281 says a `back_office` order with no open balance request ships as today. `orders.source` does not exist until phase 4. Here `accept_order` opens a `balance` request when the order has a deposit set or already has any payment request; phase 4 adds "or `source = 'storefront'`". Every other order ships as today.
2. **When `balance_unpaid` applies.** While the order has a `balance` request not `cancelled` (open, paid or expired), `balance_waived_reason` is null, and the live balance (total minus paid) is above zero. A balance request paid in full and then grown by an item added after acceptance still holds the shipment; the shop requests the rest.
3. **Deposit paid** means the order's payments (refunds included) sum to at least `deposit_minor`. A manual payment that covers an open deposit request, or brings the balance to zero or below under an open balance request, closes that request as `paid` with that payment's id, even when the payment names no request.
4. **A request's amount is fixed** once opened. `set_deposit` cancels the open deposit request and opens a new one for the new amount minus paid; nothing resizes an open balance request.
5. **Cancelling an order cancels its open requests**, so no one pays a cancelled order. A provider's `paid` event is recorded even on a cancelled or closed order (the money arrived); the request becomes `paid`.
6. **A typed tax charge wins.** When an order has a `tax` charge that is not `auto`, the `auto` row is removed and not recomputed. The tax base is the spec's literal: item totals plus shipping charges when `orders_tax_shipping`; fees and discounts are not in it. "Open" for the recompute is `draft`, `quoted`, `accepted`, `in_production` and `ready`.
7. **The deposit policy in the back office** runs on "Use the policy" (`POST /orders/{id}/deposit/policy`); phase 4's submit calls the same method. Fixed amounts and `total_above` are in `orders_currency`, so those rules apply only to orders in that currency; a percent rule applies to any order.
8. **The default provider.** `payment_credentials.is_default` (at most one row). Requests the lifecycle opens (deposit, balance) and a request with no provider named use it; with none connected, the request has no provider.
9. **Provider calls after commit.** A failed link leaves the request `open` with `link_error` set; `POST …/payment-requests/{id}/link` retries, passing the request id as the provider's idempotency key.
10. **The webhook's actor** is a new `Actor.kind` `provider`, `principal` `payment:<provider id>`.
11. **Disconnecting** a provider is refused (409 `provider_has_open_requests`) while an open request names it, so a customer's link never pays into a provider whose webhooks ScadBuddy no longer reads.
12. **Connecting, disconnecting and choosing the default provider are person-only** (`auth/routes.py` `PERSON_ENDPOINTS`), as settings writes are never an agent's.
13. **An event whose `provider_ref` names no request** is recorded (`outcome = 'unmatched'`) and answered 200, so the provider stops retrying; an event in another currency than the order is recorded as `currency_mismatch` with no payment.
14. **The fake provider** appears only when `SCADBUDDY_PAYMENTS_FAKE_PROVIDER=true` (a bootstrap value, never stored), so prod cannot pick it.
15. **A shipping option** writes one `shipping` charge carrying `shipping_option_id`, replacing the order's other `shipping` charges; an option that ships needs the order's shipping address (`shipping_address_required`); a pickup needs none.

## Review Focus

1. **The shop cancels an order while a deposit link is out.** Expected: the open requests become `cancelled` in the cancel's transaction, so phase 4 shows no "Pay". Test in Task 3.
2. **A provider's `paid` webhook arrives after the order was cancelled.** Expected: the payment and its fee are recorded and the request is `paid`; the shop refunds by hand. Test in Task 3.
3. **The shop records a cash payment for the deposit without naming the request.** Expected: the open deposit request closes as `paid` and `deposit_unpaid` no longer refuses a send. Test in Task 9.
4. **The shop types a tax charge on an order with auto tax on.** Expected: one tax, the typed one; the `auto` row is gone and stays gone. Test in Task 8.
5. **The charges editor saves the whole list** (`PATCH /orders/{id}` `charges`, `PUT …/charges/after-acceptance`). Expected: the `auto` tax row survives and is recomputed, and a body that sends `auto: true` is refused. Test in Task 8.

---

## File structure

Backend (`backend/scadbuddy/`):
- `migrations/<ts>_payments_seam.sql`, `migrations/<ts>_order_deposits_tax.sql` (create).
- `payments/__init__.py`, `payments/models.py`, `payments/store.py`, `payments/credentials.py`, `payments/provider.py`, `payments/registry.py`, `payments/fake.py`, `payments/service.py`, `payments/component.py` (create).
- `orders/totals.py` (create): `OrderTotals`, `item_price`, `order_totals`, moved out of `api/orders.py` so the lifecycle can read a balance.
- `orders/money.py` (create): `DepositRule`, `ShippingOption`, `OrderRules`, `tax_minor`, `tax_base`, `deposit_for`.
- `orders/models.py`, `orders/store.py`, `orders/lifecycle.py`, `orders/component.py` (modify).
- `api/order_payments.py`, `api/payments.py`, `api/payment_webhooks.py` (create); `api/orders.py`, `api/settings.py` (modify).
- `auth/routes.py`, `core/settings.py`, `library/settings_store.py` (modify).

Agent: `agent/src/tools/orders.ts`, `agent/src/tools/coverage.ts`, `agent/test/orders.test.ts`, `agent/test/toolTier.test.ts`.

Frontend (`frontend/src/`): `api/types.ts`, `api/client.ts`; `mocks/features/payments.ts` (create), `mocks/features/orders/money.ts` (create), `mocks/features/orders/state.ts`, `mocks/features/orders/orders.ts`, `mocks/features/settings.ts`, `mocks/fixtures.ts`; `pages/settings/sections/payments.tsx` (create) and its test; `components/orders/PaymentRequests.tsx`, `components/orders/DepositControls.tsx`, `components/orders/ShippingOptionPicker.tsx`, `components/settings/DepositPolicyEditor.tsx`, `components/settings/ShippingOptionsEditor.tsx` (create, each with a test); `components/orders/ChargesEditor.tsx`, `pages/OrderPage.tsx`, `pages/settings/sections/orders.tsx`, `pages/settings/fields.ts` (modify).

Docs: `README.md` "Deploying", `backend/CLAUDE.md` (the orders and payments notes).

---

## PR A1 (#4957): the seam

### Task 1: Schema, row shapes and the payments store

**Files:**
- Create: `backend/scadbuddy/migrations/<ts>_payments_seam.sql`, `backend/scadbuddy/payments/__init__.py`, `backend/scadbuddy/payments/models.py`, `backend/scadbuddy/payments/store.py`
- Modify: `backend/scadbuddy/orders/models.py` (`Actor`, `Payment`), `backend/scadbuddy/orders/store.py` (`_PAYMENT`, `insert_payment`)
- Test: `backend/tests/test_pg_migrations.py`, `backend/tests/payments/__init__.py`, `backend/tests/payments/conftest.py` (as `tests/orders/conftest.py`: `pool`, `rows`), `backend/tests/payments/test_store.py`

**Interfaces:**
- Consumes: `orders/store.py` `Conn`, `OrderStore.transaction`, `OrderStore.insert_payment`.
- Produces (`payments/models.py`):
  - `PaymentRequestKind = Literal["deposit", "balance"]`; `PaymentRequestStatus = Literal["open", "paid", "expired", "cancelled"]`.
  - `class PaymentRequest(BaseModel)`: the shared shape in Global Constraints, field for field.
  - `class PaymentRequestView(PaymentRequest)`: `provider_ref: str | None`, `link_error: str | None`, `payment_id: str | None`, `created_by: Actor`.
  - `ProviderEventKind = Literal["paid", "refunded", "expired", "other"]`; `EventOutcome = Literal["recorded", "replayed", "unmatched", "ignored", "currency_mismatch"]`.
  - `class ProviderEvent(BaseModel)`: `event_id: str` (1-200), `kind: ProviderEventKind`, `provider_ref: str | None`, `amount_minor: Money | None` (`ge=1`), `currency: str | None`, `fee_minor: Money | None` (`ge=0`).
  - `class HostedRequest(BaseModel)`: `provider_ref: str`, `hosted_url: HttpUrl` (https only).
- Produces (`orders/models.py`): `Actor.kind: Literal["browser", "agent", "provider"]`; `Payment` gains `provider: str | None = None`, `payment_request_id: str | None = None`.
- Produces (`payments/store.py`, each in the caller's transaction):
  - `insert_request(conn: Conn, *, order_id: str, kind: PaymentRequestKind, provider: str | None, amount_minor: Money, currency: str, created_by: Actor) -> PaymentRequestView`
  - `request(conn: Conn, request_id: str) -> PaymentRequestView | None`
  - `requests_for_order(conn: Conn, order_id: str) -> list[PaymentRequestView]` (oldest first)
  - `open_requests_for_order(conn: Conn, order_id: str) -> list[PaymentRequest]` (status `open`, oldest first, built as `PaymentRequest`, so `provider_ref` never leaves)
  - `request_by_ref(conn: Conn, provider: str, provider_ref: str) -> PaymentRequestView | None`
  - `set_request_status(conn: Conn, request_id: str, status: PaymentRequestStatus, *, payment_id: str | None = None) -> PaymentRequestView` (`paid` stamps `paid_at`)
  - `set_request_link(conn: Conn, request_id: str, *, provider_ref: str | None, hosted_url: str | None, link_error: str | None) -> PaymentRequestView`
  - `insert_event(conn: Conn, provider: str, event: ProviderEvent) -> int | None` (the row id; None when `(provider, event_id)` is already there)
  - `finish_event(conn: Conn, event_row: int, *, outcome: EventOutcome, order_id: str | None = None, payment_request_id: str | None = None, payment_id: str | None = None) -> None`
  - `default_provider(conn: Conn) -> str | None`; `has_open_requests(conn: Conn, provider: str) -> bool`
- Produces (`orders/store.py`): `insert_payment(..., provider: str | None = None, payment_request_id: str | None = None) -> Payment`.

- [ ] **Step 1: Write the failing migration test** in `backend/tests/test_pg_migrations.py`

```python
def test_the_payments_seam_migration(pg_conninfo: str) -> None:
    # migrate everything; one customer, one order, one payment
    cols = {r["column_name"] for r in conn.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name = 'order_payments'")}
    assert {"provider", "payment_request_id"} <= cols
    with pytest.raises(psycopg.errors.CheckViolation):   # amount must be > 0
        conn.execute("INSERT INTO payment_requests (order_id, kind, amount_minor, currency, created_by)"
                     " VALUES (%s, 'balance', 0, 'USD', '{}')", (order_id,))
    with pytest.raises(psycopg.errors.CheckViolation):   # no provider, no link
        conn.execute("INSERT INTO payment_requests (order_id, kind, amount_minor, currency, created_by, hosted_url)"
                     " VALUES (%s, 'deposit', 5, 'USD', '{}', 'https://x')", (order_id,))
    conn.execute("INSERT INTO payment_events (provider, event_id, kind) VALUES ('fake', 'e1', 'paid')")
    with pytest.raises(psycopg.errors.UniqueViolation):
        conn.execute("INSERT INTO payment_events (provider, event_id, kind) VALUES ('fake', 'e1', 'paid')")
    # two default credentials are refused (partial unique index)
```

- [ ] **Step 2: Write the failing store tests** in `backend/tests/payments/test_store.py`

```python
def test_open_requests_for_order_leaves_out_provider_ref_and_closed_ones(rows: OrderStore) -> None:
    # two requests on one order: one linked (provider "fake", ref "fake_1"), one paid
    found = open_requests_for_order(conn, order.id)
    assert [r.id for r in found] == [linked.id]
    assert type(found[0]) is PaymentRequest and "provider_ref" not in found[0].model_dump()
    assert found[0].hosted_url == "https://payments.fake.invalid/pay/fake_1"

def test_a_request_with_no_provider_has_no_link(rows: OrderStore) -> None:
    req = insert_request(conn, order_id=order.id, kind="deposit", provider=None, amount_minor=500, currency="USD", created_by=BY)
    assert (req.provider, req.hosted_url, req.status, req.paid_at) == (None, None, "open", None)

def test_paid_stamps_paid_at_and_names_the_payment(rows: OrderStore) -> None:
    paid = set_request_status(conn, req.id, "paid", payment_id=payment.id)
    assert paid.status == "paid" and paid.paid_at is not None and paid.payment_id == payment.id

def test_an_event_is_inserted_once(rows: OrderStore) -> None:
    first = insert_event(conn, "fake", EVENT)
    assert isinstance(first, int) and insert_event(conn, "fake", EVENT) is None

def test_request_by_ref_is_per_provider(rows: OrderStore) -> None:
    assert request_by_ref(conn, "fake", "fake_1").id == linked.id
    assert request_by_ref(conn, "other", "fake_1") is None
```

- [ ] **Step 3: Run them to verify they fail**

Run: `cd backend && uv run pytest tests/test_pg_migrations.py::test_the_payments_seam_migration tests/payments/test_store.py -v`
Expected: FAIL (no table `payment_requests`, no module `scadbuddy.payments`).

- [ ] **Step 4: Write the migration**

One file, in this order:
- `payment_credentials`: `id text PK DEFAULT gen_random_uuid()::text`, `provider text NOT NULL UNIQUE CHECK (provider ~ '^[a-z][a-z0-9_]{0,39}$')`, `account_label text NOT NULL`, `settings jsonb NOT NULL DEFAULT '{}'` (non-secret fields), `secret_sealed bytea NOT NULL`, `dek_sealed bytea NOT NULL`, `kek_id text NOT NULL`, `is_default boolean NOT NULL DEFAULT false`, `connected_at timestamptz NOT NULL DEFAULT clock_timestamp()`, `last_webhook_at timestamptz`; `CREATE UNIQUE INDEX payment_credentials_default ON payment_credentials ((true)) WHERE is_default`.
- `payment_requests`: `id`, `order_id text NOT NULL REFERENCES orders (id) ON DELETE CASCADE`, `kind text NOT NULL CHECK (kind IN ('deposit','balance'))`, `provider text`, `provider_ref text`, `hosted_url text`, `amount_minor bigint NOT NULL CHECK (amount_minor > 0)`, `currency text NOT NULL`, `status text NOT NULL DEFAULT 'open' CHECK (status IN ('open','paid','expired','cancelled'))`, `link_error text`, `payment_id text` (FK added below), `created_by jsonb NOT NULL`, `created_at timestamptz NOT NULL DEFAULT clock_timestamp()`, `paid_at timestamptz`, `CHECK ((status = 'paid') = (paid_at IS NOT NULL))`, `CHECK (provider IS NOT NULL OR (provider_ref IS NULL AND hosted_url IS NULL))`; index `(order_id, created_at)`; `CREATE UNIQUE INDEX payment_requests_ref ON payment_requests (provider, provider_ref) WHERE provider_ref IS NOT NULL`.
- `order_payments`: `ADD COLUMN provider text`, `ADD COLUMN payment_request_id text REFERENCES payment_requests (id)`; then `ALTER TABLE payment_requests ADD FOREIGN KEY (payment_id) REFERENCES order_payments (id)`.
- `payment_events`: `id bigserial PK`, `provider text NOT NULL`, `event_id text NOT NULL`, `kind text NOT NULL`, `outcome text CHECK (outcome IN ('recorded','replayed','unmatched','ignored','currency_mismatch'))`, `order_id text REFERENCES orders (id) ON DELETE SET NULL`, `payment_request_id text REFERENCES payment_requests (id) ON DELETE SET NULL`, `payment_id text REFERENCES order_payments (id) ON DELETE SET NULL`, `received_at timestamptz NOT NULL DEFAULT clock_timestamp()`, `UNIQUE (provider, event_id)`. No body column: an event's payload is never stored.

- [ ] **Step 5: Implement the models and store functions** named in Interfaces. `payments/store.py` imports only `orders/store.py` (`Conn`), `orders/models.py` and `payments/models.py`, so `orders/lifecycle.py` can import it without a cycle. Add `provider, payment_request_id` to `orders/store.py` `_PAYMENT`.

- [ ] **Step 6: Run the tests to verify they pass**

Run: `cd backend && uv run pytest tests/test_pg_migrations.py tests/payments tests/orders -v && uv run mypy`
Expected: PASS; mypy clean.

- [ ] **Step 7: Commit**

```bash
git add backend/scadbuddy/migrations backend/scadbuddy/payments backend/scadbuddy/orders/models.py backend/scadbuddy/orders/store.py backend/tests/test_pg_migrations.py backend/tests/payments
git commit -m "feat(payments): payment requests, events and credentials tables (#4957)"
```

---

### Task 2: The provider seam: protocol, registry, fake provider and sealed credentials

**Files:**
- Create: `backend/scadbuddy/payments/provider.py`, `backend/scadbuddy/payments/registry.py`, `backend/scadbuddy/payments/fake.py`, `backend/scadbuddy/payments/credentials.py`
- Modify: `backend/scadbuddy/core/settings.py` (`payments_fake_provider`, `BOOTSTRAP_FIELDS`)
- Test: `backend/tests/payments/test_fake.py`, `backend/tests/payments/test_credentials.py`

**Interfaces:**
- Consumes: `core/secrets.py` `Kek`, `Envelope`, `seal_secret`, `open_secret`; `library/settings_store.py` `settings_kek`; Task 1's models.
- Produces (`payments/provider.py`):
  - `@dataclass(frozen=True) class Credential`: `settings: Mapping[str, str]`, `secrets: Mapping[str, str] = field(repr=False)`.
  - `class ProviderField(BaseModel)`: `name: str`, `label: str`, `secret: bool`.
  - `class BadSignatureError(Exception)`; `class ProviderError(Exception)` (its message is safe to show: no secret).
  - `class PaymentProvider(Protocol)`: `id: str`; `label: str`; `fields: Sequence[ProviderField]`; `async def verify(self, credential: Credential) -> str` (the account label for "Connected as …"); `async def create_request(self, credential: Credential, request: PaymentRequestView, *, return_url: str | None, idempotency_key: str) -> HostedRequest`; `def parse_webhook(self, credential: Credential, headers: Mapping[str, str], body: bytes) -> ProviderEvent` (raises `BadSignatureError` before it parses anything).
- Produces (`payments/registry.py`): `def providers(*, fake: bool) -> dict[str, PaymentProvider]` (empty today but for `fake` when `fake` is true; #4958 adds Stripe here).
- Produces (`payments/fake.py`):
  - `FAKE = FakeProvider()`, `id = "fake"`, fields `account` (not secret) and `signing_secret` (secret).
  - `SIGNATURE_HEADER = "x-fake-signature"`; `def sign(secret: str, body: bytes) -> str` (`"sha256=" + hmac_sha256(secret, body).hexdigest()`); `def signed_event(secret: str, event: Mapping[str, object]) -> tuple[bytes, dict[str, str]]` (the body and headers a test or a dev script posts).
  - `verify` returns `f"Fake account {account}"`; `create_request` returns `provider_ref = f"fake_{request.id}"`, `hosted_url = f"https://payments.fake.invalid/pay/fake_{request.id}"`; `parse_webhook` checks the header with `hmac.compare_digest`, then reads `{"id", "type", "provider_ref", "amount_minor", "currency", "fee_minor"}` (`type` one of `paid`, `refunded`, `expired`, else `other`).
- Produces (`payments/credentials.py`):
  - `def credential_aad(credential_id: str, provider: str) -> str` → `f"payment_credentials:{credential_id}:{provider}"`.
  - `class KekMissingError(Exception)`; `class CredentialUnreadableError(Exception)` (names the provider only).
  - `class ProviderRow(BaseModel)`: `provider: str`, `account_label: str`, `settings: dict[str, str]`, `is_default: bool`, `connected_at: datetime`, `last_webhook_at: datetime | None`.
  - `class PaymentCredentials`: `__init__(self, store: OrderStore, kek: Kek | None)`; `connect(self, provider: str, *, settings: Mapping[str, str], secrets: Mapping[str, str], account_label: str) -> ProviderRow` (upsert; the first row connected becomes default); `disconnect(self, provider: str) -> bool` (raises `ProviderBusyError` while `has_open_requests`; the default moves to the oldest other row); `credential(self, provider: str) -> Credential | None`; `rows(self) -> list[ProviderRow]`; `set_default(self, provider: str) -> None`; `touch_webhook(self, provider: str) -> None`.
- Produces (`core/settings.py`): `payments_fake_provider: bool = False`, in `BOOTSTRAP_FIELDS` (env `SCADBUDDY_PAYMENTS_FAKE_PROVIDER`).

- [ ] **Step 1: Write the failing tests**

```python
# tests/payments/test_fake.py
def test_a_signed_event_parses() -> None:
    body, headers = signed_event("s3cret", {"id": "e1", "type": "paid", "provider_ref": "fake_r1", "amount_minor": 500, "currency": "USD", "fee_minor": 30})
    event = FAKE.parse_webhook(CRED, headers, body)
    assert event == ProviderEvent(event_id="e1", kind="paid", provider_ref="fake_r1", amount_minor=500, currency="USD", fee_minor=30)

def test_a_bad_signature_is_refused_before_the_body_is_read() -> None:
    body, headers = signed_event("other", {"id": "e1", "type": "paid"})
    with pytest.raises(BadSignatureError):
        FAKE.parse_webhook(CRED, headers, body)
    with pytest.raises(BadSignatureError):
        FAKE.parse_webhook(CRED, {}, b"not json")

def test_the_registry_offers_the_fake_only_when_asked() -> None:
    assert providers(fake=False) == {} and set(providers(fake=True)) == {"fake"}

# tests/payments/test_credentials.py (Postgres)
def test_a_secret_is_sealed_in_its_row_and_opens_back(rows: OrderStore, pool: PgPool) -> None:
    creds = PaymentCredentials(rows, KEK)
    creds.connect("fake", settings={"account": "a"}, secrets={"signing_secret": "s3cret"}, account_label="Fake account a")
    raw = pool.connection().execute("SELECT * FROM payment_credentials").fetchone()
    assert b"s3cret" not in raw["secret_sealed"] and raw["kek_id"] == KEK.id
    assert creds.credential("fake").secrets == {"signing_secret": "s3cret"}

def test_the_seal_is_bound_to_the_row_and_the_provider(rows: OrderStore, pool: PgPool) -> None:
    # copy the row's sealed columns onto a second row with provider "other"
    with pytest.raises(CredentialUnreadableError):
        creds.credential("other")

def test_no_kek_refuses_a_connect(rows: OrderStore) -> None:
    with pytest.raises(KekMissingError):
        PaymentCredentials(rows, None).connect("fake", settings={}, secrets={"signing_secret": "x"}, account_label="x")

def test_the_first_connected_is_the_default_and_disconnect_moves_it(rows: OrderStore) -> None: ...

def test_disconnect_is_refused_while_a_request_is_open(rows: OrderStore) -> None:
    with pytest.raises(ProviderBusyError):
        creds.disconnect("fake")

def test_repr_never_shows_a_secret() -> None:
    assert "s3cret" not in repr(Credential(settings={}, secrets={"signing_secret": "s3cret"}))
```

- [ ] **Step 2: Run them to verify they fail**

Run: `cd backend && uv run pytest tests/payments/test_fake.py tests/payments/test_credentials.py -v`
Expected: FAIL (no module `scadbuddy.payments.fake`).

- [ ] **Step 3: Implement** the modules in Interfaces. The sealed plaintext is `json.dumps(dict(secrets), sort_keys=True)`; the columns are `secret_sealed`, `dek_sealed`, `kek_id` as `printers/store.py` stores an access code. A row whose seal will not open logs the provider name only and raises `CredentialUnreadableError`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd backend && uv run pytest tests/payments -v && uv run mypy`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/scadbuddy/payments backend/scadbuddy/core/settings.py backend/tests/payments
git commit -m "feat(payments): the provider seam, a fake provider and sealed credentials (#4957)"
```

---

### Task 3: The lifecycle's money: requests, manual payments, provider events and fees

**Files:**
- Create: `backend/scadbuddy/orders/totals.py`
- Modify: `backend/scadbuddy/orders/lifecycle.py` (`add_payment`, `cancel_order`, new methods), `backend/scadbuddy/api/orders.py` (import `OrderTotals`, `item_price`, `order_totals` from `orders/totals.py`; delete its copies)
- Test: `backend/tests/orders/test_totals.py`, `backend/tests/orders/test_payment_requests.py` (reuse `Shop`, `BY` from `tests.orders.test_lifecycle`)

**Interfaces:**
- Consumes: Task 1's store functions and models; `OrderStore.insert_cost_entry`; `CostEntryInput`.
- Produces (`orders/totals.py`):
  - `class OrderTotals(BaseModel)`: exactly today's fields (`api/orders.py:424`).
  - `def item_price(item: OrderItem, quotes: Sequence[Quote]) -> tuple[Money, bool] | None` (today's rule).
  - `def order_totals(items: Sequence[tuple[OrderItem, Sequence[Quote]]], charges: Sequence[Charge], payments: Sequence[Payment]) -> OrderTotals`.
  - `def totals_of(store: OrderStore, conn: Conn, order_id: str) -> OrderTotals`.
- Produces (`Lifecycle`):
  - `open_payment_request(self, order_id: str, by: Actor, *, kind: PaymentRequestKind = "balance", amount_minor: Money | None = None, provider: str | None | UnsetType = UNSET) -> PaymentRequestView`: `amount_minor` None is the balance now; `UNSET` provider is `default_provider(conn)`. Refused: `order_cancelled`/`order_closed`; `nothing_due` when the amount is 0 or less. Audit `payment_request.opened` (`request_id`, `kind`, `amount_minor`).
  - `cancel_payment_request(self, order_id: str, request_id: str, by: Actor) -> PaymentRequestView`: `payment_request_not_found` (404, also for another order's request), `payment_request_not_open`. Audit `payment_request.cancelled`.
  - `set_request_link(self, request_id: str, *, hosted: HostedRequest | None, error: str | None) -> PaymentRequestView`: under the request's order lock; publishes `order.changed`; writes no audit row.
  - `add_payment(..., payment_request_id: str | None = None)`: the named request must be this order's (`payment_request_not_found`) and `open` (`payment_request_not_open`), and `amount_minor >= 1` (422 `amount_minor`); it becomes `paid` with this payment's id. Then, named or not, an open `deposit` request is closed as `paid` once payments reach `deposit_minor` (Task 9 adds the column; until then only a named request closes), and an open `balance` request once the balance is 0 or less. Audit `payment.added` gains `payment_request_id`.
  - `record_provider_event(self, provider: str, event: ProviderEvent) -> EventOutcome`: one transaction. `insert_event` None → `replayed`. No `provider_ref` or no request → `unmatched`. Else lock the request's order; `kind` `paid`: when `event.currency` differs from the order's → `currency_mismatch`; else insert a payment (`amount_minor` the event's, `method = provider`, `reference = provider_ref`, `provider`, `payment_request_id`), set the request `paid` (from any status), and when `fee_minor` is above 0 insert a `CostEntryInput(kind="payment_fee", amount_minor=fee_minor, source=f"payment:{payment.id}", note=f"{provider} fee")`; `refunded`: a payment of `-amount_minor`, same request id, no fee entry; `expired`: an `open` request becomes `expired`; `other` → `ignored`. Every outcome is written by `finish_event`. Actor `Actor(kind="provider", principal=f"payment:{provider}")`. Audit rows `payment.added` (with `provider`) and `cost.added` for the fee. Allowed on a cancelled, closed or on-hold order.
  - `cancel_order` cancels every open request of the order in its transaction (audit `payment_request.cancelled` each).

- [ ] **Step 1: Write the failing tests**

```python
# tests/orders/test_totals.py
def test_order_totals_match_the_old_route() -> None:
    # one accepted item 1000, one quoted-not-accepted 300 x 2, one with no quote; charge +500; payment 200
    t = order_totals(items, charges, payments)
    assert (t.subtotal_minor, t.pending_minor, t.charges_minor, t.total_minor, t.paid_minor, t.balance_minor) == (1600, 600, 500, 2100, 200, 1900)
    assert t.unpriced_items == [unpriced.id]

# tests/orders/test_payment_requests.py
def test_a_request_for_the_balance_takes_total_minus_paid(shop: Shop) -> None:
    order, _ = shop.accepted()          # total 1000
    shop.lifecycle.add_payment(order.id, BY, amount_minor=300)
    req = shop.lifecycle.open_payment_request(order.id, BY)
    assert (req.kind, req.amount_minor, req.currency, req.provider, req.status) == ("balance", 700, "USD", None, "open")
    assert "payment_request.opened" in shop.kinds(order)

def test_nothing_due_refuses_a_balance_request(shop: Shop) -> None:
    # pay 1000 first
    with pytest.raises(LifecycleError) as e: shop.lifecycle.open_payment_request(order.id, BY)
    assert e.value.slug == "nothing_due"

def test_a_manual_payment_naming_the_request_closes_it(shop: Shop) -> None:
    pay = shop.lifecycle.add_payment(order.id, BY, amount_minor=700, method="cash", payment_request_id=req.id)
    assert pay.payment_request_id == req.id and pay.provider is None
    closed = shop.rows_request(req.id)
    assert closed.status == "paid" and closed.payment_id == pay.id

def test_a_manual_payment_that_clears_the_balance_closes_the_open_balance_request(shop: Shop) -> None:
    shop.lifecycle.add_payment(order.id, BY, amount_minor=700)      # names nothing
    assert shop.rows_request(req.id).status == "paid"

def test_a_payment_may_not_name_another_orders_or_a_closed_request(shop: Shop) -> None:
    # slugs payment_request_not_found (not_found True) and payment_request_not_open

def test_a_manual_payment_writes_no_fee(shop: Shop) -> None:
    assert shop.rows.cost_entries(order.id) == []

def test_a_paid_event_records_one_payment_and_its_fee(shop: Shop) -> None:
    # link req: set_request_link(req.id, hosted=HostedRequest(provider_ref="fake_r", hosted_url=...), error=None)
    out = shop.lifecycle.record_provider_event("fake", ProviderEvent(event_id="e1", kind="paid", provider_ref="fake_r", amount_minor=700, currency="USD", fee_minor=31))
    assert out == "recorded"
    [pay] = [p for p in shop.rows.payments(order.id) if p.provider == "fake"]
    assert (pay.amount_minor, pay.method, pay.reference, pay.payment_request_id) == (700, "fake", "fake_r", req.id)
    [fee] = shop.rows.cost_entries(order.id)
    assert (fee.kind, fee.amount_minor, fee.source) == ("payment_fee", 31, f"payment:{pay.id}")
    assert shop.rows.events(order.id)[-1].by == Actor(kind="provider", principal="payment:fake")

def test_a_replayed_event_records_nothing_more(shop: Shop) -> None:
    assert shop.lifecycle.record_provider_event("fake", EVENT) == "replayed"
    assert len(shop.rows.payments(order.id)) == 1 and len(shop.rows.cost_entries(order.id)) == 1

def test_a_refund_is_a_negative_payment_with_no_fee(shop: Shop) -> None:
    shop.lifecycle.record_provider_event("fake", ProviderEvent(event_id="e2", kind="refunded", provider_ref="fake_r", amount_minor=200, currency="USD", fee_minor=None))
    assert [p.amount_minor for p in shop.rows.payments(order.id)] == [700, -200]
    assert shop.rows_request(req.id).status == "paid"

def test_an_unknown_ref_is_recorded_as_unmatched(shop: Shop) -> None: ...   # outcome "unmatched", no payment
def test_another_currency_records_no_payment(shop: Shop) -> None: ...      # outcome "currency_mismatch"
def test_an_expired_event_expires_an_open_request(shop: Shop) -> None: ...

# Review Focus 1
def test_cancelling_the_order_cancels_its_open_requests(shop: Shop) -> None:
    shop.lifecycle.cancel_order(order.id, BY)
    assert shop.rows_request(req.id).status == "cancelled"

# Review Focus 2
def test_a_paid_event_after_the_cancel_is_still_recorded(shop: Shop) -> None:
    shop.lifecycle.cancel_order(order.id, BY)
    assert shop.lifecycle.record_provider_event("fake", EVENT) == "recorded"
    assert shop.rows_request(req.id).status == "paid" and len(shop.rows.cost_entries(order.id)) == 1
```

(`shop.rows_request(id)` is a two-line helper in the test module over `payments.store.request`.)

- [ ] **Step 2: Run them to verify they fail**

Run: `cd backend && uv run pytest tests/orders/test_totals.py tests/orders/test_payment_requests.py -v`
Expected: FAIL (no module `scadbuddy.orders.totals`, no `open_payment_request`).

- [ ] **Step 3: Move the totals** into `orders/totals.py` and point `api/orders.py` (`get_order`, `ItemDetail`) at them; `item_price` there keeps its name for the existing callers and takes `(item, item.quotes)`.

- [ ] **Step 4: Implement the lifecycle methods** in Interfaces. Each takes the order's row lock first; `record_provider_event` inserts the event row before it locks the order, inside the same transaction, so two deliveries of one event serialise on the unique index and the second sees `replayed`.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `cd backend && uv run pytest tests/orders tests/api/test_orders.py -v && uv run mypy`
Expected: PASS (the moved totals change no existing answer).

- [ ] **Step 6: Commit**

```bash
git add backend/scadbuddy/orders backend/scadbuddy/api/orders.py backend/tests/orders
git commit -m "feat(orders): payment requests, payments that close them, provider events and fees (#4957)"
```

---

### Task 4: The service, the component and the routes (webhook included), README

**Files:**
- Create: `backend/scadbuddy/payments/service.py`, `backend/scadbuddy/payments/component.py`, `backend/scadbuddy/api/order_payments.py`, `backend/scadbuddy/api/payments.py`, `backend/scadbuddy/api/payment_webhooks.py`
- Modify: `backend/scadbuddy/api/orders.py` (`PaymentCreate.payment_request_id`, `OrderDetail.payment_requests`), `backend/scadbuddy/auth/routes.py`, `README.md` ("Deploying"), `backend/CLAUDE.md` (a payments paragraph beside the orders one)
- Test: `backend/tests/api/test_order_payments.py`, `backend/tests/api/test_payment_webhooks.py`, `backend/tests/api/test_payment_providers.py`, `backend/tests/auth/test_routes.py`

**Interfaces:**
- Consumes: Tasks 1-3; `ORDERS` (`orders/component.py`); `api/components.py` `component_dep`; `orders/problems.py` `refused`; `orders/actor.py` `current_actor`.
- Produces (`payments/service.py`):
  - `class ProviderStatus(BaseModel)`: `id: str`, `label: str`, `fields: list[ProviderField]`, `connected: bool`, `account_label: str | None`, `is_default: bool`, `connected_at: datetime | None`, `last_webhook_at: datetime | None`.
  - `class ProviderConnect(BaseModel)`: `settings: dict[str, str]`, `secrets: dict[str, str]` (extra forbid; each value at most 2000 characters; a field the provider does not declare, or a missing one, is 422 naming it).
  - `class PaymentRequestCreate(BaseModel)`: `kind: PaymentRequestKind = "balance"`, `amount_minor: Money | None = Field(default=None, ge=1)`, `provider: str | None | UnsetType = UNSET`.
  - `class WebhookAnswer(BaseModel)`: `outcome: EventOutcome`.
  - `class Payments`: `__init__(self, orders: OrdersComponent, credentials: PaymentCredentials, registry: Mapping[str, PaymentProvider], public_url: Callable[[], str | None])`; `async request_payment(self, order_id: str, by: Actor, body: PaymentRequestCreate) -> PaymentRequestView` (open, then `link`); `async link(self, request_id: str) -> PaymentRequestView` (a request with no provider, or already linked, is returned as it is; `ProviderError` → `link_error`); `async link_pending(self, order_id: str) -> list[PaymentRequestView]` (every open request with a provider and no `hosted_url`); `async handle_webhook(self, provider: str, headers: Mapping[str, str], body: bytes) -> EventOutcome`; `async connect(self, provider: str, body: ProviderConnect) -> ProviderStatus`; `disconnect(self, provider: str) -> None`; `set_default(self, provider: str) -> None`; `statuses(self) -> list[ProviderStatus]`.
  - `ProviderNotFoundError` (unknown or not connected), `BadSignatureError` re-raised, `CredentialUnreadableError` re-raised.
  - The return URL passed to a provider is `f"{public_url}/orders/{order_id}"` when `public_url` is set, else None (phase 4 passes the storefront's order page).
- Produces (`payments/component.py`): `PAYMENTS: Key[PaymentsComponent]`; `PaymentsComponent.service: Payments`; `PaymentsDep`. Its build reads `components.get(ORDERS)`; the registry is `providers(fake=core.settings.payments_fake_provider)`; the KEK is `settings_kek(core.settings.secret_key_file)`.
- Produces (routes):
  - `POST /api/v1/orders/{order_id}/payment-requests` → 201 `PaymentRequestView` (`api/order_payments.py` `request_payment`); `GET /api/v1/orders/{order_id}/payment-requests` → `list[PaymentRequestView]` (`list_payment_requests`); `POST …/payment-requests/{request_id}/cancel` → `PaymentRequestView` (`cancel_payment_request`); `POST …/payment-requests/{request_id}/link` → `PaymentRequestView` (`link_payment_request`). An unknown `provider` in the body is 422 `provider`.
  - `POST /api/v1/orders/{order_id}/payments` takes `payment_request_id: str | None = None`.
  - `OrderDetail.payment_requests: list[PaymentRequestView]` (oldest first).
  - `GET /api/v1/payments/providers` → `list[ProviderStatus]` (`api/payments.py` `list_providers`); `PUT /api/v1/payments/providers/{provider}` → `ProviderStatus` (`connect_provider`; 409 `kek_missing`; 422 with the provider's `ProviderError` text when `verify` fails); `DELETE /api/v1/payments/providers/{provider}` → 204 (`disconnect_provider`; 409 `provider_has_open_requests`); `POST /api/v1/payments/providers/{provider}/default` → 204 (`default_provider`). Problem types `https://scadbuddy.dev/problems/payments/<slug>`.
  - `POST /api/v1/payments/webhooks/{provider}` (`api/payment_webhooks.py` `receive_webhook`): reads the raw body (more than 256 KiB is 413 before parsing); 404 for an unknown or not connected provider; 400 for a bad signature, recording nothing; 503 when the credential will not open; 200 `WebhookAnswer` otherwise, and `touch_webhook` on every signed event.
  - `auth/routes.py`: `"scadbuddy.api.payment_webhooks.receive_webhook"` in `PUBLIC_ENDPOINTS`; `connect_provider`, `disconnect_provider`, `default_provider` in `PERSON_ENDPOINTS`.

- [ ] **Step 1: Write the failing route tests**

```python
# tests/api/test_payment_webhooks.py (app with SCADBUDDY_PAYMENTS_FAKE_PROVIDER=true and a KEK file)
def test_the_fake_provider_drives_a_request_through_paid_and_refunded(client: TestClient) -> None:
    client.put("/api/v1/payments/providers/fake", json={"settings": {"account": "a"}, "secrets": {"signing_secret": "s3cret"}})
    req = client.post(f"/api/v1/orders/{order_id}/payment-requests", json={}).json()
    assert req["provider"] == "fake" and req["hosted_url"].startswith("https://payments.fake.invalid/pay/")
    body, headers = signed_event("s3cret", {"id": "e1", "type": "paid", "provider_ref": req["provider_ref"], "amount_minor": req["amount_minor"], "currency": "USD", "fee_minor": 31})
    assert client.post("/api/v1/payments/webhooks/fake", content=body, headers=headers).json() == {"outcome": "recorded"}
    detail = client.get(f"/api/v1/orders/{order_id}").json()
    assert detail["payment_requests"][0]["status"] == "paid" and detail["totals"]["balance_minor"] == 0
    body, headers = signed_event("s3cret", {"id": "e2", "type": "refunded", "provider_ref": req["provider_ref"], "amount_minor": 400, "currency": "USD"})
    client.post("/api/v1/payments/webhooks/fake", content=body, headers=headers)
    assert [p["amount_minor"] for p in client.get(f"/api/v1/orders/{order_id}").json()["payments"]] == [req["amount_minor"], -400]

def test_a_bad_signature_is_400_and_records_nothing(client: TestClient, pool: PgPool) -> None:
    body, headers = signed_event("wrong", {...paid...})
    assert client.post("/api/v1/payments/webhooks/fake", content=body, headers=headers).status_code == 400
    assert pool.connection().execute("SELECT count(*) AS n FROM payment_events").fetchone()["n"] == 0
    assert client.get(f"/api/v1/orders/{order_id}").json()["payments"] == []

def test_a_replayed_event_records_one_payment(client: TestClient) -> None:
    # post the same signed body twice
    assert [r.json()["outcome"] for r in (first, second)] == ["recorded", "replayed"]
    assert len(client.get(f"/api/v1/orders/{order_id}").json()["payments"]) == 1

def test_the_webhook_needs_no_session_in_enforce(enforce_client: TestClient) -> None:
    # auth mode enforce, no cookie, no x-scadbuddy-request header
    assert enforce_client.post("/api/v1/payments/webhooks/fake", content=body, headers=headers).status_code == 200

def test_an_unknown_provider_is_404_and_a_huge_body_413(client: TestClient) -> None: ...

# tests/api/test_order_payments.py
def test_a_manual_payment_names_an_open_request(client) -> None:
    r = client.post(f"/api/v1/orders/{order_id}/payments", json={"amount_minor": 1000, "method": "cash", "payment_request_id": req_id})
    assert r.status_code == 201 and r.json()["payment_request_id"] == req_id
def test_naming_a_closed_request_is_409(client) -> None:   # type .../orders/payment_request_not_open
def test_with_no_provider_the_request_has_no_link(client) -> None:   # provider None, hosted_url None
def test_a_failed_link_is_kept_and_retried(client, monkeypatch) -> None:
    # make FAKE.create_request raise ProviderError("down") once: link_error == "down", status open; then POST …/link fills hosted_url

# tests/api/test_payment_providers.py
def test_the_page_never_sees_a_secret(client) -> None:
    status = client.get("/api/v1/payments/providers").json()[0]
    assert status["account_label"] == "Fake account a" and "s3cret" not in json.dumps(status)
def test_connecting_without_a_kek_is_409(client_without_kek) -> None:   # type .../payments/kek_missing
def test_an_agent_cannot_connect_a_provider(client) -> None:   # author header of an agent: 403 in enforce (PERSON_ENDPOINTS)
def test_disconnect_with_an_open_request_is_409(client) -> None:   # type .../payments/provider_has_open_requests

# tests/auth/test_routes.py
def test_only_the_webhook_is_public_among_payments_routes(app) -> None:
    classes = {endpoint_name(r): route_class(r) for r in app.routes if "payment" in r.path}
    assert [n for n, c in classes.items() if c == "public"] == ["scadbuddy.api.payment_webhooks.receive_webhook"]
```

- [ ] **Step 2: Run them to verify they fail**

Run: `cd backend && uv run pytest tests/api/test_order_payments.py tests/api/test_payment_webhooks.py tests/api/test_payment_providers.py tests/auth/test_routes.py -v`
Expected: FAIL (404 for the new routes).

- [ ] **Step 3: Implement** the service, the component and the three route modules. Every lifecycle call goes through `asyncio.to_thread` and maps `LifecycleError` with `refused`. The webhook route reads `await request.body()` itself (no Pydantic body), lower-cases header names, and never logs the body or the headers.

- [ ] **Step 4: Write the README note** under "Deploying", a "Payment webhooks" paragraph: the front door (Pomerium, the LAN gateway) stays in front of every route but one; route only the path prefix `/api/v1/payments/webhooks/` from the public host to the API Service, with no other path, and name the provider's dashboard as where the webhook URL `https://<public host>/api/v1/payments/webhooks/<provider>` is entered. Add the `backend/CLAUDE.md` payments paragraph: the package's files, the tables, the rule that a provider is called only after commit, and the fee entry.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `cd backend && uv run pytest tests/api tests/auth tests/payments tests/orders -n auto && uv run mypy && uv run ruff check . && uv run ruff format --check .`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add backend/scadbuddy README.md backend/CLAUDE.md backend/tests
git commit -m "feat(payments): request payment, provider settings and the signed webhook route (#4957)"
```

---

### Task 5: Agent tools for payments

**Files:**
- Modify: `agent/src/tools/orders.ts`, `agent/src/tools/coverage.ts`
- Test: `agent/test/orders.test.ts`, `agent/test/toolTier.test.ts`

**Interfaces:**
- Consumes: Task 4's routes (through the generated client).
- Produces:
  - `order_request_payment` (`outward`; routes `POST /api/v1/orders/{order_id}/payment-requests`): input `{order_id, kind?: 'deposit' | 'balance', amount_minor?: money (>= 1), provider?: string | null}`; returns the request with its `hosted_url` and `link_error`; description says it sends the customer something to pay and that the link is for the shop to pass on.
  - `order_payment_requests` (`read`; route `GET /api/v1/orders/{order_id}/payment-requests`): input `{order_id}`.
  - `order_update`'s `payment` object gains `payment_request_id?: ID`.
  - `NOT_A_TOOL` entries, each with its reason: `POST /api/v1/payments/webhooks/{provider}` (a provider's call, never an agent's), the four `/api/v1/payments/providers…` operations (Settings → Payments, a person's), `POST …/payment-requests/{request_id}/cancel` and `…/link` (the order page's).

- [ ] **Step 1: Write the failing tests** in `agent/test/orders.test.ts`

```ts
it('requests a payment as an outward step, and reads the requests (#4957)', async () => {
  let sent: unknown
  server.use(http.post(`${BACKEND}/api/v1/orders/${ORDER}/payment-requests`, async ({ request }) => { sent = await request.json(); return HttpResponse.json(REQUEST, { status: 201 }) }))
  expect(tool('order_request_payment').risk).toBe('outward')
  expect(tool('order_payment_requests').risk).toBe('read')
  await tool('order_request_payment').handler({ order_id: ORDER, kind: 'deposit', amount_minor: 500 }, ctx())
  expect(sent).toEqual({ kind: 'deposit', amount_minor: 500 })
})
it('records a payment that closes a request', async () => { /* order_update payment.payment_request_id reaches the body */ })
```

Add both names to the tier list in `tiers each as plan Ruling 14 does` and in `toolTier.test.ts`.

- [ ] **Step 2: Run them to verify they fail**

Run: `cd agent && pnpm gen:api && pnpm exec vitest run test/orders.test.ts test/coverage.test.ts test/toolTier.test.ts`
Expected: FAIL (`no tool order_request_payment`; coverage names the uncovered operations).

- [ ] **Step 3: Implement** the tools and the coverage entries in Interfaces.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd agent && pnpm lint && pnpm typecheck && pnpm exec vitest run test/orders.test.ts test/coverage.test.ts test/toolTier.test.ts`
Expected: PASS.

- [ ] **Step 5: Commit and open PR A1**

```bash
git add agent/src/tools/orders.ts agent/src/tools/coverage.ts agent/test/orders.test.ts agent/test/toolTier.test.ts
git commit -m "feat(agent): order_request_payment and order_payment_requests (#4957)"
```

PR title `feat(payments): the payment-provider seam (#4957)`, body `Part of #4957` (A2 closes it).

---

## PR A2 (#4957): the back office's payments UI

### Task 6: Settings → Payments, "Request payment" and the payment rows

**Files:**
- Create: `frontend/src/pages/settings/sections/payments.tsx` and `payments.test.tsx`, `frontend/src/components/orders/PaymentRequests.tsx` and `PaymentRequests.test.tsx`, `frontend/src/mocks/features/payments.ts`
- Modify: `frontend/src/api/types.ts`, `frontend/src/api/client.ts`, `frontend/src/mocks/features/orders/state.ts` (`paymentRequests`), `frontend/src/mocks/features/orders/orders.ts` (`OrderDetail.payment_requests`, the payment route's `payment_request_id`), `frontend/src/pages/OrderPage.tsx` (the Money card), `frontend/src/pages/OrderPage.test.tsx`

**Interfaces:**
- Consumes: Task 4's routes and shapes.
- Produces:
  - `types.ts`: `PaymentRequestView`, `ProviderStatus`, `ProviderConnect`, `PaymentRequestCreate` from the schema.
  - `client.ts`: `paymentProviders()`, `connectPaymentProvider(id: string, body: ProviderConnect)`, `disconnectPaymentProvider(id: string)`, `setDefaultPaymentProvider(id: string)`, `requestPayment(orderId: string, body: PaymentRequestCreate)`, `cancelPaymentRequest(orderId: string, requestId: string)`, `linkPaymentRequest(orderId: string, requestId: string)`.
  - `mocks/features/payments.ts`: the provider routes with one provider `fake` (fields `account`, `signing_secret`), never echoing a secret; `connect` answers `account_label: "Fake account <account>"`; the mock's KEK flag (`setMockKek(false)`) answers 409 `kek_missing`; disconnect with an open request answers 409 `provider_has_open_requests`; and `mockPaymentEvent(orderId, requestId, kind)` for tests, which applies the backend's `record_provider_event` rules (paid, refunded, a fee entry).
  - The orders mock mirrors Task 3: a balance request takes total minus paid, `nothing_due`, a named request closes as paid, a payment that clears the balance closes the open balance request, and a cancel cancels the open requests.
  - `PaymentRequests({ order, requests, onChanged })`: a "Request payment" button (amount defaults to the balance; kind; provider when more than one is connected), the list of requests with status, amount, provider and a "Copy link" button and a `mailto:` link when `hosted_url` is set, the instructions "Record the payment below when it arrives" when it is not, "Retry link" when `link_error` is set, and "Cancel" for an open one.
  - The payment form gains a "For request" select of the open requests; payment rows show `provider` ("Paid by Fake") when set.

- [ ] **Step 1: Write the failing tests**

```tsx
// PaymentRequests.test.tsx
it('requests the balance and shows the link to copy', async () => {
  // connected fake provider; order total 1000, paid 300
  await user.click(screen.getByRole('button', { name: 'Request payment' }))
  await user.click(screen.getByRole('button', { name: 'Request' }))
  expect(await screen.findByText('$7.00')).toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Copy link' })).toBeEnabled()
  expect(screen.getByRole('link', { name: 'Email' })).toHaveAttribute('href', expect.stringMatching(/^mailto:/))
})
it('shows a paid request with the provider after its webhook', async () => {
  mockPaymentEvent(order.id, request.id, 'paid')
  expect(await screen.findByText('Paid')).toBeInTheDocument()
})
it('shows nothing_due as the backend says it', async () => { /* refusal text */ })
it('offers Retry link on a link error', async () => { /* link_error set */ })

// payments.test.tsx
it('connects a provider and never shows its secret again', async () => {
  // type account "a" and secret "s3cret", Save
  expect(await screen.findByText('Connected as Fake account a')).toBeInTheDocument()
  expect(screen.queryByDisplayValue('s3cret')).not.toBeInTheDocument()
})
it('says the key file is missing on kek_missing', async () => { setMockKek(false) /* ... */ })

// OrderPage.test.tsx
it('records a payment for an open request and closes it', async () => { /* "For request" select */ })
```

- [ ] **Step 2: Run them to verify they fail**

Run: `cd frontend && pnpm gen:api && pnpm exec vitest run src/components/orders/PaymentRequests.test.tsx src/pages/settings/sections/payments.test.tsx src/pages/OrderPage.test.tsx`
Expected: FAIL (no module `PaymentRequests`).

- [ ] **Step 3: Implement** the mocks, the client calls, the section (area `system`, title "Payments", order after the existing sections; shows "Last webhook …" with `When`) and the component; mount `PaymentRequests` in the Money card under the payments list.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd frontend && pnpm lint && pnpm typecheck && pnpm test`
Expected: PASS.

- [ ] **Step 5: Commit and open PR A2**

```bash
git add frontend/src
git commit -m "feat(payments): Settings → Payments and Request payment on the order page (#4957)"
```

PR title `feat(payments): request payment and connect providers in the back office (#4957)`, body `Fixes #4957`.

---

## PR B1 (#5281): deposits, the balance, shipping options and auto tax

### Task 7: Schema, settings and the money rules

**Files:**
- Create: `backend/scadbuddy/migrations/<ts>_order_deposits_tax.sql`, `backend/scadbuddy/orders/money.py`
- Modify: `backend/scadbuddy/orders/models.py` (`Order`, `ChargeInput`, `Charge`), `backend/scadbuddy/orders/store.py` (`_ORDER`, `_CHARGE`, `replace_charges`, new setters), `backend/scadbuddy/core/settings.py`, `backend/scadbuddy/library/settings_store.py`, `backend/scadbuddy/api/settings.py`, `backend/scadbuddy/orders/component.py` (`rules`), `backend/scadbuddy/orders/lifecycle.py` (constructor)
- Test: `backend/tests/test_pg_migrations.py`, `backend/tests/orders/test_money.py`, `backend/tests/api/test_settings.py`

**Interfaces:**
- Produces (`orders/money.py`):
  - `DepositCriterion = Literal["has_request", "total_above", "first_order"]`.
  - `class DepositRule(BaseModel)` (extra forbid): `criterion: DepositCriterion`, `above_minor: Money | None = Field(default=None, ge=0)`, `percent: Rate | None = None` (above 0, at most 100), `amount_minor: Money | None = Field(default=None, ge=1)`; a model validator: exactly one of `percent` and `amount_minor`; `above_minor` set iff `criterion == "total_above"`.
  - `class ShippingOption(BaseModel)` (extra forbid): `id: str = Field(pattern=r"^[a-z0-9_-]{1,40}$")`, `label: str = Field(min_length=1, max_length=200)`, `amount_minor: Money = Field(ge=0)`, `ships: bool`.
  - `@dataclass(frozen=True) class OrderRules`: `shop_currency: str`, `tax_percent: Decimal`, `tax_shipping: bool`, `deposit_policy: tuple[DepositRule, ...]`, `shipping_options: tuple[ShippingOption, ...]`. `NO_RULES = OrderRules("USD", Decimal(0), False, (), ())`.
  - `def tax_minor(base_minor: Money, percent: Decimal) -> Money` (`base * percent / 100`, half up, once).
  - `def tax_base(items_minor: Money, charges: Sequence[ChargeInput], *, tax_shipping: bool) -> Money` (items plus `shipping` charges when `tax_shipping`).
  - `def deposit_for(policy: Sequence[DepositRule], *, total_minor: Money, has_request: bool, first_order: bool, currency: str, shop_currency: str) -> Money | None` (the largest positive amount among the rules that apply; a percent of `total_minor`, half up; a fixed amount or `total_above` only when `currency == shop_currency`; None when none applies).
- Produces (models/store): `Order` gains `deposit_minor: Money | None = None`, `deposit_waived_reason: str | None = None`, `balance_waived_reason: str | None = None`. `ChargeInput` gains `shipping_option_id: str | None = None`; `Charge` gains `auto: bool = False`. `OrderStore.replace_charges` deletes and rewrites only rows `NOT auto`. New: `OrderStore.set_auto_tax(conn, order_id: str, *, label: str, amount_minor: Money | None) -> None` (None deletes the row); `OrderStore.set_deposit_fields(conn, order_id: str, *, deposit_minor: Money | None | UnsetType = UNSET, deposit_waived_reason: str | None | UnsetType = UNSET, balance_waived_reason: str | None | UnsetType = UNSET) -> Order`; `OrderStore.accepted_before(conn, customer_id: str, order_id: str) -> bool` (another order of the customer with `accepted_at` set).
- Produces (settings): `Settings.orders_tax_percent: str = "0"` (validator: `decimal_string`, below 100), `Settings.orders_tax_shipping: bool = False`, both `"live"` in `APPLIES`; `StoredSettings` / `SettingsPatch` / `api/settings.py` view gain those two and `orders_deposit_policy: list[DepositRule] | None` (max 20), `storefront_shipping_options: list[ShippingOption] | None` (max 20; duplicate ids are 422), `storefront_payment_instructions: str | None` (max 2000).
- Produces (`Lifecycle.__init__`): `rules: Callable[[], OrderRules] = lambda: NO_RULES`; `OrdersComponent.lifecycle` passes a reader of `core.settings_store.snapshot()`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/orders/test_money.py
def test_tax_rounds_once_half_up() -> None:
    assert tax_minor(1005, Decimal("10")) == 101 and tax_minor(1004, Decimal("10")) == 100 and tax_minor(999, Decimal("0")) == 0

def test_shipping_is_taxed_only_when_asked() -> None:
    charges = [ChargeInput(kind="shipping", label="Post", amount_minor=500), ChargeInput(kind="discount", label="-", amount_minor=-200)]
    assert tax_base(1000, charges, tax_shipping=False) == 1000 and tax_base(1000, charges, tax_shipping=True) == 1500

@pytest.mark.parametrize(("rules", "ctx", "want"), [
    ([DepositRule(criterion="has_request", amount_minor=2000)], {"has_request": True}, 2000),
    ([DepositRule(criterion="has_request", amount_minor=2000)], {"has_request": False}, None),
    ([DepositRule(criterion="total_above", above_minor=10000, percent="50")], {"total_minor": 10001}, 5001),
    ([DepositRule(criterion="total_above", above_minor=10000, percent="50")], {"total_minor": 10000}, None),
    ([DepositRule(criterion="first_order", percent="25")], {"first_order": True, "total_minor": 999}, 250),
    ([DepositRule(criterion="first_order", amount_minor=500), DepositRule(criterion="has_request", amount_minor=800)], {"first_order": True, "has_request": True}, 800),
    ([DepositRule(criterion="first_order", amount_minor=500)], {"first_order": True, "currency": "EUR"}, None),
])
def test_the_largest_rule_that_applies_wins(rules, ctx, want) -> None: ...

def test_a_rule_needs_one_amount() -> None:
    with pytest.raises(ValidationError): DepositRule(criterion="first_order", percent="10", amount_minor=5)
    with pytest.raises(ValidationError): DepositRule(criterion="total_above", amount_minor=5)

# tests/test_pg_migrations.py
def test_the_deposits_tax_migration(pg_conninfo: str) -> None:
    # an auto charge must be tax; two auto rows on one order are refused (unique index);
    # deposit_minor 0 and a 501-character reason are refused (CHECK)

# tests/api/test_settings.py
def test_the_money_settings_round_trip(client) -> None:
    client.patch("/api/v1/settings", json={"orders_tax_percent": "8.25", "orders_tax_shipping": True,
        "orders_deposit_policy": [{"criterion": "has_request", "amount_minor": 2000}],
        "storefront_shipping_options": [{"id": "pickup", "label": "Pick up", "amount_minor": 0, "ships": False}],
        "storefront_payment_instructions": "Venmo @shop"})
    got = client.get("/api/v1/settings").json()
    assert got["orders_tax_percent"] == "8.25" and got["storefront_shipping_options"][0]["ships"] is False
def test_a_tax_percent_of_100_is_refused(client) -> None: ...      # 422 orders_tax_percent
def test_duplicate_option_ids_are_refused(client) -> None: ...     # 422 storefront_shipping_options
```

- [ ] **Step 2: Run them to verify they fail**

Run: `cd backend && uv run pytest tests/orders/test_money.py tests/test_pg_migrations.py::test_the_deposits_tax_migration tests/api/test_settings.py -k "money or deposits_tax or tax_percent or option_ids" -v`
Expected: FAIL (no module `scadbuddy.orders.money`).

- [ ] **Step 3: Write the migration**: `orders` `ADD COLUMN deposit_minor bigint CHECK (deposit_minor > 0)`, `ADD COLUMN deposit_waived_reason text CHECK (char_length(deposit_waived_reason) BETWEEN 1 AND 500)`, `ADD COLUMN balance_waived_reason text CHECK (char_length(balance_waived_reason) BETWEEN 1 AND 500)`; `order_charges` `ADD COLUMN auto boolean NOT NULL DEFAULT false`, `ADD COLUMN shipping_option_id text`, `CHECK (NOT auto OR kind = 'tax')`, `CHECK (shipping_option_id IS NULL OR kind = 'shipping')`; `CREATE UNIQUE INDEX order_charges_auto_tax ON order_charges (order_id) WHERE auto`.

- [ ] **Step 4: Implement** the module, the model fields, the store methods and the settings in Interfaces.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `cd backend && uv run pytest tests/orders tests/test_pg_migrations.py tests/api/test_settings.py tests/api/test_settings_runtime.py -n auto && uv run mypy`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add backend/scadbuddy backend/tests
git commit -m "feat(orders): deposit, waiver and auto-tax columns; the money settings (#5281)"
```

---

### Task 8: Auto tax and shipping options

**Files:**
- Modify: `backend/scadbuddy/orders/lifecycle.py`
- Test: `backend/tests/orders/test_auto_tax.py`, `backend/tests/orders/test_shipping_options.py`

**Interfaces:**
- Consumes: Task 7's `OrderRules`, `tax_minor`, `tax_base`, `set_auto_tax`; Task 3's `totals_of`.
- Produces:
  - `Lifecycle._retax(self, conn: Conn, order: Order) -> None`: when the order is `draft`, `quoted`, `accepted`, `in_production` or `ready`, writes the `auto` row: none when `tax_percent` is 0, when the order has a `tax` charge that is not `auto`, or when the amount is 0; else label `f"Tax {percent}%"` and `tax_minor(tax_base(items, charges, tax_shipping), percent)`, items from `totals_of` (`subtotal_minor`). Called last, before `_rederive`, in each of: `_added` (every add, before and after acceptance: `add_item`, `add_request_item`, `add_library_item`, `add_custom_item`), `update_item`, `delete_item`, `cancel_item`, `reopen_item`, `reopen_order`, `record_manual_quote`, `record_slice_quote`, `accept_quote`, `_revise`, `edit_order` (charges), `replace_charges_after_acceptance`, `set_shipping_option`.
  - A `ChargeInput` cannot set `auto` (it has no such field; extra forbid makes it 422).
  - `Lifecycle.set_shipping_option(self, order_id: str, by: Actor, option_id: str | None) -> Order`: `draft` or `quoted` only (`order_not_editable` past it, naming `PUT …/charges/after-acceptance`); an id not in `rules().shipping_options` is 422 `option_id` (slug `shipping_option_not_found`, `unprocessable`); an option that ships with no `shipping_address_id` is `shipping_address_required`; replaces every `shipping` charge with one `ChargeInput(kind="shipping", label=option.label, amount_minor=option.amount_minor, shipping_option_id=option.id)`; None removes the `shipping` charges. Audit `shipping.option_set` (`option_id`).

- [ ] **Step 1: Write the failing tests**

```python
# tests/orders/test_auto_tax.py (Shop built with rules=lambda: OrderRules("USD", Decimal("10"), False, (), ()))
def tax(shop, order) -> list[tuple[int, bool]]:
    return [(c.amount_minor, c.auto) for c in shop.rows.charges(order.id) if c.kind == "tax"]

def test_tax_follows_a_quote_its_acceptance_and_a_quantity_change(shop: Shop) -> None:
    order = shop.order(); item = shop.item(order)
    assert tax(shop, order) == []
    shop.quoted(order, item, price=1005)
    assert tax(shop, order) == [(101, True)]
    shop.lifecycle.update_item(order.id, item.id, BY, quantity=2)    # drops to draft; newest quote x 2
    assert tax(shop, order) == [(201, True)]

@pytest.mark.parametrize("transition", ["add_custom_item", "cancel_item", "reopen_item", "delete_item", "edit_order_charges", "replace_charges_after_acceptance", "add_item_after_acceptance=True", "add_revision", "set_shipping_option"])
def test_every_change_of_items_quotes_or_charges_retaxes(shop: Shop, transition: str) -> None:
    # each case changes the taxable total; assert tax == tax_minor(new base, 10)

def test_shipping_is_in_the_base_only_with_orders_tax_shipping(shop_taxing_shipping: Shop) -> None: ...

def test_no_tax_at_zero_percent_and_none_once_shipped(shop: Shop) -> None: ...

# Review Focus 4
def test_a_typed_tax_charge_replaces_the_auto_one(shop: Shop) -> None:
    shop.lifecycle.edit_order(order.id, BY, OrderEdit(charges=[ChargeInput(kind="tax", label="VAT", amount_minor=77)]))
    assert tax(shop, order) == [(77, False)]
    shop.quoted(order, shop.item(order), price=5000)
    assert tax(shop, order) == [(77, False)]

# Review Focus 5
def test_replacing_the_charges_keeps_and_recomputes_the_auto_row(shop: Shop) -> None:
    shop.lifecycle.edit_order(order.id, BY, OrderEdit(charges=[ChargeInput(kind="fee", label="Rush", amount_minor=100)]))
    assert tax(shop, order) == [(101, True)]
def test_a_body_cannot_set_auto(client) -> None:
    r = client.patch(f"/api/v1/orders/{order_id}", json={"charges": [{"kind": "tax", "label": "x", "amount_minor": 1, "auto": True}]})
    assert r.status_code == 422

# tests/orders/test_shipping_options.py (rules with POST {id "post", 500, ships True} and PICKUP {id "pickup", 0, ships False})
def test_an_option_writes_the_one_shipping_charge(shop: Shop) -> None:
    shop.lifecycle.set_shipping_option(order.id, BY, "post")
    assert [(c.kind, c.label, c.amount_minor, c.shipping_option_id) for c in shop.rows.charges(order.id)] == [("shipping", "Post", 500, "post")]
def test_a_pickup_needs_no_address(shop_without_address: Shop) -> None:
    shop.lifecycle.set_shipping_option(order.id, BY, "pickup")          # no refusal
def test_an_option_that_ships_needs_an_address(shop_without_address: Shop) -> None:   # slug shipping_address_required
def test_an_unknown_option_is_422(shop: Shop) -> None:   # slug shipping_option_not_found, unprocessable
def test_options_close_with_the_charges_after_quoted(shop: Shop) -> None:   # order_not_editable once accepted
```

- [ ] **Step 2: Run them to verify they fail**

Run: `cd backend && uv run pytest tests/orders/test_auto_tax.py tests/orders/test_shipping_options.py -v`
Expected: FAIL (no tax row; no `set_shipping_option`).

- [ ] **Step 3: Implement** `_retax`, its call sites and `set_shipping_option`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd backend && uv run pytest tests/orders tests/api/test_orders.py tests/api/test_order_charges_after_acceptance.py -n auto && uv run mypy`
Expected: PASS (at 0 percent, the default, no existing test sees a tax row).

- [ ] **Step 5: Commit**

```bash
git add backend/scadbuddy/orders/lifecycle.py backend/tests/orders
git commit -m "feat(orders): an auto tax charge and shipping options (#5281)"
```

---

### Task 9: Deposits: set, policy, the deposit request and deposit_unpaid

**Files:**
- Modify: `backend/scadbuddy/orders/lifecycle.py` (`send_revision`, `_claim_test_print`, `add_payment`, new methods)
- Test: `backend/tests/orders/test_deposits.py`

**Interfaces:**
- Consumes: Task 3's `open_payment_request` internals (`payments.store.insert_request`, `default_provider`); Task 7's `deposit_for`, `set_deposit_fields`, `accepted_before`.
- Produces:
  - `Lifecycle.set_deposit(self, order_id: str, by: Actor, amount_minor: Money | None) -> Order`: `draft` or `quoted` only (`order_not_editable`); `amount_minor` below 1 is 422 `amount_minor`; cancels the open `deposit` request; sets `deposit_minor` and clears `deposit_waived_reason`; when `amount_minor - paid > 0`, opens a `deposit` request for that, on the default provider. Audit `deposit.set` (`amount_minor`).
  - `Lifecycle.apply_deposit_policy(self, order_id: str, by: Actor) -> Order`: `set_deposit` with `deposit_for(rules().deposit_policy, total_minor=totals.total_minor, has_request=<an item of kind request not cancelled>, first_order=not accepted_before(...), currency=order.currency, shop_currency=rules().shop_currency)`. Audit `deposit.set` with `policy: true`.
  - `Lifecycle.waive_deposit(self, order_id: str, by: Actor, reason: str) -> Order`: "Proceed without deposit"; refused `order_closed`/`order_cancelled`; reason trimmed, 1-500 (422 `reason`); the open deposit request stays open. Audit `deposit.waived` (`reason`).
  - `Lifecycle._deposit_paid(self, conn: Conn, order: Order) -> None`: raises `LifecycleError("deposit_unpaid", …)` when `deposit_minor` is set, `deposit_waived_reason` is null and the payments sum is below `deposit_minor`.
  - `send_revision` calls `_deposit_paid` after its "already sent" return and before it stamps; `_claim_test_print` calls it after `_test_printable` (a retried claim of an existing attempt is not refused).
  - `add_payment` (Task 3) now closes an open `deposit` request once payments reach `deposit_minor`.

- [ ] **Step 1: Write the failing tests**

```python
def test_a_set_deposit_opens_a_deposit_request(shop: Shop) -> None:
    order = shop.lifecycle.set_deposit(shop.order().id, BY, 2000)
    [req] = requests_for(shop, order)
    assert (order.deposit_minor, req.kind, req.amount_minor, req.status) == (2000, "deposit", 2000, "open")

def test_changing_the_deposit_cancels_the_old_request(shop: Shop) -> None:
    shop.lifecycle.set_deposit(order.id, BY, 2000); shop.lifecycle.set_deposit(order.id, BY, 3000)
    assert [(r.amount_minor, r.status) for r in requests_for(shop, order)] == [(2000, "cancelled"), (3000, "open")]

def test_clearing_the_deposit_cancels_its_request(shop: Shop) -> None: ...
def test_the_deposit_is_fixed_once_accepted(shop: Shop) -> None:   # order_not_editable

def test_an_unpaid_deposit_refuses_a_send(shop: Shop) -> None:
    # request item with a revision and a render attachment; deposit 2000
    with pytest.raises(LifecycleError) as e: shop.lifecycle.send_revision(order.id, item.id, rev.id, BY)
    assert e.value.slug == "deposit_unpaid"

def test_an_unpaid_deposit_refuses_a_test_print(shop: Shop) -> None:   # claim_print(..., revision_id=rev.id) → deposit_unpaid

# Review Focus 3
def test_a_cash_payment_for_the_deposit_closes_its_request_and_lifts_the_rule(shop: Shop) -> None:
    shop.lifecycle.add_payment(order.id, BY, amount_minor=2000, method="cash")
    assert requests_for(shop, order)[0].status == "paid"
    assert shop.lifecycle.send_revision(order.id, item.id, rev.id, BY).sent_at is not None

def test_proceed_without_deposit_needs_a_reason_and_lifts_the_rule(shop: Shop) -> None:
    with pytest.raises(LifecycleError) as e: shop.lifecycle.waive_deposit(order.id, BY, "  ")
    assert (e.value.slug, e.value.unprocessable) == ("reason", True)
    order = shop.lifecycle.waive_deposit(order.id, BY, "regular customer")
    assert order.deposit_waived_reason == "regular customer"
    assert shop.rows.events(order.id)[-1].detail == {"reason": "regular customer"}
    shop.lifecycle.send_revision(order.id, item.id, rev.id, BY)   # no refusal

@pytest.mark.parametrize(("policy", "setup", "want"), [
    ([has_request_2000], "request_item", 2000),
    ([total_above_10000_50pct], "item_quoted_20000", 10000),
    ([first_order_25pct], "first_order_quoted_1000", 250),
    ([first_order_25pct], "second_order_after_an_accepted_one", None),
])
def test_the_policy_sets_the_largest_deposit_that_applies(shop_with_policy, policy, setup, want) -> None: ...
```

- [ ] **Step 2: Run them to verify they fail**

Run: `cd backend && uv run pytest tests/orders/test_deposits.py -v`
Expected: FAIL (no `set_deposit`).

- [ ] **Step 3: Implement** the methods and the two checks in Interfaces.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd backend && uv run pytest tests/orders -n auto && uv run mypy`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/scadbuddy/orders/lifecycle.py backend/tests/orders/test_deposits.py
git commit -m "feat(orders): deposits, the deposit policy and deposit_unpaid (#5281)"
```

---

### Task 10: The balance: a request at accept, balance_unpaid and waive_balance

**Files:**
- Modify: `backend/scadbuddy/orders/lifecycle.py` (`accept_order`, `add_shipment`, new method)
- Test: `backend/tests/orders/test_balance.py`

**Interfaces:**
- Consumes: Tasks 3 and 9.
- Produces:
  - `accept_order` opens a `balance` request for `total - paid` on the default provider, in its transaction, when that is above 0 and the order has `deposit_minor` set or any payment request (gap 1). Audit `payment_request.opened`.
  - `Lifecycle._balance_paid(self, conn: Conn, order: Order) -> None`: raises `LifecycleError("balance_unpaid", …)` when the order has a `balance` request not `cancelled`, `balance_waived_reason` is null and `totals.balance_minor > 0`. `add_shipment` calls it after `_not_on_hold`.
  - `Lifecycle.waive_balance(self, order_id: str, by: Actor, reason: str) -> Order`: "Ship without full payment"; refused `order_closed`/`order_cancelled` and before `accepted` (`order_not_accepted`); reason as `waive_deposit`. Audit `balance.waived` (`reason`).

- [ ] **Step 1: Write the failing tests**

```python
def test_accepting_an_order_with_a_deposit_opens_the_balance_request(shop: Shop) -> None:
    # quoted 1000, deposit 300 paid in cash
    order = shop.lifecycle.accept_order(order.id, BY)
    assert [(r.kind, r.amount_minor, r.status) for r in requests_for(shop, order)][-1] == ("balance", 700, "open")

def test_a_back_office_order_with_no_request_ships_as_today(shop: Shop) -> None:
    order, item = shop.accepted(); shop.fulfilled(order, item)
    assert requests_for(shop, order) == []
    shop.shipped(order, item)                                   # no refusal, nothing paid

def test_an_unpaid_balance_refuses_a_shipment_and_a_pickup(shop: Shop) -> None:
    with pytest.raises(LifecycleError) as e: shop.shipped(order, item)
    assert e.value.slug == "balance_unpaid"

def test_paying_the_balance_lets_it_ship(shop: Shop) -> None:
    shop.lifecycle.add_payment(order.id, BY, amount_minor=700, payment_request_id=bal.id)
    shop.shipped(order, item)

def test_ship_without_full_payment_needs_a_reason(shop: Shop) -> None:
    order = shop.lifecycle.waive_balance(order.id, BY, "pays on pickup")
    assert order.balance_waived_reason == "pays on pickup" and "balance.waived" in shop.kinds(order)
    shop.shipped(order, item)

def test_a_paid_balance_request_still_holds_a_grown_balance(shop: Shop) -> None:
    # pay the balance request, add an item after acceptance, quote and accept it, fulfil both
    with pytest.raises(LifecycleError) as e: shop.shipped(order, item, added)
    assert e.value.slug == "balance_unpaid"

def test_the_balance_cannot_be_waived_before_acceptance(shop: Shop) -> None:   # order_not_accepted
```

- [ ] **Step 2: Run them to verify they fail**

Run: `cd backend && uv run pytest tests/orders/test_balance.py -v`
Expected: FAIL (no balance request at accept; no `waive_balance`).

- [ ] **Step 3: Implement** the three changes in Interfaces.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd backend && uv run pytest tests/orders tests/api -n auto && uv run mypy`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/scadbuddy/orders/lifecycle.py backend/tests/orders/test_balance.py
git commit -m "feat(orders): the balance request at accept, balance_unpaid and waive_balance (#5281)"
```

---

### Task 11: Routes and agent tools for deposits, the balance and shipping

**Files:**
- Modify: `backend/scadbuddy/api/order_payments.py`, `backend/scadbuddy/api/orders.py` (`accept_order` calls `payments.link_pending`), `backend/scadbuddy/orders/totals.py`, `agent/src/tools/orders.ts`, `agent/src/tools/coverage.ts`, `backend/CLAUDE.md`
- Test: `backend/tests/api/test_order_deposits.py`, `agent/test/orders.test.ts`, `agent/test/toolTier.test.ts`

**Interfaces:**
- Consumes: Tasks 8-10; Task 4's `Payments.link_pending`.
- Produces (routes, in `api/order_payments.py`):
  - `PUT /api/v1/orders/{order_id}/deposit` body `DepositBody {amount_minor: Money | None = Field(ge=1)}` → `Order` (`set_deposit`), then `link_pending`.
  - `POST /api/v1/orders/{order_id}/deposit/policy` → `Order` (`apply_deposit_policy`), then `link_pending`.
  - `POST /api/v1/orders/{order_id}/deposit/waive` body `WaiveBody {reason: str = Field(min_length=1, max_length=500)}` → `Order`.
  - `POST /api/v1/orders/{order_id}/balance/waive` body `WaiveBody` → `Order`.
  - `PUT /api/v1/orders/{order_id}/shipping-option` body `ShippingOptionBody {option_id: str | None}` → `Order`.
  - `POST /orders/{id}/accept` calls `link_pending` after the accept, so a balance request on a provider gets its link.
- Produces (`OrderTotals`, computed in `order_totals` with the order): `deposit_due_minor: Money = 0` (`max(0, deposit_minor - paid)`, 0 when waived or unset) and `shipping_held: bool = False` (`_balance_paid`'s rule). `order_totals` gains a keyword `order: Order | None = None` and `requests: Sequence[PaymentRequest] = ()`.
- Produces (agent):
  - `order_deposit_set` (`outward`; routes `PUT …/deposit`, `POST …/deposit/policy`): input `{order_id, amount_minor?: money (>=1) | null, policy?: true}`, exactly one given (`ToolError` otherwise).
  - `order_deposit_waive` (`write`; `POST …/deposit/waive`): `{order_id, reason: string 1-500}`.
  - `order_balance_waive` (`outward`; `POST …/balance/waive`): `{order_id, reason: string 1-500}`.
  - `order_shipping_option_set` (`write`; `PUT …/shipping-option`): `{order_id, option_id: string | null}`; the description names Settings → Orders as where the options are.

- [ ] **Step 1: Write the failing tests**

```python
# tests/api/test_order_deposits.py
def test_deposit_unpaid_is_a_409_problem(client) -> None:
    client.put(f"/api/v1/orders/{order_id}/deposit", json={"amount_minor": 2000})
    r = client.post(f"/api/v1/orders/{order_id}/items/{item_id}/revisions/{rev_id}/send")
    assert r.status_code == 409 and r.json()["type"] == "https://scadbuddy.dev/problems/orders/deposit_unpaid"
    assert client.get(f"/api/v1/orders/{order_id}").json()["totals"]["deposit_due_minor"] == 2000
def test_a_waiver_needs_a_reason(client) -> None:   # {"reason": ""} → 422 on reason
def test_shipping_held_until_the_balance_is_paid(client) -> None:
    # accept an order with a deposit; totals.shipping_held is True; POST shipments → 409 balance_unpaid
def test_accept_links_the_balance_request_on_the_default_provider(client_with_fake) -> None:
    assert client.get(f"/api/v1/orders/{order_id}").json()["payment_requests"][-1]["hosted_url"]
def test_a_pickup_option_needs_no_address(client) -> None: ...
```

```ts
// agent/test/orders.test.ts
it('sets a deposit or applies the policy as an outward step, waives with a reason (#5281)', async () => {
  expect(tool('order_deposit_set').risk).toBe('outward')
  expect(tool('order_balance_waive').risk).toBe('outward')
  expect(tool('order_deposit_waive').risk).toBe('write')
  expect(tool('order_shipping_option_set').risk).toBe('write')
  // policy: true hits POST …/deposit/policy; amount_minor hits PUT …/deposit with {amount_minor}
  await expect(tool('order_deposit_set').handler({ order_id: ORDER }, ctx())).rejects.toThrow(ToolError)
})
```

- [ ] **Step 2: Run them to verify they fail**

Run: `cd backend && uv run pytest tests/api/test_order_deposits.py -v` and `cd agent && pnpm gen:api && pnpm exec vitest run test/orders.test.ts test/coverage.test.ts test/toolTier.test.ts`
Expected: FAIL (404 routes; no tools).

- [ ] **Step 3: Implement** the routes, the totals fields, the tools; add the deposits paragraph to `backend/CLAUDE.md` (the columns, the slugs, gaps 1-7).

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd backend && uv run pytest -n auto && uv run mypy && uv run ruff check . && uv run ruff format --check .` and `cd agent && pnpm lint && pnpm typecheck && pnpm test`
Expected: PASS.

- [ ] **Step 5: Commit and open PR B1**

```bash
git add backend agent
git commit -m "feat(orders): deposit, waiver and shipping-option routes and agent tools (#5281)"
```

PR title `feat(orders): deposits, the balance rule, shipping options and auto tax (#5281)`, body `Part of #5281`.

---

## PR B2 (#5281): the back office's deposits UI

### Task 12: The order page and Settings → Orders

**Files:**
- Create: `frontend/src/components/orders/DepositControls.tsx` and test, `frontend/src/components/orders/ShippingOptionPicker.tsx` and test, `frontend/src/components/settings/DepositPolicyEditor.tsx` and test, `frontend/src/components/settings/ShippingOptionsEditor.tsx` and test, `frontend/src/mocks/features/orders/money.ts`
- Modify: `frontend/src/api/types.ts`, `frontend/src/api/client.ts`, `frontend/src/mocks/features/orders/orders.ts`, `frontend/src/mocks/features/orders/revisions.ts` (send → `deposit_unpaid`), `frontend/src/mocks/features/settings.ts`, `frontend/src/mocks/fixtures.ts`, `frontend/src/components/orders/ChargesEditor.tsx` (+ test), `frontend/src/components/orders/ShipmentForm.tsx`, `frontend/src/pages/OrderPage.tsx`, `frontend/src/pages/settings/fields.ts`, `frontend/src/pages/settings/sections/orders.tsx` (+ test), `frontend/src/pages/settings/sections/payments.tsx` (the instructions field)

**Interfaces:**
- Consumes: Task 11's routes and `OrderTotals` fields; Task 7's settings.
- Produces:
  - `client.ts`: `setDeposit(orderId: string, amount_minor: number | null)`, `applyDepositPolicy(orderId: string)`, `waiveDeposit(orderId: string, reason: string)`, `waiveBalance(orderId: string, reason: string)`, `setShippingOption(orderId: string, option_id: string | null)`.
  - `mocks/features/orders/money.ts`: exports `retax(orderId: string): void`, `depositFor(...)`, `depositUnpaid(orderId: string): Refused | null`, `balanceUnpaid(orderId: string): Refused | null`, mirroring Tasks 7-10 (half-up rounding, the largest rule, a typed tax wins, the `auto` row kept on a whole replace); `orders.ts` calls `retax` from every handler the backend's `_retax` list names, and refuses a shipment with `balanceUnpaid`.
  - `DepositControls({ order, totals, requests, onChanged })`: the deposit amount (Set, Clear, "Use the policy"), its state (paid, due `deposit_due_minor`, waived with its reason), "Proceed without deposit" and, once accepted, "Ship without full payment" while `shipping_held`, each opening a dialog that requires a reason.
  - `ShippingOptionPicker({ order, charges, options, onChanged })`: a select of `storefront_shipping_options` (label and price; "pickup, no address" for `ships: false`) while `draft` or `quoted`, showing `shipping_address_required` as the backend says it.
  - `ChargesEditor` shows the `auto` row read-only with an "auto" tag and never sends it.
  - `ShipmentForm` shows `balance_unpaid` with a link to "Ship without full payment".
  - Settings → Orders: `orders_tax_percent`, `orders_tax_shipping` (through `fields.ts`), `DepositPolicyEditor` (rows of criterion, threshold for `total_above`, percent or fixed amount; under `has_request` the note "A design request has no estimate, so a percent counts it as zero: use a fixed amount"), `ShippingOptionsEditor` (id, label, price, ships). Settings → Payments: `storefront_payment_instructions` (plain text, shown as text).

- [ ] **Step 1: Write the failing tests**

```tsx
// DepositControls.test.tsx
it('sets a deposit, shows it due, and blocks a send until it is paid', async () => {
  await user.type(screen.getByLabelText('Deposit (USD)'), '20'); await user.click(screen.getByRole('button', { name: 'Set deposit' }))
  expect(await screen.findByText('Deposit due $20.00')).toBeInTheDocument()
})
it('asks for a reason before proceeding without the deposit', async () => {
  await user.click(screen.getByRole('button', { name: 'Proceed without deposit' }))
  expect(screen.getByRole('button', { name: 'Proceed' })).toBeDisabled()
  await user.type(screen.getByLabelText('Reason'), 'regular customer')
  await user.click(screen.getByRole('button', { name: 'Proceed' }))
  expect(await screen.findByText(/Waived: regular customer/)).toBeInTheDocument()
})
it('applies the policy', async () => { /* setMockSettings({orders_deposit_policy: [{criterion: 'has_request', amount_minor: 2000}]}); request item */ })

// ChargesEditor.test.tsx
it('shows the auto tax read-only and keeps it on save', async () => { /* orders_tax_percent '10'; save a fee; auto row recomputed */ })
// ShippingOptionPicker.test.tsx
it('a pickup needs no address; a shipping option without one is refused', async () => { ... })
// ShipmentForm via OrderPage.test.tsx
it('says the balance is unpaid and offers Ship without full payment', async () => { ... })
// orders.test.tsx (settings)
it('saves the tax, the deposit policy and the shipping options', async () => {
  expect(mockSettings().orders_deposit_policy).toEqual([{ criterion: 'total_above', above_minor: 10000, percent: '50' }])
})
```

- [ ] **Step 2: Run them to verify they fail**

Run: `cd frontend && pnpm gen:api && pnpm exec vitest run src/components/orders src/components/settings src/pages/settings/sections/orders.test.tsx src/pages/OrderPage.test.tsx`
Expected: FAIL (no module `DepositControls`).

- [ ] **Step 3: Implement** the mocks, the client calls and the components; mount `DepositControls` and `ShippingOptionPicker` in the Money card.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd frontend && pnpm lint && pnpm typecheck && pnpm test && pnpm build`
Expected: PASS.

- [ ] **Step 5: Commit and open PR B2**

```bash
git add frontend/src
git commit -m "feat(orders): deposits, the balance, shipping options and auto tax on the order page (#5281)"
```

PR title `feat(orders): deposits and shipping options in the back office (#5281)`, body `Fixes #5281`.

---

## Self-review

1. **Spec coverage.** §7 `kind` (Task 1), manual payment closing a request (Task 3), hosted URL and waiting for the webhook (Tasks 3-4; the storefront's page is phase 4), the public webhook route (Task 4), instructions text (Tasks 7, 12). §8 `deposit_minor` and changes before accept (Task 9), the policy and its three criteria with "largest wins" (Tasks 7, 9), the deposit request (Task 9), `deposit_unpaid` on send and test print with "Proceed without deposit" (Task 9), the balance request at accept (Task 10), `balance_unpaid` and `waive_balance` (Task 10), the back-office exemption (Task 10). §9 shipping options and pickup (Tasks 7-8), auto tax and the typed tax (Task 8). #4957 registry, credentials, settings page, "Request payment", payments list with provider, agent tools, README, fake provider end to end, bad signature, replay (Tasks 1-6). Fee entry (Task 3). §6.2's `submit_order` and the storefront `accept_order` trigger are phase 4.
2. **Step scan.** Every code step names its signatures and file; test steps carry the spec's values (2000, 50 %, 25 %, half-up 101 from 1005 at 10 %).
3. **Type consistency.** `PaymentRequestView`, `open_requests_for_order`, `record_provider_event`, `EventOutcome`, `OrderRules`, `set_deposit`, `waive_deposit`, `waive_balance`, `set_shipping_option`, `deposit_due_minor`, `shipping_held` are spelled the same in every task.
4. **Review Focus.** Each line has its test: 1 and 2 in Task 3, 3 in Task 9, 4 and 5 in Task 8.
5. **Proportion.** Tests are sketches with their assertions; no function bodies.
