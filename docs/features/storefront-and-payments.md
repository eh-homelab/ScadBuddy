# Storefront and payments

## What it does

A public shop: published listings that a visitor can customize (colours, text, size)
with a live render and a price estimate, a cart, design requests, "print my model"
uploads, passwordless accounts, an order page, and payment through Stripe, PayPal or
Square. In the back office, **Sell on storefront** turns a model into a listing, and the
Storefront page edits, prepares and publishes listings. See README
["Storefront"](../../README.md#storefront-5284) and
["Payment webhooks"](../../README.md#payment-webhooks-4957).

## How it works

The storefront edge is its own process with no database, KEK or Temporal access. Every
call it makes is a `crud.storefront` route on the API's CRUD port. Payment webhooks are
served by the API, not the edge. Customer uploads are checked by the intake worker in a
sandbox before they are used.

```mermaid
sequenceDiagram
    participant V as Visitor browser
    participant E as Storefront edge :8083
    participant API as API (CRUD :8082)
    participant R as Render worker
    participant I as Intake worker
    participant P as Payment provider
    V->>E: GET /api/store/v1/listings, POST /renders
    E->>API: /api/v1/internal/crud/storefront/… (service credential)
    API->>R: TemplatePipeline (render)
    V->>E: POST /draft/items, /draft/submit
    E->>API: submit_draft → order
    V->>E: POST /uploads (own model)
    API->>I: CustomerFileCheck (customer-files queue)
    V->>E: POST /pay/checkout
    E->>API: open provider page
    P->>API: POST /api/v1/payments/webhooks/{provider} (signed)
```

## Code

- Edge: `backend/scadbuddy/storefront/__main__.py:24-48`, `app.py`, `routes.py:83`
  (visitor routes `:117-760`), `settings.py:18-26` (what it refuses).
- CRUD side: `backend/scadbuddy/api/crud_storefront.py:208`, `backend/scadbuddy/orders/storefront/`
  (`listings.py:230`, `renders.py`, `drafts.py`, `submit.py:118`, `accounts.py`,
  `mail.py`, `turnstile.py`, `prepare.py`, `intake.py`).
- Back office: `backend/scadbuddy/api/storefront.py:51`, `frontend/src/pages/StorefrontPage.tsx`,
  `frontend/src/components/storefront/ListingEditor.tsx:488`.
- Public UI: `frontend/src/storefront/` (router `App.tsx:86-97`, client
  `api/client.ts:67`); its OpenAPI is exported by
  `backend/scadbuddy/tools/export_openapi.py` `--storefront`.
- Payments: `backend/scadbuddy/payments/` (`registry.py:13-21`, `stripe.py`, `paypal.py`,
  `square.py`, `service.py:171`); webhook route `backend/scadbuddy/api/payment_webhooks.py:58`.
- Intake: `backend/scadbuddy/intake_worker/`, `backend/scadbuddy/workflows/staged_file.py`.

## Data

- Tables: `storefront_listings`, `storefront_renders`, `storefront_visitors`,
  `storefront_accounts`, `storefront_sessions`, `storefront_mail`, `storefront_uploads`,
  `storefront_order_links`, `payment_credentials`, `payment_requests`, `payment_events`.
- Blob keys: `listing-media-<sha256>`, `customer-preview-<sha256>`,
  `customer-file-<sha256>`.
- Settings: `storefront_*` (`backend/scadbuddy/core/settings.py:228-310`); provider
  credentials are sealed rows, entered in Settings.

## Tests

- Backend: `backend/tests/storefront/` (the edge), `backend/tests/orders/storefront/`,
  `backend/tests/api/test_crud_storefront_*.py`, `backend/tests/payments/`,
  `backend/tests/intake_worker/`, `backend/tests/test_prepare_listing_workflow.py`,
  `test_staged_file_workflow.py`.
- e2e: `frontend/e2e/storefront.spec.ts`, `storefront-account.spec.ts`,
  `storefront-models.spec.ts`, `storefront-sell.spec.ts`, `storefront-admin.spec.ts`.

## Limits and known issues

- The estimate comes from volume, not a slice
  ([#6153](https://github.com/eh-homelab/ScadBuddy/issues/6153)).
- Customer model imports after the check are a later phase
  (`backend/scadbuddy/workflows/staged_file.py:17`;
  [#6133](https://github.com/eh-homelab/ScadBuddy/issues/6133)).
- Venmo's pay page is off (`backend/scadbuddy/payments/venmo.py:48`;
  [#4960](https://github.com/eh-homelab/ScadBuddy/issues/4960)).
- Listings refuse pipeline templates ([#5701](https://github.com/eh-homelab/ScadBuddy/issues/5701));
  colours-only picking ([#6258](https://github.com/eh-homelab/ScadBuddy/issues/6258)) and
  swatches ([#6215](https://github.com/eh-homelab/ScadBuddy/issues/6215)) are open.
