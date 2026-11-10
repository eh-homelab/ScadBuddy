# Customer storefront: design revisions, a public site and checkout

Status: approved by the owner on 2026-11-04. Epic #4962 (parent #4248); phases #5280,
#4957, #5281, #5282, #5283 and #5284 (§11). It amends the orders spec
(`2026-10-18-orders-design.md`, "the orders spec") and adds to the payments seam #4957.
The owner made the decisions in §2 in a brainstorm on 2026-11-02; every other choice is
marked **(call)**: the author made it, and the owner may overturn it.

Every fact about the code was read at `origin/main` `130f14298` on 2026-11-02. Nothing
here was measured against a live payment provider or a live mail server.

## 1. Why

Today an order starts as a message. A customer asks for a part; the shop finds or makes
the model, quotes it from a slice, takes the money, prints and ships. The owner named the
slow steps: getting the model, getting the quote, and printing. The storefront takes the
first steps off the shop: a customer, known or a stranger, picks a product or asks for a
new design, sees a price, follows the design through revisions until they accept it,
pays, and tracks the order. Every order made there is the same order the back office
already manages (orders spec §3).

## 2. Decisions (the owner's)

1. **Who.** Both the people who message the shop today and strangers who find it online.
2. **Revisions until accepted.** Each line item goes through design revisions. A revision
   is a render of the model, a photo of a test print, or both. The customer accepts it or
   asks for changes; that repeats until they accept. Photos of a print belong to the
   revision (and so to the model) they show.
3. **Design and price.** The shop chooses per item: the customer approves the design
   first and the price after, or both in one step.
4. **No sign-in wall.** A visitor works without an account. "Sign up to save" makes an
   account and keeps their work.
5. **A separate public site** with its own reduced API, on the same orders. It holds no
   database secret: it reaches the orders only through the CRUD service's HTTP routes
   (amended 2026-11-07, the owner: "No direct SQL", #5295). The back office stays behind
   the front door.
6. **Price.** The rough estimate at once; the firm price is a slice quote, sent later
   with a revision. A design request shows no price until the shop quotes it.
7. **What is for sale.** The shop lists a template and chooses which parameters a
   customer may change. Renders are limited per visitor.
8. **The cart is a draft order.** No separate cart: a visitor's work is an order in
   `draft`.
9. **Deposits.** Optional per order, set in the back office, and also required by a
   deposit policy whose criteria are: a design request on the order, an order total
   above an amount, a customer's first order.
10. **Shipping and tax** were left to the author (§9).
11. **Acceptance and the balance.** On a storefront order, the customer's last accept
    accepts the order with no click from the shop, and opens the balance request. The
    shop cannot ship, or hand over at pickup, until the balance is paid, except with
    "Ship without full payment" and a reason (§8).

## 3. Goals, non-goals, anti-goals

Goals: a visitor can browse, customize a listed product, ask for a new design, submit,
answer revisions, pay a deposit and the balance, and see the order's status, from a
phone, without an account. The shop sees every storefront order in the back office it
uses now.

Non-goals (this spec): carrier-rate lookup and label printing; tax by region or a tax
service; coupons; a customer chat thread (the revision answers are the only channel);
reviews; several shops on one install; the assistant's flow that turns a design
request into a model (its own work; §4.7 gives its input and output only).

Anti-goals, which make it a failure even if it works:

- **Any back-office route reachable from the public site.** The storefront is a second
  process with its own routes; it does not proxy `/api/v1`.
- **A stranger who can cost the shop money**: unbounded renders, uploads or slices.
- **A customer who sees another customer's anything.**
- **A second order model.** The storefront writes orders only through the CRUD service's routes, which call
  `Lifecycle`.
- **A sign-in wall** before a visitor can customize or submit.

## 4. Design revisions (back office first)

This part ships before anything is public. The shop can use it for orders that come in
by message.

### 4.1 The item's design has one authority

Today an item's `reference` (slug, commit, inputs, libraries, `template_name`,
`render_key`; `orders/models.py:248-261`) is written by `add_item` and `update_item`.
From now on **only creating a revision writes it**. `order_item_revisions` holds every
design an item has had; `order_items.current_revision_id` names the one in force, and the
item's reference columns are a copy that only `Lifecycle.add_revision` writes, in the
same transaction. `add_item` with a reference makes revision 1; an `update_item` that
changes inputs, commit or colours makes the next revision. No other route writes the
reference columns. (A view in place of the copy was considered; the copy keeps every
existing read and the custom-item CHECK, `20261029T0733Z_order_custom_items.sql`, as
they are.)

An imported mesh, from MakerWorld or elsewhere, becomes a library mesh model (#2146) with
a slug and a commit, so it fits the same reference. Nothing references a raw file.

### 4.2 Item kinds

`ItemKind` gains `request` beside `model` and `custom` (#4954):

- `model`: has a reference from revision 1.
- `custom`: never has a reference or revisions (a title, a quantity, a price).
- `request`: a design request. It holds the customer's text and attachments
  (`order_attachments` role `reference`) and has no reference until its first revision.
  It cannot be quoted, sliced or printed until then; `quote` and `order_item_print` refuse
  it with `item_has_no_design`. Its first revision gives it a reference, and from then
  on it behaves as a `model` item. The kind stays `request`, so the shop can see where
  it came from.

The custom-item CHECK becomes: the reference columns are required for `model`, null for
`custom`, and for `request` either all null or all set.

### 4.3 A revision

`order_item_revisions`: `id`, `item_id`, `number` (1, 2, … per item), the reference
columns, `colours` (the item's colour slots and filaments: what the customer sees is
what they accept, so colours are part of the design **(call)**), `note` (the shop's, shown to the customer), `created_at`, `created_by`, `sent_at`
(null until the shop sends it), `answer` (`accepted`, `changes_requested`, null),
`answer_note`, `answered_at`, `answered_by` (jsonb, an `Actor`).

Attachments gain the subject `order_item_revision:<id>` and two roles: `render` (an image
or a 3D preview the shop or the render produced) and `test_print` (a photo or video of a
physical test print). A revision is sent with at least one attachment. The lifecycle
checks that a revision subject belongs to the order, as it does for `order_item:`
(`lifecycle.py` `add_attachment`).

Rules:

- Only the newest revision can be sent or answered. Making a new one while the newest is
  sent and unanswered marks that one `superseded` (an answer value the customer cannot
  give).
- `changes_requested` takes a note and may take attachments (role `reference`, on the
  revision's subject).
- A quote names the revision it was sliced from (`order_item_quotes.revision_id`). A new
  revision makes every earlier quote stale, as an edit does today (`update_item` drops a
  quoted item to `draft`).
- A quote's filaments come from its revision. Today the quote run writes the body's
  filaments onto the item through `update_item` (`orders/quote.py`); from now on a quote
  whose filaments differ from the newest revision's first makes a new revision with them,
  in the run's first write (the check writes nothing, orders spec Ruling 8), and is
  sliced from that one, so a `together` quote never replaces
  the revision it names **(call)**.
- In phase 1, before the public site exists, "Send" only stamps `sent_at`; the shop
  passes the revision on by hand and records the customer's answer itself, as an
  `Actor` of kind `browser` with the customer named in the note.
- #4673's slice choices are recorded per quote, so they hang under a revision. The word
  "version" stays #4673's; this spec says "revision" only.

### 4.4 Test prints are attempts

The owner needs each job's real cost, so a test print is recorded on the order
(decided by the session that owns job costing and #4251's print path):

- A test print is a row in `order_item_fulfilments`, the print path's attempts (#4719),
  with a new `purpose` column, `production` (every existing row) or `test`, and
  `revision_id` (required for `test`).
- `order_item_print` takes `purpose: test` and a revision. The print path prints the
  item's output, so only the current revision can be test-printed; an older one is
  `revision_not_newest`. A test print needs a slice quote of that revision first.
- A test attempt never needs the order `accepted`, never changes the item's status,
  never counts toward the fulfilled quantity, and is ignored by `derive` (it is not in
  `attempted`), `accept_fulfilment` and `unfulfil_item`.
- `test_print` attachments stay on the revision's subject and may name their attempt
  (`order_attachments.fulfilment_id`, nullable).
- Job costing reads every attempt, test, failed and production; it is a separate
  spec.

### 4.5 Approval mode

`order_items.approval`: `design_first` or `together` **(call: default `together` for
`model` and `design_first` for `request`)**. The shop changes it while the item is open.

- `together`: the shop sends a revision with a quote. The customer's accept accepts both:
  the revision is `accepted` and `accept_quote` runs for its quote.
- `design_first`: the customer accepts the design; the shop then quotes; the customer
  accepts the price, which is today's `accept_quote`.

Either way an item reaches `approved` only by a customer's accept when the order came from
the storefront, or by the shop's as today when it did not (§6.1). A customer's accept is
recorded with an `Actor` of kind `customer` (§6.4).

### 4.6 Photos onto the model

A `test_print` photo on an accepted revision can be added to the model's media with one
action ("Add to model photos"). `template_media` has no commit (`20260928T0718Z_template_media.sql`); it
gains `source_commit` and `source_revision_id`, both nullable, so the gallery can say
which revision a print photo shows. They travel on `MediaItem`, because the media store
rewrites a model's rows whole on every edit (`library/media_store.py` `replace`). The photo is copied through the existing media upload
operation (`library/media_operations.py`); the order's copy stays.

### 4.7 Back office surfaces

- The order page's item row gets a Revisions panel: the list, each with its attachments,
  note, sent time and answer; "New revision" (a render from the item's customizer or a new
  commit, plus photos), "Send to customer", and the approval mode.
- Agent tools: `order_item_revision_add` (write), `order_item_revision_send` (outward: it
  tells the customer), and the revisions in `order_get`. The assistant's request-to-model flow
  ends with `order_item_revision_add`.

## 5. The public site

### 5.1 Shape

A second process from the same image: `python -m scadbuddy.storefront`, port 8083
**(call)**, with its own FastAPI app, its own routes under `/api/store/v1`, and its own
page bundle. The back-office routes are not mounted in it.

**No direct SQL (amended 2026-11-07, the owner; CLAUDE.md "Database access (#5295)").**
The storefront holds no database secret and opens no Postgres pool. It is an HTTP client
of the CRUD service (today's API process), as every worker will be once #5298 lands
(CLAUDE.md "Database access (#5295)"): every read and write goes to routes under
`/api/v1/internal/crud/storefront/*` (route class `crud.storefront`, by `auth/routes.py`
`CRUD_PATH`, as for every caller), sent with the storefront's
own short-lived service credential: a projected ServiceAccount token exchanged at the CRUD
service for a 15-minute credential scoped to the storefront's routes, as
[the service-credentials spec](2026-11-05-service-credentials-without-db-design.md) (#5325)
decides for every caller. An
earlier draft gave it its own Postgres role, `scadbuddy_storefront`, limited by a
`STOREFRONT_GRANTS` list; that is dropped, because a role per caller in one database is
not a boundary (the review of PR #5287).

- The internal storefront routes are the boundary. Each one does only what the public may
  do, and the CRUD service checks ownership itself: it scopes every draft read and write
  to the visitor id the storefront sends, and it never trusts the storefront to have
  checked it. The Lifecycle, `RenderService` and the estimate run in the CRUD service.
- The storefront mounts no data volume and holds no Temporal, blob store or KEK secret.
  Renders start through the CRUD service. Uploads, previews and attachments move through
  URLs or routes the CRUD service gives it (presigned on `s3`), so it needs no store
  credentials of its own.
- Its ingress is a public host of its own (§10). The back office host is unchanged.
- The page is a second Vite entry in `frontend/` (`storefront.html`, `src/storefront/`)
  **(call)**, so the 3D viewer and money formatting are shared; it imports nothing from
  the back office's pages, and a lint rule enforces that.

### 5.2 Visitors, accounts and the order link

- **Visitor.** The first request sets a random visitor id in a `Secure`, `HttpOnly`,
  `SameSite=Lax` cookie, signed (HMAC) with the storefront's own key (§10), so the
  storefront drops a forged or garbled cookie before it calls the CRUD service. The
  storefront sends the verified id to the CRUD service in a header (§6.4).
  `storefront_visitors` stores the id's sha256, created and last-seen times, and the account it belongs to, if any. A draft order names its visitor
  (`orders.storefront_visitor_id`).
- **Account.** "Sign up to save" asks for an email and sends a sixteen-character code
  from Crockford's base32 without 0 and 1, shown in groups of four, and the mail carries a
  sign-in link (#5283, [comment 6757727525](https://github.com/eh-homelab/ScadBuddy/issues/5283#issuecomment-6757727525))
  **(call: passwordless, no passwords to store or reset)**. `storefront_accounts`: email
  (verified), `customer_id`, created and last sign-in times. On verify, the visitor joins
  the account and its drafts move with it. An account is linked to a `customers` row: an
  existing customer with that verified email contact, else a new one. The shop sees the
  link and can merge customers by hand.
- **Order link.** A submitted order gets a capability link, emailed to the address given
  at submit: a random token, stored as its sha256 in `storefront_order_links`, that opens
  that one order without an account. The shop can revoke it and send a new one. A
  `back_office` order can get a link too ("Send customer link"), so a customer who
  ordered by message can answer revisions and pay online **(call)**.
- **What an account sees** **(call)**: the orders its own visitors made, the orders
  whose link was opened while signed in to it (opening a link while signed in attaches
  the order to the account), and the orders submitted while signed in to it (a signed-in
  submit attaches the order, `attach_order`, even when the cart's visitor never joined the
  account). Nothing else, even when the account's customer has other
  orders: a contact the shop typed is not proof of who owns an email. An account links to
  an existing customer only when exactly one customer has that email as a contact; with
  none or several it makes a new customer, and the shop merges by hand.
  A signed-in submit never puts an order on a customer the shop has archived: it makes a
  new customer, and the account links to it from then on. Two submits of one account at
  once make one: the account's row is locked before its customer's, always in that order.
- These are separate from staff sign-in (#4975, `auth_sessions`). A storefront session
  never reaches the back office, and a staff session means nothing to the storefront.

### 5.3 Listings

`storefront_listings`: `id`, `slug`, `commit` (the version for sale: a template change is
not live until the shop republishes), `title`, `description`, `position`, `published`,
`params` (jsonb: for each unlocked parameter, its allowed range or choices, a subset of
the schema's), `fixed` (jsonb: the values of the locked ones), `colours` (the price-book
filaments a customer may pick), and the media to show (ids from `template_media`). A mesh
model is a listing with no unlocked parameters. A design request ("Ask for a design") is a
fixed entry, on when `storefront_requests_enabled`.

The back office gets a Storefront page: list, edit, publish, order, and a live preview of
the customer's view.

### 5.4 Customize, estimate and the draft

- The CRUD service's storefront render route validates inputs against the listing
  (`params` ∪ `fixed`), never only against the schema, and renders through
  `RenderService` with `reuse_finished`, so two visitors who choose the same inputs share
  one render. It never relies on the storefront having checked the inputs.
- **Render limits per visitor** **(call)**: settings `storefront_renders_per_minute`
  (default 3), `storefront_renders_per_day` (30), `storefront_render_concurrency` (2,
  across all visitors), counted by the CRUD service in Postgres per visitor and per client
  address. The storefront resolves the client address (`core/proxies.py` with its own
  `SCADBUDDY_TRUSTED_PROXIES`) and sends it in a header (§6.4); the CRUD service sees only
  the storefront pod, so it cannot find the address itself. A reused render costs nothing.
  Over the limit is a 429 with the time to retry.
- **Estimate.** `GET /orders/estimate` needs a saved output (`api/orders.py:537-604`).
  The CRUD service's storefront estimate route estimates from the render job instead: `estimate_grams` over the job's
  part volumes (`orders/pricing.py:160`), at the listing's colours, priced from the price
  book. It is shown as "about $X, confirmed after we check it", never as a quote. The
  common part moves out of the route into `orders/estimate.py`.
- **Add to order.** The CRUD service adds an item to the visitor's draft: `kind=model` with revision 1
  from the listing's commit and the inputs, or `kind=request` with the text and uploads.
  The storefront streams each upload to a CRUD route, which goes through
  `order_attachment_add`, typed by
  `orders/attachments.py` `sniff`, with storefront caps **(call)**: 25 MB a file, 10 files
  a request, images, video and PDF only.
- **A draft before submit** **(call)**. It needs no customer and no number:
  `orders.customer_id` and `orders.number` become nullable, with a CHECK that both are set
  unless `source = 'storefront' AND submitted_at IS NULL`. The number is taken from
  `order_numbers` at submit, so strangers' carts use no numbers and the sequence stays
  gapless. The back office's lists hide these rows by default.
- A draft that has had no activity for `storefront_draft_ttl_days` (30) is deleted by a
  housekeeping sweep: its attachment rows first (the FK is `RESTRICT`), then their holds,
  then the order. This is the one exception to the orders spec's Ruling 6 (nothing the
  process produced is deleted): an unsubmitted cart is not yet a record of the shop's.
  A cart with a time or cost entry is never swept (the lifecycle refuses those on a cart,
  `order_not_submitted`). Once a delete commits, the bus is told `order.changed` with
  `deleted: true`: an open orders list reads again without it, and an open `order:<id>`
  page shows the order as not found when that frame arrives (a page that missed it keeps
  its last view until it reads the order again).

### 5.5 Submit

Submit takes a name, an email, a shipping option (§9) and an address when the option
ships. It:

1. links or makes the customer (§5.2),
2. sets the shipping and tax charges (§9),
3. sets the deposit (§8),
4. stamps `orders.submitted_at`, which moves the order into the shop's queue,
5. emails the order link.

A submitted order stays `draft` in `OrderStatus`; "submitted" is `submitted_at`, not a new
status, so `derive` and `_settle_open` are untouched. Before submit the customer can edit
anything; after it, only answer revisions, accept quotes, pay and cancel while nothing is
approved.

### 5.6 The customer's order page

Status in the customer's words (a mapping from `OrderStatus`/`ItemStatus`, not new
states); each item's revisions with their pictures and "Accept" / "Ask for changes"; the
quote and its price; "Pay deposit" or "Pay" when a payment request is open (§7); the
shipment and its tracking text.

### 5.7 Mail

The CRUD service sends the storefront's transactional mail **(call)**: the sign-in code,
the order link, "a revision is ready", "your price is ready", "payment requested",
"shipped". It sends each one when the storefront calls the route that needs it, or when
the back office makes the change. Settings: SMTP host, port, user, sender, and the
password sealed under the CRUD service's KEK; the storefront never holds them. Every
other customer notification stays a plugin's job (orders spec §12).

### 5.8 Abuse

Sign-up, submit and design requests take a bot check, Cloudflare Turnstile, on when its
keys are set **(call)**. Asking for a sign-in code (action `sign_up`) and verifying one
(`sign_in`) take it too (the coordinator's ruling on #5283, 2026-11-09). Every storefront
route has a per-client rate limit. Nothing a
customer writes is rendered as HTML: notes are text.

## 6. Lifecycle changes

### 6.1 Where an order came from

`orders.source`: `back_office` or `storefront`. The orders list filters by it and hides
unsubmitted storefront drafts by default. For a storefront order the shop cannot accept a
revision or a quote on the customer's behalf except with "Accept for the customer", which
asks for a reason and records it.

### 6.2 New transitions

`add_revision`, `send_revision`, `answer_revision` (customer), `submit_order` (customer),
`set_deposit`, `waive_balance`. On a `storefront` order, the `answer_revision` or
`accept_quote` that leaves every open item `approved` runs `accept_order` in the same
transaction, as the customer (decision 11). Only the customer's own accept (or the shop's
"Accept for the customer") accepts the order this way; a shop's `cancel_item` that leaves
every open item `approved` does not, and the shop accepts the order itself. When
`accept_order` refuses (a payment in flight, a hold), the item's accept still commits and the
audit row `order.accept_skipped` names the refusal. Each takes the order's row lock, writes an `order_events` row, and
publishes `order.changed` / `order_item.changed`, as every transition does.

### 6.3 Refusals

New `LifecycleError` slugs: `item_has_no_design`, `revision_not_newest`,
`revision_not_sent`, `revision_answered`, `revision_empty` (a send with no attachment),
`revision_not_quoted` (a `together` send or accept with no quote of the revision),
`revision_not_accepted` (a price accept, or "Add to model photos", before the design is
accepted), `revision_not_found` (404), `order_not_submitted`, `order_submitted`
(a write only a draft allows), `deposit_unpaid`, `balance_unpaid` (§8), `customer_accepts` (the shop's own accept of a revision, a quote or a
custom price on a storefront order; and "Accept for the customer" by anyone but the shop's
staff), `order_not_storefront` ("Accept for the customer" on a back-office order).

### 6.4 The customer actor

`Actor.kind` gains `customer`, with `principal` `customer:<customer id>` and the visitor's
or account's id. The CRUD service builds it from the visitor id (and, once signed in, the
account) that the storefront sends in request headers: `X-ScadBuddy-Visitor` (the verified
visitor id) and `X-ScadBuddy-Store-Client` (the client address the storefront resolved,
which keys the per-client render limit, §5.4). It takes those headers only from a call
made with the storefront's service credential, and never from a request body. A customer actor
acts only on the order whose `customer_id` its principal names; any other order is 404
`order_not_found`, so its existence does not leak.

## 7. Payments: what the storefront needs from #4957

#4957's seam stands as written. This spec adds:

- `payment_requests.kind`: `deposit` or `balance`.
- A request records a payment made outside ScadBuddy: cash, Square, a bank transfer.
  The existing `POST /orders/{id}/payments` gains an optional `payment_request_id`; a
  manual payment that names an open request of that order closes it as `paid`.
- The storefront shows the request's hosted URL as "Pay"; a provider's return URL lands
  on the order page, which waits for the webhook, not the redirect, before it says paid.
- The webhook route `POST /api/v1/payments/webhooks/{provider}` stays on the API; it is
  the one back-office route the public reaches, routed by path at the ingress (#4957
  "Deployment").

The storefront ships with whatever providers exist then; without one, "Pay" is the shop's
own instructions text (cash, Venmo handle, …) and the shop records the payment by hand.

## 8. Deposits

- `orders.deposit_minor` (nullable). Set by the policy at submit, changed or cleared by
  the shop at any time before the order is accepted.
- Policy in settings, `orders_deposit_policy`: a list of rules, each a criterion and an
  amount (a percent of the estimate or quote total, or a fixed amount). Criteria:
  `has_request` (an item of kind `request`), `total_above` (minor units), `first_order`
  (the customer has no earlier order that reached `accepted`). A percent applies to the
  estimate total at submit; an item with no estimate (a `request`) counts as zero, so a
  `has_request` rule should be a fixed amount, and the settings page says so. The largest
  amount that applies wins **(call)**.
- When a deposit is set, submit opens a `deposit` payment request for it.
- `deposit_unpaid`: until the deposit is paid, the shop cannot send any revision of the
  order or start a test-print attempt (§4.4) **(call: the deposit protects design work
  and test prints, which come before acceptance; a production print already waits for
  `accepted`)**. "Proceed without deposit" overrides it per order, with a reason.
- The balance request is opened when the order is accepted, for total minus paid.
- `balance_unpaid`: while the balance is above zero, recording a shipment (a pickup is
  recorded as a shipment too) is refused. "Ship without full payment" (`waive_balance`)
  lifts it per order, with a reason in the audit row. The rule applies to every order
  with an open balance request, so a `back_office` order without one ships as today.

## 9. Shipping and tax (the author's calls)

- **Shipping options**, `storefront_shipping_options` setting: a list of `{id, label,
  amount_minor, ships}`; `ships=false` is pickup and needs no address. Submit writes one
  `shipping` charge from the chosen option. The shop can edit it while the order is open.
  The amounts are in the shop's currency (`orders_currency`) and are never converted: in
  the back office, an order in another currency (one made before the shop changed it)
  takes no option (409 `shipping_currency_mismatch`). A cart holds no amount, so a
  storefront submit first gives the cart the shop's currency, and its draft view shows
  that currency and its options. Carrier rates are a later story.
- **Tax**, settings `orders_tax_percent` (default 0, off) and `orders_tax_shipping`
  (bool). The lifecycle keeps one `tax` charge marked `auto` (`order_charges.auto`),
  recomputed in every transaction that changes items, quotes or charges while the order is
  open: percent × (item totals + shipping when taxed), rounded once with
  `orders/pricing.py`'s rule. A tax charge the shop types by hand is not `auto` and is
  never recomputed. This fills in the orders spec's open tax decision (§12) both ways: the
  percent by default, a typed charge when the shop wants it. Tax by region is a non-goal.

## 10. Deployment

- The same image; the storefront is a new Deployment in eh-homelab/clusters with its own
  public host behind Cloudflare and the UDM/ingress path, the back office host unchanged.
- Environment: the CRUD service's in-cluster URL, the projected ServiceAccount token it
  exchanges for its credential (#5325), a signing key of its own for the visitor cookie (never the KEK),
  `SCADBUDDY_STOREFRONT_PUBLIC_URL`, `SCADBUDDY_TRUSTED_PROXIES`. No
  `SCADBUDDY_DATABASE_URL`, no Temporal address and no `SCADBUDDY_SECRET_KEY_FILE`: mail
  and link tokens are made in the CRUD service.
- A test starts the storefront with no database URL and checks that it opens no Postgres
  connection (#5295: no untrusted or public-facing pod holds a database secret).
- Public host: `michaels-workshop.nullreference.io` (the owner, 2026-11-07, #5284).
- README "Deploying" gets a Storefront section.

## 11. Phases

1. **Revisions in the back office**, #5280 (§4, §6): the revision table and the reference
   authority, `request` items, attachments on revisions, test-print attempts, approval
   mode, photos to the model, the agent tools. Useful at once for orders by message.
   Unblocks the assistant's request-to-model flow (another session's work).
2. **The payments seam**, #4957, built whole (registry, credentials, webhooks, a fake
   provider) with §7's additions, plus deposits (§8) and shipping and tax (§9) in the back
   office, #5281. Stripe (#4958) can follow at once.
3. **The public site, read and customize**, #5282: the process, its internal CRUD
   routes, listings and the Storefront page, visitor tokens, customize, render limits, estimate, drafts and the
   draft sweep (§5.1, §5.3, §5.4).
4. **Submit and follow**, #5283: submit, accounts, order links, mail, the customer's order page,
   revision answers and payment, abuse checks (§5.2, §5.5–5.8).
5. **Go live**, #5284: the clusters Deployment and host, README.

Each phase gets its own plan under `docs/superpowers/plans/`.

## 12. Left to the builder

The exact column types and index choices; the storefront page's look (the
`frontend-design` skill applies); copy text; the visitor cookie's name; how the shop's
Storefront page previews a listing; the order of routes within a phase.
