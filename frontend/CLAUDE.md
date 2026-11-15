# Frontend — notes for agents

These notes cover `frontend/`; paths are repo-relative; the root `CLAUDE.md` has the repo-wide rules.

## Commands
Frontend (`frontend/`, Node 24, pnpm via corepack from `packageManager`):

```bash
cd frontend
corepack enable
pnpm install --frozen-lockfile
pnpm lint && pnpm typecheck && pnpm test && pnpm build
pnpm exec playwright test         # msw-mocked e2e against `pnpm preview` of the bundle
```

CI runs the lint, typecheck, unit tests and build in the `frontend` job, and Playwright
in three `frontend-e2e` jobs of its own (`--shard=N/3`, one worker each);
`frontend-e2e-split` checks that the three selections add up to the whole `--list` and
that each shard ran what it selected, as `backend-pytest-split` does.
`e2e/real-backend.spec.ts` skips unless `E2E_BASE_URL` points at a running container.
`e2e/real-agent.spec.ts` also needs `E2E_AGENT=1`: a stack whose one origin routes
`/api/v1/ai/*` to a real agent with a credential pointed at a fake Anthropic endpoint
(its header lists the script `E2E_AGENT_SCRIPTED=1` expects; `pnpm preview` routes the
same way, `vite.config.ts`).
- `pnpm exec tsc -p tsconfig.app.json --noEmit` - the quick typecheck. It skips
  `pretypecheck`, so run `pnpm gen:api` once first, or the generated `src/api/schema.d.ts`,
  `src/storefront/api/schema.d.ts` and `src/api/settingDocs.json` (the Settings field docs
  `mocks/fixtures.ts` imports) are missing.
  Run it again after a merge or rebase onto main: a stale `schema.d.ts` gives type errors
  about fields the merge added or removed.
  Playwright's web server runs `pnpm build`, which typechecks first, so one type error in
  a test also fails all three `frontend-e2e` shards (#6238).
- No prettier here; eslint is the only lint and format check.
- Shared test helpers go in `src/test/` (e.g. `src/test/appLinks.ts`), not copied per file.
- The pending-input stream (`lib/pendingChanges.ts`) opens in a passive effect after the
  first paint: a test that ticks a fake `EventSource` waits for `stream.open()` first (#6287).


## Layout
- `frontend/src/` — React 19 + Vite; `src/mocks/` is the msw API used by vitest and
  the mocked e2e run. A new feature's mocks go under `src/mocks/features/`, in
  `<feature>.ts` or a `<feature>/` folder. Every `.ts` file there except tests is picked
  up without editing `handlers.ts`, and must export `handlers` (and optionally
  `reset`) (#508).
- `frontend/src/lib/tracing.ts` — the browser's OpenTelemetry (#988), a lazy chunk
  `main.tsx` loads after the first paint; spans leave through `traceScrub.ts` and
  `relayExporter.ts` to the backend relay `/telemetry/v1/traces`, and stop for the
  page's life when it answers `X-ScadBuddy-Tracing: off` (`startTracing` then undoes
  itself, so no `traceparent` is sent; msw always answers off,
  `src/mocks/features/telemetry.ts`). A user action is `traceAction`
  (`lib/traceAction.ts`, entry chunk, API only): a request issued after an `await` joins
  the action's trace only inside its `within`. `traceparent` goes on same-origin
  requests only. The chunk loads through `loadOptionalChunk` (`lib/staleChunks.ts`), so
  a blocked one (an error naming `tracing-<hash>.js`, or Safari's naming no URL while it
  loads) does not trigger the stale-chunk reload; any other chunk's error still does.
- `frontend/src/template-ui/` — template-owned UIs (#425): `host.ts` (Host API v1 over the page's
  inputs), `TemplateUi.tsx` (loads `ui/<module>` with `import()`, mounts into a shadow root, and
  reports a failure through `onFailure`; the Customize page then falls back to the generated form
  with a banner), `elements.ts` (`sb-param`/`sb-preview`/`sb-generate`, rendered by portal). Inputs are `{params, v, …ui state}` (`backend/scadbuddy/render/inputs.py`);
  `backend/scadbuddy/api/template_ui.py` serves `ui/**` live and at `/versions/{commit}/`;
  `api/static.py` `PAGE_CSP` (mirrored in `frontend/page-csp.txt`) is the page's policy.
  Its one `script-src` hash is `frontend/index.html`'s inline entry-reload handler (#4873):
  a page whose entry `index-*.js` or CSS 404s mid-rollout reloads, up to 3 times per failed
  URL, for parse-time tags only (a lazy chunk's CSS is `staleChunks.ts`'s, #4874). Editing
  that script changes the hash; `src/entryReload.test.ts` names the new one.

- `frontend/src/storefront/` — the public storefront page (#5282, spec 2026-11-02 §5): a
  second Vite entry, `storefront.html`, that the storefront edge serves on its own host
  (`backend/scadbuddy/storefront/static.py`: the page and only the chunks the
  `storefront.html` entry loads, read from `dist/.vite/manifest.json`). Its client
  (`api/client.ts`) calls the edge's `/api/store/v1`, typed by `api/schema.d.ts` from the
  edge's spec; every write sends one `Idempotency-Key` per visitor action, which the edge
  forwards bound to the visitor. It imports nothing from the back office:
  `eslint.config.js` `storefrontImports` refuses its pages, any `api/*` module but the
  types-only `api/types`, its feature components and `agent/` (`lint.test.ts`), and CI's
  `frontend` job runs `pnpm check:storefront-bundle` after the build
  (`scripts/check-storefront-bundle.mjs`: no back-office API path and no mocks in the
  entry's closure). So the shared viewer takes its unit as a prop (`Preview` `unit`), and
  only the back office fetches the stored one (`lib/loadDisplayUnit.ts`). The customer's
  form is `components/ListingView.tsx`, which the back office's Storefront page mounts as
  its live preview (the back office may import from `src/storefront`, never the reverse).
  `vite dev` and
  `vite preview` serve the page under `/store` (`storefrontPreview`); its mocks are
  `src/mocks/features/storefrontPublic.ts`, its e2e `e2e/storefront.spec.ts` (390 px wide).
  The bot check (#5283): `turnstile.ts` reads the site key (`GET /api/store/v1/turnstile`)
  and loads Cloudflare's script only when `components/Turnstile.tsx` mounts. A form that
  takes the check sends the widget's token as `X-Turnstile-Token` and draws a new widget
  after each send, because a token passes one check only. The mocks keep the check off.
  The customer's order page (#5283, spec §5.6) is `pages/OrderPage.tsx` at `/o#<token>`: it
  reads the order link's token from the fragment and posts it in each body (never a path or
  the history); a 404 with no body is a link that opens nothing. It says "Paid" only from
  the edge's answer ("Check for updates" reads again), never from a provider's redirect
  (spec §7). The mocks open one order with the token `tok`. Its pay link keeps
  `rel="noreferrer"` (a provider's hosted page never learns `/o?order=<id>`), so the pay
  page (`pages/PayPage.tsx`) cannot read the referrer to offer "Back to your order": a
  click on a same-origin pay link sets the `sb_pay_from_order` sessionStorage flag
  (`payReturn.ts`), and the pay page reads and clears it (and needs `history.length > 1`).
  No flag, or blocked storage, shows only the mail-link line and "Your orders".
  Accounts (#5283, spec §5.2): `pages/SignInPage.tsx` (`/account/sign-in`, two steps:
  "Send me a code" mails a code to the address, Turnstile `sign_up`, and says the same for
  every address; then the address and the code, Turnstile `sign_in`. A mailed link opens on
  the second step, the code filled from its `#code=` and then dropped from the address)
  and `pages/AccountPage.tsx` (`/account`; "Your orders",
  each linking through `api/client.ts` `ACCOUNT_ORDER_URL` to `/o?order=<id>`, and
  sign-out, which on a 503 `sign_out_incomplete` shows the edge's text). Each order's
  status reads from the page's `STATUS_LABELS`, typed by the spec's status enum (backend
  `OrderStatus`, so tsc names a missing label); a value outside it reads "In progress".
  The edge's two `sign_out_incomplete` bodies are
  `test-fixtures/storefront/sign-out-incomplete.json`: `backend/tests/storefront/
  test_accounts_edge.py` checks the real edge answers them, and the page's test serves
  them. The session is
  the edge's HttpOnly cookie; the page never sees it. Mocks
  `src/mocks/features/storefrontAccount.ts`, e2e `e2e/storefront-account.spec.ts`.
  A customer's own models (#6133 PR 4, spec 2026-11-13 §4, §10.5): `components/ModelForm.tsx`
  at `/print-my-model` (linked from the listings' "Have something else in mind?"). It asks
  the edge for presigned slots, PUTs each file straight to the bucket with XHR
  (`api/client.ts` `putFile`, for the progress bar), tries a failed PUT again on the same
  URL until it expires, then adds the request (Turnstile `model`). `components/PurchaseLinks.tsx`
  is the "things to buy" list it shares with `RequestForm` (the multipart part
  `purchase_links`). `components/ModelSources.tsx`: `CartSources` (the draft page reads the
  cart again while a file is `checking`, `poll.ts` timing) and `OrderSources` (the order
  page reads each preview by a POST of the reader, `orderPreview`, into a `blob:` URL, so no
  URL holds the token). A customer's link is always text, never an anchor. Mocks
  `src/mocks/features/storefrontModels.ts` (the bucket on the page's own origin,
  `/mock-bucket/staging`, because `vite preview` sends `page-csp.txt`, which names no
  bucket; the order `/o#models`). One route has one feature module
  (`mocks/features.test.ts`), so `storefrontPublic.ts`'s `GET /draft` and order view call
  its `advanceChecks` and `modelsOrderOf`. e2e `e2e/storefront-models.spec.ts`.

- "Sell on storefront" (#6131, design 2026-11-13): Generate's menu (`/storefront?sell=job:<id>`)
  and a History row (`?sell=output:<id>`) open the listing editor with the listing filled in,
  unsaved (`components/storefront/sellSource.ts`; the list redirects the address to
  `/storefront/listings/new?sell=…`); `?listing=<id>` redirects to a saved one's editor. The smart defaults are `listingDraft.ts` `defaultsFor` (free text and
  every colour offered, from the priced filaments of the design's material, the rest locked;
  a support setting is never offered, `isSupport`; an offered colour whose template default
  no colour on offer has starts at one that is, `fittedDefault`, #6144), pinned by
  `test-fixtures/storefront/sell-draft-vectors.json`, which the agent's copy
  (`agent/src/tools/storefrontDraft.ts`) is tested against too; a
  colour no listing filament has is `colourProblems`. e2e `e2e/storefront-sell.spec.ts`.

- The Storefront back office (#6157): `pages/StorefrontPage.tsx` is the list, a table the
  server searches, filters, sorts and pages (`GET /storefront/listings` answers 50 rows, the
  matches' `total` and the shop's `places`, the last place a move can name; a page past the
  last moves to the last); `components/storefront/listingsQuery.ts` keeps that state in the URL. Bulk
  Publish, Unpublish and Delete run one listing at a time. "Move to top" and "Move to
  position…" send `POST /storefront/listings/{id}/move`; there is no drag. The editor is
  `pages/StorefrontListingPage.tsx` at `/storefront/listings/<id>` (or `new`); the list's
  links pass its query in the history state, which "Back to listings" returns to. While it
  holds unsaved edits, `lib/useLeaveGuard.ts` holds every in-app navigation away and asks.
  Each offered parameter's "Starts at" (`DefaultField`, #6144) is the value an unpicked render
  takes: the template's while the bounds allow it, else the listing's `default`, which
  narrowed bounds move to the nearest value they allow (`listingDraft.ts` `fittedDefault`). A
  publish refused about one parameter (`listingRefusal` kind `param`) is said under it. The old
  `/storefront?listing=<id>` and `?sell=…` addresses redirect to the editor. Mocks
  `src/mocks/features/storefront.ts`; e2e `e2e/storefront-admin.spec.ts` seeds 2,000
  listings into the mock's `sessionStorage`.
  Materials (#6258): the editor's "Materials allowed" is the listing's `materials`, first
  preferred (`components/storefront/MaterialOrder.tsx`: up, down and remove buttons, the
  moved row's button keeps the focus; "Add a material" offers the price book's customer
  materials); empty shows "Shop default (…)" from `storefront_materials`, which Settings →
  Storefront pricing's `components/settings/StorefrontMaterials.tsx` edits with the same
  list. "Colours on offer" groups the price book's filaments by allowed material, in that
  order, with "Offer all" and "Offer none", support filaments last; a ticked filament of a
  material not allowed is named apart. A publish refused `listing_material_not_allowed` or
  `listing_locked_colours_unprintable` (`listingDraft.ts` `listingRefusal`) is said beside
  "Materials allowed" or "Colours on offer" until the next edit. The mock's publish checks
  only the materials (`materialRefusal`).
  Pictures (#6213): the editor's `components/storefront/ListingPictures.tsx` lists every
  id of the draft's `media_ids` (a video, a missing file or an unknown id says so), orders,
  removes and adds a listing's images, uploads one into the template's media, and marks
  each one Live from the listing's published `media` (`media_id`; a listing published
  before that was kept says to publish again, never matched by place). A template's Media
  dialog has "Storefront…" per image (`components/storefront/StorefrontPicture.tsx`),
  which sets the image on each listing of that template through `PUT
  /storefront/listings/{id}/media`; a published listing is published again at once.
  Prepared colours (#6237): `components/storefront/PrepareColours.tsx`, under a published
  listing's actions, shows the count (`GET …/prepare`), confirms, starts (`POST …/prepare`)
  and shows "n of N ready", read again while the run is open (`running`) or renders are in
  the queue (a failed read does not stop the reads; the next good one clears its error, a
  failed start's message stays, #6299 review); renders that could not be started (`failed_to_start`) are named, with "Prepare
  again"; over the cap it says the count and the cap. Mocks in
  `src/mocks/features/storefront.ts` (`setPrepareCap`, `failNextPrepareStarts`).

- The order lists (#6334): `pages/OrdersPage.tsx`'s views (`lib/orderViews.ts`
  `ORDER_VIEWS`, each a fixed set of URL filters; the active one is the one the URL holds)
  and its closed-order rule: a closed or cancelled order that ended more than N days ago is
  left out by the API's `ended_since` (`endedSince`, local midnight, so the list's key moves
  once a day) unless `old=1` or the query is a profit report row's (`isReportFiltered`: a
  row's orders are mostly closed long ago, and its link lists them all). N, "Remember my view" and the last view's query (never the
  search or a profit report row's filters) live in `localStorage` (`ORDERS_MEMORY_KEY`); a
  visit to `/orders` with no query opens the remembered one. An order's items table puts
  the live lines first and the fulfilled and cancelled ones in groups after them
  (`components/orders/itemGroups.ts`, `DataTable` `groups`), each group's state remembered
  there too. A unit test of the list pins `Date` (`vi.useFakeTimers({ toFake: ['Date'] })`)
  and the e2e `page.clock.setFixedTime`, so the mocks' closed order stays on its side of N.
- Customer messages (#5660): Settings → Orders' `components/settings/CustomerMessaging.tsx`
  (bind, check, test send, unbind) and the order page's `components/orders/OrderMessages.tsx`
  ("Mark sent", "Send again" through a preview). The list and "Mark sent" go through the
  backend (`api/client.ts` `orderMessages`, `markOrderMessageSent`); the binding's check and
  test send, and the preview and send, go through the agent's routes (`api/orderMessages.ts`).
  An open preview keeps one `Idempotency-Key` for all its sends, so a retry after a lost
  answer is served from the first claim. A `storefront_mail` refusal asks "Send it here too?"
  and a row skipped for it offers "Send here too", both sending with
  `also_when_storefront_mails`. Mocks `src/mocks/features/orders/messages.ts`.

- The print watcher (#4699 phase 2): Settings → Printing's "Print watcher"
  (`pages/settings/sections/print-watcher.tsx`; the list fields' form values are
  comma-joined, `values.ts` `sourcesValue`, `printerIdsValue`, `registryIdsValue`), and the Prints page's
  "Unsorted imports" (`components/prints/UnsortedImports.tsx`). The inbox reads ScadBuddy
  only, so it shows on every backend before Bambuddy's lists are asked for. "Link…" opens
  `ImportPrintDialog` with `start`: that print, and the source the watcher recorded chosen
  when the sources list it and it can be used (a kept printer file is its library file;
  the printer is never read for it; a recorded source that is not listed falls back to
  the preselected one), with no way back to the archive picker. "Dismiss" calls the
  dismiss route. While a page of 50 is full, "Show more" reads the next (`before`); once
  every shown print is dismissed, the first page is read again (the functional `setPages`
  update decides it, so dismisses that answer together still read again, #6029). Mocks
  `src/mocks/features/printWatch.ts`.
  #6041, LAN-only printers (no Bambuddy link): the section's "Leave these LAN-only printers
  alone" skips them by registry id (`print_auto_import_skip_lan_printers`, the registry's
  physical printers with no `bambuddy_printer_id`). Unsorted imports lists their inbox
  records in a second list (`GET /print-imports/auto/lan`), each with its printer's and
  job's name, paged on its own (`useInbox`: the record ids are not archive ids). "Link to
  model…" opens a model picker only (`LanLinkDialog`, no source step, no Import print), and
  its answer says the print is in Activity (`prints.ts` `lanPrintsActivityPath`), as a
  model's Prints tab does when the model has such a print (`PrintsPage.tsx`
  `LanPrintsNote`).
- "Needs you" (#6399): `lib/needsYou.ts` is every request that waits on the person in one list
  (the agent's pending input, `attention.ts` `WaitingEntry`; the flow grants of
  `GET /api/v1/flow-grant-requests`; the tab's pairing requests, from `AgentLink`'s
  `onPairings`), which `AppShell` provides as `NeedsYouContext`. The bell
  (`components/activity/ActivityBell.tsx`) lists it first, counts it until it is answered
  (opening the bell clears only the finished rows) and turns `bg-needs` (the `--sb-needs`
  tokens, AA in `theme.contrast.test.ts`) while anything waits; the Activity page's
  `#needs-you` (`components/activity/NeedsYou.tsx`) lists it in full. Each new item is
  toasted and, while the page is hidden, notified (`needsYouNotices`). With every tab closed
  the agent pushes the grants and pairing requests too (`agent/src/notify/waiting.ts`, read
  every 30 s, and pushed only in its first 24 h, so a swept claim never pushes it again), under the same notice ids (`run-grant:<grant id>`, `trigger-grant:<grant id>:<requested_at, epoch s>`,
  `pairing:<id>`), so a push and the page's notification share one tag; a click opens
  `#needs-you` (`public/push-sw.js`). Tests build the same
  context with `src/test/needsYou.tsx` `NeedsYouHarness`; a Playwright test parks agent
  requests with the `sessionStorage` key `scadbuddy.mock.pending-input`
  (`mocks/features/pendingInput.ts`). The mocks hold two requested grants by default
  (`run-ask`, `trig-render`), so the tab title of the mocked app starts with `(2)`.
- `frontend/src/lib/appLinks.ts` (`appUrl`, `useAppUrl`, `components/AppLink.tsx`) — links
  to ScadBuddy's own pages use the host the user is on: a path, or a URL on a known origin
  (`public_url` plus `SCADBUDDY_ALLOWED_ORIGINS`), is put on `window.location.origin`
  (#6232). `public_url` is only for links that leave the browser. Bambuddy links:
  `lib/bambuddyLinks.ts`.

## See also

- Real-stack e2e: see `e2e-stack/CLAUDE.md`. Generated API files: see the root `CLAUDE.md`.
- Backend side of the Orders UI, Activity UI and `LanRunControls`: see `backend/CLAUDE.md` (orders, activity, hot path).
- Bambuddy iframe facts (`embed.ts`, `useFullscreen.ts`): see the root `CLAUDE.md`.
