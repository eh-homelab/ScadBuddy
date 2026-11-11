# Storefront Phase 4: Submit and Follow Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A visitor submits their cart as an order and then follows it: they get an order link by mail, answer revisions, accept quotes, cancel, and pay. An account keeps their orders together.

**Architecture:** As in phase 3, everything that reads or writes the database runs in the CRUD service, behind routes under `/api/v1/internal/crud/storefront/*`. The edge only checks the cookie, the session and the bot check, then forwards. Mail goes through an outbox table, written in the transaction that causes it and sent after the commit, so a rolled-back change sends nothing. The order link's token never travels in a URL path: the page reads it from the URL fragment (`/o#<token>`) and sends it in a request body.

**Tech Stack:** Python 3.12, FastAPI, psycopg 3, Postgres 18, `smtplib` (stdlib), React 19 + Vite (the `storefront` entry), msw, Playwright.

**Spec:** `docs/superpowers/specs/2026-11-02-storefront-design.md` (§5.2, §5.5–§5.8, §6.1–§6.4, §7). Phase 4 is §11 item 4 (#5283).

## Global Constraints

- Every phase 3 constraint still holds (`docs/superpowers/plans/2026-11-07-storefront-phase-3-public-site.md` "Global Constraints" and "Trust model"):
  - the edge holds no database secret, no KEK and no SMTP password;
  - the CRUD routes are the boundary, and they check ownership themselves;
  - migrations are new files only;
  - no generated API file is committed.
- No token in a path. The order link token and the pay token travel in request bodies only. The page carries the order link in the URL fragment, `/o#<token>`, which the browser never sends. The CRUD side looks each token up by its sha256 (hex). The order link token is also kept sealed under the CRUD service's KEK (`storefront_order_links.token_sealed`, cleared on revoke), so that every order mail can carry the link; the pay token is stored as sha256 only. Tokens are 32 random bytes, URL-safe base64.
- Mail is sent only by the CRUD service. It writes an outbox row in the same transaction as the change that causes it, and sends after the commit. The SMTP password is sealed under the CRUD service's KEK (`core/secrets.py` `seal_secret`), as payment credentials are (`payments/credentials.py`). With no SMTP host or no sender set, the `order_link` and `sign_in_code` rows stay queued and the shop sees them as not sent. The four order events (`revision_ready`, `price_ready`, `payment_requested`, `shipped`) queue only when `storefront_mail_covers` is true, which needs a host and a sender; without one they leave no row and stay #5660's plugin's job. Nothing fails.
- Mail templates are plain text. Each link they carry is built from the existing env-only `storefront_public_url` (`SCADBUDDY_STOREFRONT_PUBLIC_URL`, `https://michaels-workshop.nullreference.io` in prod). It is never a stored setting: a stored value could point customers' links anywhere (`core/settings.py`). Customer text in a mail is never HTML.
- The customer actor (§6.4): `Actor.kind` gains `customer`, with `principal` `customer:<customer id>` and `session` the visitor's row id, the account's row id, or `link:<link row id>` for a reader who holds only an order link (Task 5's body `token`, which comes with no visitor header). Only the CRUD side builds it. A link reader's `session` changes when Task 4 re-issues the link, so audit readers see one customer (the same `principal`) under two sessions, not two people.
- A submitted order stays `draft` in `OrderStatus`. "Submitted" is `orders.submitted_at` (§5.5).
- After submit, the customer may only answer revisions, accept quotes, pay, and cancel while nothing is approved (§5.5). Every other customer write gets 409 `order_submitted`.
- The bot check (§5.8): Cloudflare Turnstile on sign-up, submit and design requests (Task 9).
  - It is on when a site key is set.
  - The secret is a stored setting sealed under the CRUD service's KEK (`SECRET_FIELDS`). The CRUD side calls Siteverify, and the edge only forwards `X-Turnstile-Token`.
  - With no site key, everything passes. With a site key but a missing or unreadable secret, the check fails closed with 503 `bot_check_unavailable`.
- Problem types stay under `https://scadbuddy.dev/problems/storefront/`.
- PR titles are conventional commits with scope `storefront`. Each PR body says `Part of #5283`, and the last one says `Fixes #5283`.

## Ownership (agreed with scadsy on #5283, 2026-11-09)

- **This session:** Tasks 1–7 (the customer actor, mail, submit, the order link, the customer's order page, the pay page, back-office link controls).
- **scadsy:** Tasks 8–9 (accounts, Turnstile).

Task 2 lands first: Tasks 3, 4 and 8 send mail through it. Task 9 (scadsy's accounts PR 1) lands first and defines `storefront/turnstile.py` `require_turnstile`. Task 3 imports it and adds no stub; if Task 3 is ready earlier, it waits or is sequenced with scadsy.

## Review Focus

1. **A replayed submit.** The same Idempotency-Key twice gives one order, one number and one mail (Task 3).
2. **A stolen or revoked link.** A revoked token answers 404 with an empty body, exactly like an unknown one. A token for order A never reads order B (Task 5).
3. **Mail after a rollback.** A transition that fails after it queued mail leaves no outbox row (Task 2).
4. **Two answers at once.** Two tabs answer the same revision. One wins, and the other gets 409 `revision_answered` (Task 5).
5. **The last acceptance on a storefront order.** The answer or quote acceptance that approves the last open item accepts the order in the same transaction, as the customer. A back-office order does not (Task 1).

---

### Task 1: The customer actor and storefront acceptance rules (this session)

**Files:**
- Modify: `backend/scadbuddy/orders/models.py` (`Actor.kind` gains `"customer"`), `backend/scadbuddy/orders/lifecycle.py` (`answer_revision`, `_accept_quote`, a new `accept_for_customer`), `backend/scadbuddy/api/orders.py` (route), `agent/src/tools/coverage.ts`
- Create: `backend/scadbuddy/orders/storefront/customers.py`
- Test: `backend/tests/orders/storefront/test_customer_actor.py`

**Interfaces:**
- Produces:
  - `customer_actor(customer_id: str, session: str) -> Actor`.
  - `Lifecycle.accept_for_customer(order_id: str, item_id: str, what: Literal["revision", "quote"], target_id: str, reason: str, by: Actor) -> OrderItem`, with the route `POST /api/v1/orders/{order_id}/items/{item_id}/accept-for-customer` and body `{what, target_id, reason}`.
  - On a `storefront` order, the back office's own `answer_revision`/`accept_quote` is refused with 409 `customer_accepts`. The shop accepts only through `accept_for_customer`, which records the reason in `order_events`.
  - On a `storefront` order, the customer's `answer_revision(accepted)` or `accept_quote` that leaves every open item `approved` calls `accept_order` in the same transaction, `by` the customer (§6.2).

- [ ] **Step 1: Write the failing tests**

```python
def test_the_last_customer_acceptance_accepts_a_storefront_order(lifecycle, submitted_storefront_order_with_two_items, customer):
    a, b = submitted_storefront_order_with_two_items.items
    accept_both_revisions_but_last(lifecycle, a, b, customer)
    assert lifecycle.store.order(a.order_id).status == "accepted"
    assert last_event(a.order_id).actor.kind == "customer"

def test_the_shop_accepts_for_the_customer_only_with_a_reason(client, storefront_order_with_sent_revision):
    o, i, rev = storefront_order_with_sent_revision  # the order, its item, the item's newest sent revision
    r = client.post(f"/api/v1/orders/{o.id}/items/{i.id}/revisions/{rev.id}/answer", json={"answer": "accepted"})
    assert r.status_code == 409 and r.json()["type"].endswith("customer_accepts")
    r = client.post(f"/api/v1/orders/{o.id}/items/{i.id}/accept-for-customer", json={"what": "revision", "target_id": rev.id, "reason": ""})
    assert r.status_code == 422
    r = client.post(f"/api/v1/orders/{o.id}/items/{i.id}/accept-for-customer", json={"what": "revision", "target_id": rev.id, "reason": "By phone, 9 Nov"})
    assert r.status_code == 200 and "By phone, 9 Nov" in events_text(o.id)

def test_a_back_office_order_is_not_accepted_by_the_last_answer(...)  # unchanged behaviour
```

- [ ] **Step 2: Run them and confirm they fail.** Run `cd backend && uv run --frozen pytest tests/orders/storefront/test_customer_actor.py -v`.
- [ ] **Step 3: Implement the Interfaces block.** Add the agent tool coverage entry for the new route.
- [ ] **Step 4: Run them again and confirm they pass.** Also run `tests/orders`.
- [ ] **Step 5: Commit.** `feat(storefront): the customer actor; a storefront order accepts on the customer's last answer`

### Task 2: Mail, through an outbox (this session)

**Files:**
- Create: the migration `<ts>_storefront_mail.sql`, `backend/scadbuddy/orders/storefront/mail.py`, `backend/scadbuddy/orders/storefront/mail_templates.py`, `backend/scadbuddy/orders/storefront/links.py`
- Modify: `backend/scadbuddy/core/settings.py` and `api/settings.py` (`storefront_mail_host`, `_port` 587, `_user`, `_sender`, `_tls` (`starttls` by default, `implicit` or `none`; `none` only without a login); every connection field is person-only, like the password; not `storefront_public_url`, which stays env-only), `backend/scadbuddy/workflows/housekeeping.py` (step `STOREFRONT_MAIL_SWEEP`, patch `housekeeping-send-storefront-mail`, appended last), `main.py`, `frontend/src/components/settings/` (a "Storefront mail" section with the sealed password, as the payment credential fields are)
- Test: `backend/tests/orders/storefront/test_mail.py`, `backend/tests/test_housekeeping.py`, `backend/tests/test_housekeeping_replay.py`, the settings test

**Interfaces:**
- Produces:
  - Table `storefront_mail`: `id`, `order_id` (null for a sign-in code), `to_address`, `template`, `context jsonb` (an order mail holds `link_id`, never a token), `created_at`, `next_attempt_at`, `lease_until`, `sent_at`, `attempts`, `last_error`.
  - Table `storefront_mail_secret` (one row): the sealed password envelope, bound to the host, port, user and TLS mode it was saved for.
  - Table `storefront_mail_server` (one row): the last server-level error, which the shop sees.
  - Table `storefront_order_links`: `id`, `order_id`, `token_sha256` (unique), `token_sealed` (cleared on revoke), `created_at`, `revoked_at`, `sent_to`, `opened_at`. `storefront_mail_covers` reads it, and Tasks 3–5 use it through `links.py` (`create_order_link`, `revoke_order_links`, `live_link`).
  - `MailEvent = Literal["sign_in_code", "order_link", "revision_ready", "price_ready", "payment_requested", "shipped"]`.
  - `queue_mail(conn, event: MailEvent, to: str, context: dict, order_id: str | None) -> str`.
  - `queue_mail` accepts exactly one plain address; anything else raises `EmailInvalidError` (422 `email_invalid` at a route).
  - `send_queued(limit: int = 50) -> int`.
    - It leases rows (`lease_until`, claimed with `FOR UPDATE SKIP LOCKED` and committed at once), then sends each one with no transaction held, then stamps the result in a short transaction.
    - It sends with `smtplib` in a thread with a 20 s timeout, and logs in only over TLS.
    - A server-level failure (connect, TLS, login, sender, a binding mismatch) never counts against a row; it backs the process off from 1 min to 1 h.
    - A recipient refusal counts against its row, with `next_attempt_at` backing off from 1 min to 1 h, and the row is given up after 8 attempts. A fixed failure fails at once.
    - A `sign_in_code` row not sent within 10 minutes expires, and its code is cleared.
    - One send pass runs at a time per process.
  - `storefront_mail_covers(conn, order_id: str, event: MailEvent, *, ready: bool) -> bool`: true when the order has a live (unrevoked) order link and ``ready`` (a mail host and a sender are set, #5969). #5660's plugin reads it.
  - A transition that queued mail starts `send_queued` from the lifecycle's after-commit step (`_told`). The housekeeping step retries what is left.
  - Hooks:
    - `send_revision` queues `revision_ready`;
    - recording a quote that the customer can accept queues `price_ready`;
    - `open_payment_request` queues `payment_requested`;
    - `add_shipment` queues `shipped`.
    Each queues only when `storefront_mail_covers` is true, in the same transaction.

- [ ] **Step 1: Write the failing tests**
  - A queued row in a transaction that rolls back is gone.
  - `send_queued` against a local `aiosmtpd` server (or a fake `SMTP` object) sends the template's subject and body and stamps `sent_at`.
  - With no host, nothing is sent and nothing raises.
  - The template renders customer text as plain text.
  - Each of the four hooks queues exactly one row when a live link exists, and none without one.
- [ ] **Step 2: Run them and confirm they fail.**
- [ ] **Step 3: Implement.** The templates are short plain text: a subject plus a body with the link `{public_url}/o#{token}`. A sign-in code mail carries the code and no link.
- [ ] **Step 4: Run them again and confirm they pass.** Also run `tests/test_housekeeping.py` and `tests/test_housekeeping_replay.py`, the latter with a recorded fixture `storefront_mail.json`.
- [ ] **Step 5: Commit, then open PR 1.** `feat(storefront): mail through an outbox, sent by the CRUD service`

### Task 3: Submit (this session)

**Files:**
- Create: `backend/scadbuddy/orders/storefront/submit.py` (the `storefront_order_links` table comes from Task 2's migration)
- Modify: `backend/scadbuddy/orders/lifecycle.py` (`submit_order`), `api/crud_storefront.py` (area `drafts`: `POST …/draft/submit`), `crud/storefront.py`, `storefront/routes.py` (`POST /api/store/v1/draft/submit`), `frontend/src/storefront/pages/DraftPage.tsx` and a new `components/SubmitForm.tsx`
- Test: `backend/tests/orders/storefront/test_submit.py`, `backend/tests/storefront/test_edge_routes.py`, `frontend/src/storefront/pages/DraftPage.test.tsx`

**Interfaces:**
- Produces:
  - Uses Task 2's `storefront_order_links` (`id`, `order_id`, `token_sha256` unique, `token_sealed`, `created_at`, `revoked_at`, `sent_to`, `opened_at`).
  - `SubmitInput`: `name` (1–200), `email` (an address, at most 254), `shipping_option_id: str | None`, `address: Address | None`, and `account_id`, which comes only from the CRUD side's session lookup, never from the body.
  - `Lifecycle.submit_order(order_id, body: SubmitInput, by: Actor) -> tuple[Order, str]` returns the order and the new link token, in one transaction under the order lock:
    1. It makes a new customer, with the name, the email contact and the given address as its default shipping address. It never links an existing one at submit (spec §5.2: only a verified account links).
    2. It takes the number from `order_numbers`.
    3. It runs `set_shipping_option`, which refuses 409 `shipping_address_required` (`orders/lifecycle.py`, unchanged) for an option that ships with no address.
    4. It recomputes the auto tax.
    5. It runs `apply_deposit_policy`. A deposit request opened here queues no `payment_requested` mail (no link exists yet, so `storefront_mail_covers` is false): this is intended, because the `order_link` mail of step 8 leads to the order page, which shows it.
    6. It stamps `submitted_at`.
    7. It creates the link.
    8. It queues `order_link` mail to the email.
  - An empty cart gets 409 `draft_empty`. A second submit with a new key gets 409 `order_submitted`. A replay with the same key answers the first reply and sends no second mail.
  - The edge's submit route calls `await require_turnstile(request, "submit")` (Task 9) before the CRUD call. The page renders Task 9's Turnstile widget component and sends its token in `X-Turnstile-Token`.

- [ ] **Step 1: Write the failing tests.**
  - One order, one number and one queued `order_link` row, with the key replayed twice.
  - A customer with the same email is never linked or changed.
  - A shipping option that ships, without an address, gets 409 `shipping_address_required`.
  - The deposit policy applies.
  - Another visitor's submit sees no draft (404 `draft_not_found`).
  - On the edge: 428 without a key, and a test that monkeypatches `require_turnstile` asserts it was awaited with `"submit"`.
  - On the page: the form shows the shipping options with their prices, asks for an address only when the option ships, and on success shows "We have your order. We sent a link to {email}."
- [ ] **Step 2: Run them and confirm they fail.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run them again and confirm they pass.** Also run `tests/orders`, `tests/storefront`, the frontend lint, typecheck, `vitest run src/storefront` and `check:storefront-bundle`.
- [ ] **Step 5: Commit, then open PR 2.** `feat(storefront): submit a cart as an order`

### Task 4: The order link in the back office (this session)

**Files:**
- Modify: `backend/scadbuddy/api/orders.py` (`POST /api/v1/orders/{order_id}/customer-link` sends a new link and revokes the old ones; `DELETE` revokes; `GET` shows the link's state without its token), `orders/storefront/mail.py` (`issue_link`, a wrapper over Task 2's `links.py`: it calls `revoke_order_links` and `create_order_link`, so there is one revoke path; not in `submit.py` or `links.py`, which would make import cycles), `frontend/src/pages/OrderPage.tsx` (a "Customer link" card), `agent/src/tools/coverage.ts`
- Test: `backend/tests/api/test_order_customer_link.py`, the OrderPage test

**Interfaces:**
- `issue_link(conn, kek, order_id, label, to: str) -> str` (the token) revokes every live link of the order through `links.py` `revoke_order_links`, makes a new one with `create_order_link`, and queues `order_link` mail. A `back_office` order can get a link too. It needs a customer with an email contact, or an explicit `to` in the body. Otherwise it gets 422 `no_email`.
- Agent tools: `order_customer_link_send`, which is outward and returns a pending action, as other mail-sending tools do; and `order_customer_link_revoke`.

- [ ] **Steps 1–5:** test first, implement, then commit `feat(storefront): send or revoke an order's customer link`.

### Task 5: The customer's order page (this session)

**Files:**
- Modify: `api/crud_storefront.py` (area `order`), `crud/storefront.py`, `storefront/routes.py`, `crud_areas.py`
- Create: `backend/scadbuddy/orders/storefront/order_page.py`, `frontend/src/storefront/pages/OrderPage.tsx`
- Test: `backend/tests/orders/storefront/test_order_page.py`, `backend/tests/storefront/test_edge_routes.py`, `frontend/src/storefront/pages/OrderPage.test.tsx`, `frontend/e2e/storefront.spec.ts`

**Interfaces:**
- CRUD routes. Each takes `{"token": …}` in the body, or an account session id that owns the order. Each answers an empty 404 for a token that reads nothing:
  - `POST …/order/view` returns a `CustomerOrderView`:
    - status in the customer's words (a mapping table in `order_page.py`);
    - each item with its revisions (the pictures as presigned URLs), "Accept" or "Ask for changes" on the newest sent one, and the quote and its price;
    - the open payment requests (`payments/store.py` `open_requests_for_order`), each with its pay link;
    - the shipments with their tracking text.
    It never shows costs, margins, internal notes or the shop's events.
  - `POST …/order/revisions/{revision_id}/answer` with `{token, answer, note}`.
  - `POST …/order/quotes/{quote_id}/accept` with `{token}`.
  - `POST …/order/cancel` with `{token, reason}`. It refuses 409 when anything is approved.
  - Each write takes an Idempotency-Key and runs as `customer_actor`. The first view stamps `opened_at`.
- Edge routes: `/api/store/v1/order/*`, the same shapes. The page lives at `/o`, reads the token from `location.hash`, and never puts the token into history or a path.

- [ ] **Step 1: Write the failing tests.**
  - A token reads only its own order.
  - A revoked token gets the same 404 as an unknown one.
  - Two answers to one revision: the second gets 409 `revision_answered`.
  - The last acceptance accepts the order (Task 1).
  - Cancel is refused after an approval.
  - The view has no cost field. An allowlist test, as in phase 3's `ESTIMATE_FIELDS`, keeps it that way.
  - On the page: a revision's "Ask for changes" needs a note. A paid request shows "Paid" only after a fresh read, never right after the redirect (§7).
  - The e2e is mocked: open `/o#tok`, accept a revision, see the price, then see "Pay".
- [ ] **Step 2: Run them and confirm they fail.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run them again and confirm they pass.** Run every frontend and backend check, as in Task 3.
- [ ] **Step 5: Commit, then open PR 3.** `feat(storefront): the customer's order page`

### Task 6: The pay page (this session)

**Files:**
- Modify: `storefront/routes.py` (`POST /api/store/v1/pay/checkout`, which forwards `{token}` to #5769's CRUD route `…/pay/checkout`), `storefront/static.py` (the `/pay` path gets its own CSP, which allows PayPal's SDK origins, `https://www.paypal.com` and `https://www.sandbox.paypal.com`, for `script-src`, `frame-src` and `connect-src`; every other page keeps the phase 3 CSP plus Task 9's Turnstile origins; `/pay` keeps Task 9's origins as well when a site key is set, because one CSP builder serves every page and PayPal's origins are only added on top), `payments/venmo.py` (`PAY_PAGE_READY = True`, only after Step 0 below; `pay_link` returns `f"{storefront_url}/pay#{token}"`, not `/pay/{token}`, with its test in `tests/payments/test_venmo.py`; and every copy of the path form: `payments/venmo.py` (the module docstring, the `PAY_PAGE_READY` comment and `pay_link`), `crud/storefront.py`, `api/crud_storefront.py` (module and the checkout route), `api/crud_areas.py`, `backend/CLAUDE.md` ("once the storefront's `/pay/<token>` page ships"), `README.md` ("a pay page on the storefront, `/pay/<token>`"), the docstrings in `backend/tests/api/test_payment_providers.py` and `backend/tests/payments/test_venmo.py`, and the links `test_venmo.py` builds or strips with `f"{STOREFRONT}/pay/{token}"`; a final `git grep -n "/pay/<token>\|/pay/{token}"` finds none outside merged migrations and dated plans; the fake provider's own hosted page, `payments/fake.py` `https://payments.fake.invalid/pay/{ref}`, is not the storefront's pay link and stays as it is), `payments/paypal.py` (`create_request`: only if Step 0 finds the SDK cannot fund a `payment_source.paypal` order with Venmo, a pay-page request's order is created without `payment_source`). The merged migration `20261108T0512Z_payment_requests_pay_token.sql` keeps its comment: a merged migration is never edited.
- Create: `frontend/src/storefront/pages/PayPage.tsx`
- Test: `backend/tests/storefront/test_static.py` (the CSP for each path), `backend/tests/storefront/test_edge_routes.py` (an empty CRUD 404 maps to the edge's own 404), `backend/tests/payments/test_venmo.py`, `frontend/src/storefront/pages/PayPage.test.tsx`

**Interfaces:**
- The page is `/pay#<token>`, read from the hash, never from the path.
- It loads the PayPal JS SDK with the checkout's client id, currency and `enable-funding=venmo`.
- It shows the amount and what it is for. On approval, it says "Thanks. We will confirm your payment shortly." and links to the order page. The webhook settles the payment, not the page.
- With the fake provider (`SCADBUDDY_PAYMENTS_FAKE_PROVIDER`), a request's hosted URL is the fake's own page. "Pay" on the order page links there, and the pay page is not used.

- [ ] **Step 0: Measure in PayPal's sandbox** (`payments/venmo.py` module docstring: "Phase 4 measures it in the sandbox before `PAY_PAGE_READY` flips"). With sandbox credentials, create an order as `create_request` does (`payment_source.paypal`), render the JS SDK buttons with `enable-funding=venmo`, and record whether the Venmo button appears and can approve that order. Record the result, how and where it was measured, and PayPal's documentation link in `payments/venmo.py`'s docstring (#259). If the SDK cannot fund it, change `create_request` to create a pay-page request's order without `payment_source`, with a test. If no sandbox credentials are available, ship the page with `PAY_PAGE_READY` left `False`, and the flag flips in its own PR once measured. Also check a second SDK load in the same document after a fragment change (`/pay#a` to `/pay#b`) under the `/pay` CSP (#5971 review).
- [ ] **Steps 1–5:** test first, implement, then commit and open PR 4: `feat(storefront): the pay page for Venmo through PayPal`.

### Task 7: The order link's state, and mail, in the back office (this session)

**Files:**
- Modify: `frontend/src/pages/OrderPage.tsx` (the link card shows "Sent to … on …, opened on …", or "not opened"), and the outbox state for each order (`GET /api/v1/orders/{order_id}/mail`: event, to, sent_at, last_error, never the body)
- Test: the OrderPage test, `backend/tests/api/test_order_mail.py`

- [ ] **Steps 1–5:** test first, implement, then commit and open PR 5: `feat(storefront): the shop sees each order's customer mail`.

### Task 8: Accounts (scadsy)

**Files:**
- Create: the migration `<ts>_storefront_accounts.sql` (`storefront_accounts`, `storefront_sign_in_codes`, `storefront_sessions`; `storefront_visitors.account_id`), `backend/scadbuddy/orders/storefront/accounts.py`, and the edge's session cookie `sb_session`, signed as `sb_visitor` is
- Modify: `api/crud_storefront.py` (area `accounts`), `crud/storefront.py`, `storefront/routes.py`, the storefront page (sign up, sign in, "Your orders")

**Interfaces** (what Tasks 3 and 5 rely on):
- `account_for_session(conn, session_sha256: str) -> Account | None`.
- The edge sends the verified session id in the header `X-ScadBuddy-Store-Session`. The CRUD side takes it only from a `storefront` credential, as with `X-ScadBuddy-Visitor`.
- `orders_of_account(conn, account_id) -> list[str]`: the orders the account's visitors made, plus the orders whose link was opened while signed in (§5.2).
- Codes are six digits, valid for 10 minutes and good for 5 tries, with only their sha256 stored. They are sent with `queue_mail(conn, "sign_in_code", email, {"code": …}, None)` (Task 2).
- On verify, the visitor joins the account. The account links to a customer only when exactly one customer has that email as a contact.

### Task 9: Turnstile (scadsy)

As built by scadsy's accounts PR 1 (see #5283). The secret is sealed under the CRUD service's KEK (#5295), so Siteverify runs on the CRUD side, and the edge only forwards the token.

**Files:**
- Create: `backend/scadbuddy/storefront/turnstile.py`.
- Modify:
  - `storefront/routes.py`: `GET /api/store/v1/config`, and the check on `POST /draft/requests`;
  - `storefront/static.py`: the CSP;
  - `crud/storefront.py`;
  - `api/crud_storefront.py`: area `turnstile`, with `GET …/turnstile` and `POST …/turnstile/verify`;
  - `api/crud_areas.py`;
  - the settings: the site key, and the secret in `SECRET_FIELDS`;
  - the storefront page's request form and client, `StorefrontPage.tsx`, and the mocks.

**Interfaces:**
- `async require_turnstile(request: Request, action: Literal["submit", "request", "sign_up"]) -> None`:
  - It reads the `X-Turnstile-Token` header and has the CRUD side verify it.
  - It refuses with 403 `bot_check_failed`, or with 503 `bot_check_unavailable` when the check cannot be made.
  - It passes everything when no site key is set. It fails closed when a site key is set but the secret is missing or cannot be opened.
- `GET /api/store/v1/config` tells the page the site key.
- CSP:
  - When a site key is set, every storefront page allows `https://challenges.cloudflare.com` in `script-src` and `frame-src`. A per-path CSP binds only on a full document load, and the storefront is one SPA. The script loads lazily, only when a form renders the widget.
  - With no site key, the CSP stays first-party.
  - PayPal's origins stay on the `/pay` document only (Task 6). The order page's "Pay" is a plain `<a href>` full navigation.
