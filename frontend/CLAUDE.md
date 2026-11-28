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

CI runs the lint, typecheck and build in the `frontend` job, the unit tests in three
`frontend-unit` jobs (`pnpm test --shard=N/3`; in one job they took 11 to 16 minutes and
cancelled it at 20, #6783), and Playwright
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
  Its colour parts are one block, `components/ColourParts.tsx` (#8127): from `colourParts.ts`
  `GROUP_FROM` parts in two or more groups it shows the schema's groups, the first open, a
  closed one as a strip of its colours, and an "All N parts" row; each part is a compact
  `ColourPicker` row whose grid shows the colours the design uses first. The mock `l5`
  (`DOLLHOUSE`) has 31 parts in five groups.
  `vite dev` and
  `vite preview` serve the page under `/store` (`storefrontPreview`); its mocks are
  `src/mocks/features/storefrontPublic.ts`, its e2e `e2e/storefront.spec.ts` (390 px wide).
  The price (#6153, #6921): `pages/CustomizePage.tsx` reads `store.variant` (the instant
  read, `POST /listings/{id}/variant`) first on load and on each change. A cached preview
  shows at once; with it and a `slice` price the page posts no render (the add takes the
  variant the shop priced). Otherwise it renders and reads the estimate as before. A
  price, once known, stays on screen: `components/Estimate.tsx` `updating` marks one that
  is not yet the firm price of the choices on screen, and a `last` price is always marked,
  never firm. A render that ends with no price (refused, failed, too slow, or its estimate
  read failed) drops any other price and a `last` one, so nothing shows as updating when
  nothing is. "Working out the price…" shows only with no price known; "We price this
  design by hand" only when the render's estimate answers `none`. The mock holds a variant
  once its render's estimate answered (`holdVariant` seeds one).
  A file listing (#6710 PR 6, `source` `library_file`): `CustomizePage` hands it to
  `pages/FileCustomize.tsx` before its render effect sees it, so it renders nothing. Its
  parts are colour parameters named `colours.<slot>` (`fileParts.ts` `partsView`), shown by
  `ListingView` under the same `colourPlan` rule; the preview is the plate's held GLB
  (`listing.previews`, a plate chooser over the viewer for more than one), recoloured by
  `Preview` `slotColours` (`lib/partOpacity.ts` `ownSlotMaterials`, `recolourSlots`: a mesh
  with a `scadbuddy_slot` extra takes its slot's colour, one with none keeps its own). The
  price is `store.price` (`POST /listings/{id}/price`), the add sends `values` empty and
  `colours` by slot. A `last` price shows marked as updating while the page reads again; a
  read that ends with no price (a limit, a failure, too slow) keeps it on screen as `Estimate`
  `held` ("This was our last price…"), neither updating nor firm; "We confirm the price before
  you pay" only while the design can be ordered (`orderable`, #7523). Over a limit (429
  `render_limited`, or a `last` with a wait over `LIMIT_WAIT_MS`) the page reads once more on
  its own after the wait (at most `timing.limitMs`), then offers "Try again" once the wait is
  over; a colour change drops the wait. The mock lists `FILE` (`l4`) and every file listing the back office's
  mock published in the tab (`storefront.ts` `publishedFiles`, from `sessionStorage`); e2e
  `e2e/storefront-sell.spec.ts` sells a library file, publishes it and opens its public page.
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
  no colour on offer has starts at the closest one on offer, `fittedDefault`, #6144, #8126, by
  `storefront/closestColour.ts`, whose agent twin is `agent/src/tools/closestColour.ts`), pinned by
  `test-fixtures/storefront/sell-draft-vectors.json`, which the agent's copy
  (`agent/src/tools/storefrontDraft.ts`) is tested against too (its case with `file` is a new
  file listing's body, #6710: `fileTitle`, `fileColours` over `declaredSlots`, `platePictures`; its
  cases with `source` are a pipeline design's, #5701, `designInputs.ts` `soldDesign`, whose agent copy is `storefrontDraft.ts` `soldDesign`); a
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
  Pipeline listings (#5701, spec 2026-11-17 §5): `components/storefront/designInputs.ts` is the
  rules and `DesignBlock.tsx` the block under the Design line, for a commit whose schema has
  `pipeline` (`RevisionSchema`). "Set up the design" (`DesignDialog.tsx`) mounts the template's
  UI over a working copy at the commit's `INPUTS_VERSION` (`designStart`, through
  `inputs/migrate`), or the form of the inputs' own schema; only "Use this design" writes the
  draft (`withDesign`), and it saves no output, preset or print. A save always sends `inputs`
  with their `inputs_v`. `shownParams` leaves out `pipeline_params`; `withoutUnknown` takes
  them off a listing. A refusal `listing_inputs_*` (`listingRefusal` kind `inputs`) is said in
  the block by the schema's titles (`inputsRefusalText`), never by its path. The mocks'
  pipeline template is the dollhouse (`mocks/fixtures.ts` `pipelines`). e2e
  `e2e/storefront-design.spec.ts`.
  File listings (#6710, spec 2026-11-21 §9): the editor's Source block chooses a design or a
  library file (`ListingDraft.source`, `switchSource`); a file comes from `LibraryFilePicker`
  with `types` (3MF and STL). `components/storefront/listedFile.ts` `useListedFile` reads the
  file, its plates and its parts (the publish snapshot's once published, else the file's
  declared slots); `FileSource.tsx` has the picked file, the §9.4 notes ("Use the library's
  version", "Keep the published version"), Parts (a support part read-only) and
  `FilePictures` (`plate:<n>`, `upload:<sha256>`; an upload saves a new listing first). The
  page asks before a save drops a pin (another file picked, or a switch to a design). The Library page's file menu
  has "Sell on storefront…" (`?sell=library:<id>`). The changed-file note compares the
  library entry's `file_hash` (#7073) with the pin, and shows nothing when no hash is stated.
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

- Unknown spools (#7143): `api/unknownTrays.ts` (the list, resolve and dismiss calls; a 404 on the
  list is an empty list with `served: false`, as the area may be off; `UnknownSpoolButton` then says to link from the slot's page, `UnknownSpoolAreaOff`, and the Needs-you source and callouts stay quiet), `lib/unknownSpools.ts` (`useUnknownSpools`,
  read at once, every `ATTENTION_POLL_MS` (15 s), and when the tab shows again; `useUnknownTrayList`;
  `useTagMadeToasts`, one toast for each new spool in `made_from_tags`; `codeFromScan`, `prefillNetWeight`)
  and `components/filament/UnknownSpoolDialog.tsx` (pick a ranked candidate or type or scan a spool
  code, enter the net weight, "Link spool"; "Dismiss" is only for a slot that is no longer there). The tray's
  `seen_at` goes back as listed, never as a `Date`. `needsYou.ts` has the source as the kind
  `unknown_spool`, beside the agent requests, the flow grants and the pairings. Four places open the
  dialog through `useUnknownSpoolDialog` and `UnknownSpoolButton`: the Needs-you list (`AppShell.tsx`),
  Filament → Locations (`LocationsTab.tsx`), a place's page (`LocationPage.tsx`) and the print dialog's
  verdict (`components/print/PrintVerdict.tsx`, from `PrintCheck.unknown_trays`). Mocks
  `src/mocks/features/filament/unknownTrays.ts`, which checks the body and answers each code as Go's
  resolve route does (`resolveWritesOff`, `resolvePendingOnce`; #8489). The dialog has its own text
  for each code; a 404 with no code (the area off) or a 503 `writes_not_configured` names the same
  path as the button (Filament → Locations → the place), and a 503 `resolve_pending` reads the list
  again. The print
  verdict e2e is `e2e/unknown-spool.spec.ts` (the mock tray: `unknownTrays.ts` `UNKNOWN_TRAY_SEED_KEY`).
- Devices (#7905, spec `docs/superpowers/specs/2026-11-27-devices-design.md`, plan Task 9): `api/devices.ts`
  (Go core's `/devices` routes, typed by hand because Python's spec has none; the decoders' bodies and answers are the spec's `TagReadIn`/`TagReadResult` and `api/scan.ts` `CodeScan`), `pages/DevicesPage.tsx` at `/devices`
  (nav "Devices": each paired phone online or offline, its methods, its idle expiry and pairing end, Revoke),
  and `components/devices/`: `AddDevice` (`POST /devices/pairings`; the code as `scadbuddy://pair?url=<encoded>`,
  a QR code and a tap link, moved onto `window.location`'s host by `onThisHost`; `USER_ONLY`, as the code is a
  credential), `DeviceCall` (starts one call, follows `device:call:<id>` with `useSubscription`, reads
  `GET /devices/calls/{id}` on each `'resync'` for the events in log order, polls while the socket is down and past the deadline,
  and gives the parent the result's `read_id`) and `UseMyPhone`, the Scan page's one mount: `nfc.read` or
  `code.scan` on an online phone, then the read to `POST /filament/tags/reads {read_id}` or
  `POST /scan/codes {read_id}`; the pairing button when no phone is online; nothing on a 404. Mocks
  `src/mocks/features/devices.ts` (`PHONE` online, `OLD_PHONE` offline; a call plays its events 400 ms apart
  on the mock socket, `setStepMs` in tests); e2e `e2e/devices.spec.ts`. After a reconnect `lib/realtime.ts`
  resubscribes the `device:call:<id>` topics in frames of at most `MAX_CALL_TOPICS` (8, core's `MaxCallTopics`; a frame with more is refused whole), apart from the other topics.
- Dry boxes and the home return (#7167): a dry box is a dry-storage place of capacity 1 (the
  "Dry box" switch in Filament → Locations' place form, shown only for a dry-storage kind, sets
  capacity 1; a 422 `box_has_many` shows its text). `lib/homeReturns.ts` `useHomeReturns` reads
  `GET /filament/spools?awaiting_home=true` as the unknown trays are read; `needsYou.ts` lists each
  as the kind `home-return` (source `homes`, since `home_asked_at`). The shell owns the dialog
  (`components/filament/useHomeReturnDialog.tsx`, `NeedsYou.answerHome`); the spool page's "Dry box"
  banner opens the same `HomeReturnDialog`. Each place row of the Locations tab that is not a tray
  (a dry box included) has a "Print label" link to the place label (`LocationTree.tsx`). The person always names the spool that goes in, through
  `components/filament/IdentifySpool.tsx` (Scan tag only where Web NFC is; a code field always, with
  "Use camera" where `window.BarcodeDetector` exists, else "Take a photo" through `api/scan.ts`, which
  a 404 hides; Pick by hand; a code that names no thing becomes a "Pick by hand" search, `lib/scanSearch.ts` `searchesSpools`, #8372): nothing is preselected, the unloaded spool included. The box's spool or
  an empty box moves with `if_occupied: 'refuse'`, any other spool with `replace` and
  `expected_occupant_id` (the box's spool it showed; a replace without it is a 422 `expected_occupant_required`), each with `answers`; a 409 `box_full` reads
  the box again. `IdentifySpool` calls `onIdentified(null)` whenever no spool is named any more (a
  new way, an edited code, a failed lookup or read), and a tag's new spool is made through
  `scan/useSpoolFromTag.ts`, which the scan page's `NewTag` shares (required properties; a failed
  bind keeps the made spool). The archive dialog says the grams it books as
  non-print waste (`closing.non_print_waste_g`). Mocks: `mocks/features/filament/index.ts`
  `seedHomeReturn` (S-11 out of the H2C's AMS-A slot 1, home `Dry box 2` holding S-7), or `"1"` under
  `sessionStorage` `HOME_RETURN_SEED_KEY` for the dev preview.
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
- `frontend/src/pages/filament/` and `components/filament/` — the Filament section (#6262, spec
  2026-11-14 §10). `/filament` opens Spools; the tabs are Spools, Types, Presets, Tags, Suppliers, Invoices, Locations,
  Catalog, Metrics, Shopping list and Ledger (`pages/filament/tabs.ts`).
  Presets (#7204, Go core's area `filament-presets`): the Presets tab
  (`pages/filament/PresetsTab.tsx`) lists each preset with its source, version and mirror
  state, and filters by source and by type. A preset's page `/filament/presets/:id`
  (`PresetPage.tsx`) shows its versions, newest first, 10 at a time with "Show more", the
  diff between two versions and the document. Links come from `lib/filamentLinks.ts`. The
  mock is `mocks/features/filament/presets.ts`. A copy, an edit of a manual preset and "Send again" (shown for a `failed` or `pending` mirror, on the tab and the page) are `PresetForms.tsx` and `presetWrites.ts`: one `Idempotency-Key` for each opened form (1 to 200 characters; a copy and an edit need it, a send again does not), sent again after a 503 `preset_write_pending`; clearing a whole list text sends `[]`, and only an empty item inside a non-empty list is refused in the form; a 409 `version_moved` reads the preset again; a 503 `writes_not_configured` says writes are not on yet (`writesOff()` in the mock: core has no `SCADBUDDY_TEMPORAL_ADDRESS`). The mock checks as Go's `writes.go` does: the path id, then the writes, then the key (428 with none, 422 `invalid_input` for a length outside 1 to 200), then the body, and its `isCredential` is Go's rule, U+0130 included. A 404 with no `code` means Python answered
  because core does not serve the area yet. The tab then shows "Presets are not on yet on
  this server." in place of the table, and the page shows it in place of the versions and the
  document (`PresetsTab.tsx`, `PresetPage.tsx`).
  Codes and labels (#6955): `/x/:code` (`pages/CodePage.tsx`) asks `GET /api/v1/codes/{code}` and opens
  the spool, place or item kind (`codeTarget`, `inventoryPaths.kind`), and sends a 404 text with no code shape to `lib/scanSearch.ts` `scanSearchPath` (#8372); `filament/locations/:id` (`pages/filament/LocationPage.tsx`) is a place's page, with its files and links (entity `location:<id>`, #6950). The
  Locations tab's name opens that page, its count badge opens the spools filter, and "Print printer
  labels" opens the labels page with `?printers=`. The labels page also takes `?locations=<id>,<id>`
  (`CodeLabel`; a place that cannot be read is named and the rest are drawn; `?types=`, `?items=` and `?kinds=` draw spool type, item and kind labels the same way, #7169;
  `labelSheet.ts` `httpsOrigin`, `labelBase` and `locationPayload`: the QR of every code label is
  `<base>/x/<code>`, and a payload over 84 bytes is refused with an alert). The printer mode and the
  code-label mode each draw at most `MAX_LABELS` (100) labels, as spool labels do, and none past it
  (#7081); the code-label mode counts `?locations=`, `?types=`, `?items=` and `?kinds=` together.
  The spool and type pages' section "Files, links and properties" (on the spool page, anchor `#files-and-links`) holds
  `components/entities/PropertiesPanel.tsx` (#7026), which lists only the keys the form above it does not show: the spool and type pages and a catalog row's edit form pass `hideKeys={definedKeys(schema)}` (#8227), and the item page passes it while it edits; `lib/filamentSearch.ts` `propertyTexts` makes
  search match property values.
  History and metrics (#6269, plan `docs/superpowers/plans/2026-11-18-filament-history-metrics.md`):
  the spool page's timeline (`SpoolTimeline`, `eventText.ts`), grams-left chart (`GramsChart` over
  `charts/LineChart`) and metrics panel (`SpoolMetricsPanel`); the Metrics tab (`pages/filament/MetricsTab.tsx`:
  range, bucket and group in the address through `metricsQuery.ts`, behind "Remember my view"; its "By material number" table, `MaterialNumberTable`, #7152); the type page's cost calculator (`CostCalculator`, #7152: the sum is `GET /filament/cost-per-kg`'s, and "Use as default cost" only fills the cost field); the
  forecast view of the Spools tab (`SpoolForecast`, `TypeForecastTable`) and the "Consumed" tile. The
  charts are plain SVG in `components/filament/charts/` (`ColumnChart`, `LineChart`, `BarList`, `useWidth`).
  A value's colour follows the value, ranked over 365 days (`MetricsTab` `ranked`); until that request
  returns the chart uses the range's own rank. The forecast table's "Reorder now" badge moves under the type
  name by a container query (`@max-[44rem]`), exactly when the table scrolls sideways. The mocks are
  `mocks/features/filament/metrics.ts`; the burn depends on today, so tests and `e2e/filament-metrics.spec.ts`
  fix the clock at 2026-11-18T12:00Z.
  Purchasing (#6266): `api/filamentPurchasing.ts`, the exact decimal strings of `lib/decimal.ts`
  (money never goes through `Number`), and the mocks `src/mocks/features/filamentPurchasing.ts`.
  `TypePage` shows a saved type's supplier prices (`components/filament/purchasing/PricePanel.tsx`);
  "Use as default cost" only fills the cost field as an unsaved edit, and the Types tab's
  "Newest price" column reads the suppliers' price sheets. The Invoices tab (`InvoicesTab`,
  `components/filament/purchasing/invoicesQuery.ts`: status and supplier in the address, "Remember my
  view", the last supplier) starts a draft from a dropped, chosen or pasted file
  (`InvoiceUpload`), and `/filament/invoices/:id` (`InvoicePage`) is the file beside the typed
  receipt (`InvoiceFile`: a PDF in a same-origin `<iframe>`, never an `<object>`, which the page
  CSP's `object-src 'none'` blocks, and inside Bambuddy's sandboxed frame (`isEmbedded()`) a "Use Open
  or Download" line instead, since Chromium shows no PDF viewer there; a photo in `InvoicePhoto`, zoomed 1x to 8x by the wheel, the
  buttons, a double-click or a pinch, and panned by a drag, #6889): the header saves on blur, the lines through `putInvoiceLines` as they are left
  (debounced, `invoiceLines.ts` checks them first), and Confirm waits for a type on every line, the
  shop's currency and `reconciles`. A confirmed invoice keeps only its note and file. A unit test
  that uploads a file answers the `/file` part itself (jsdom's `FormData` does not reach msw;
  `InvoiceUpload.test.tsx`). The Shopping list tab (`ShoppingListTab`) adds an item from a type or by
  text, marks an item an empty spool added "Auto" with its note linked to that spool (#6868; the switch
  is Settings → Filament's `filament_shopping_auto_add_empty`), marks it purchased (it sorts below the pending ones) or removes it, and confirms "Clear
  purchased" and "Clear all"; the last quantity is remembered (`QUANTITY_KEY`). The Types tab's row
  and `TypePage` add a type to it as one pending spool (one click at a time: the button is off
  while its request is out). `useMoney.format` shows the bare decimal, with no symbol, while the
  shop's currency is unread or failed to read. `InvoicePage` refetches only when no save is in
  flight (it waits and refetches after it), keys the server figures by line id, dims them while
  an edit is unsaved, holds Confirm while a header change is in flight, and on a phone folds the
  file viewer so the form leads. `AddProductSheet`, `message` and `today` are shared in
  `components/filament/purchasing/` (`priceFormat.ts`).
  `/filament/spools/:id` (`SpoolPage`: sections, a "Go to section" select below
  1024 px, `OverrideField` with "Reset to type", `RgbaColourField` with Opaque, Translucent and
  Clear presets, `TagPanel` with "Tag is on spool #N — Move it here", which sends `override: false`, a "Read or write with a phone"
  link to the scan page (#7273), the "Override" mark of an open override binding, the weight-lock line (from `weight_locked`), and
  the override checkbox, on with a tray UUID and any kind but `ntag*` (ticking it sets the kind to `bambu_mifare`; the bind sends the flag only with that kind), and the Override mark, which is read again after every bind, #6264) and `/filament/types/:id`
  (`TypePage`; its Drying section, the temperature and hours a spool's drying form starts
  from unless the spool overrides them in Storage, the spool page saying where they came from
  and that a profile's are the AMS HT's (#6720, R-6714-18); its K table says who set each saved
  row, and nothing on a row edited since, `KValueTable` `saved`, R-6714-19, R-6714-22; its "3DFP id" and "Fetch from 3DFP", `components/filament/ProfileFetchPanel.tsx`, #6625: the fetch uses the saved id, reads the run again every 10 s while it is `running`, lists the printer models and nozzles with no preset yet (`gaps`) when no run is going, and the page shows the presets it set unless they hold unsaved edits; the nozzles are Settings → Printing → Filament's `profile_fetch_nozzles`). `/filament/labels` (`LabelsPage`, `Label`, `labelSheet.ts`, #6264) prints labels from a copy in `#label-print-root`: a QR code of `scadbuddy:spool:<id>` (`labelPayload`) with a quiet zone of 4 modules; the layout (`roll` or `sheet`) is kept in `localStorage` under `scadbuddy.labels.layout`. The size is the label template (#7157: `GET /filament/label-templates`, 50 x 25 mm by default, a custom size, or an Avery sheet printed on its own grid, `[data-sheet-page]`), saved in the stored setting `filament_label_template`. Saved sheet grids (#7216, R-7216-11 to R-7216-13) are a "Saved sheets" group of the template picker (picker value `saved:<id>`; the choice `{template: 'saved', sheet_id}`, which `cleanChoice` keeps whole). "New sheet…" opens `components/filament/LabelSheetEditor.tsx` (it sends numbers, and shows the API's 409 and 422 text as the only check); a chosen sheet has "Edit sheet…" and "Delete" (after a confirm; a deleted choice goes back to the 50 x 25 mm roll). A saved sheet becomes the choice through the same saver, and the page reads `GET /label-templates` again after each save or delete, when its tab is shown again, and before Print, which waits for the newest read (also one a save, a delete or the shown tab starts meanwhile) and draws on its grid, but waits at most 5 s (`labelSheet.ts` `printTiming`; a failed or unanswered read prints with what the page has; R-7216-21, R-7216-22, so a sheet the agent edits meanwhile prints as edited); only the newest read's answer lands, and a delete goes back to the roll only while the choice still names the deleted sheet. `sizeOf` draws a saved sheet on its grid; a stored choice whose sheet the list lacks draws at the default with a note, and "Download" still sends it (409 `label_sheet_gone`). The mocks keep the sheets in `mocks/features/filament/index.ts` `savedSheets` with the route's refusals. The file's format, PDF or PNG, and a PNG's dpi (203, 300 or 600) are `components/filament/LabelOutputPicker.tsx`, saved in the stored setting `filament_label_output`. The same setting holds the spool label's fields (#7216, R-7216-9), chosen in `components/filament/LabelFieldsPicker.tsx` (a checkbox per field; the last checked box is off); code labels show a note instead, since they print their code and name. `labelSheet.ts` `spoolLines` makes the backend's line texts and sizes, and `fitLines` leaves off lines from the bottom by line heights only, as `label_pdf.py` `fit_lines` does (R-7216-10, the same constants; `test-fixtures/filament/label-fit-vectors.json` holds them and fit cases, which `labelFitVectors.test.ts` and the backend's `test_label_fit_vectors.py` both run, R-7216-19), so `Label` (`fields`) keeps the PDF's lines; with no `qr` the text column takes the QR box's width. One saver writes the whole value (R-7216-17): a pick changes only its part (the format, the dpi or one field's box: a tick is "this field on" or "this field off", `labelSheet.ts` `tickField`, R-7216-18), keeps the other parts as read or picked, and saves no part the page has not read; a pick before `GET /settings` answers waits for it and is saved once (each tick applied in turn to the stored fields; a tick that would leave no field is not applied), and after a failed read no pick is saved. The note's first line strips Python's whitespace set (`noteLine`), not JavaScript's `trim()`. "Download" sends the fields shown. Both settings save through `lib/useSettingSaver.ts` (R-7157-9, R-7216-16: one `PUT /settings` at a time, a newer value replaces one that waits, only the newest save's failure sets `failed`, and `savedBeforeRead` keeps a pick over a stored value read after it). "Download" (its `title` names the format) saves `api/filament.ts` `labelsFile` (`POST /filament/labels`: a PDF, one PNG, or a ZIP of PNGs) through `downloadBlob`, named by `fileName` from the answer's type, and is off, with a note beside it, for code labels while Settings gives no label address (no label address and no https public URL: the route refuses them `no_label_base`) (mocks: `mocks/features/filament/index.ts` `LABEL_TEMPLATES`; the mock `POST /labels` answers one byte of `image/png` for one PNG page and `application/zip` for more pages, counted as `label_pdf.py` `page_count` does). The Spools tab imports a CSV (`SpoolImport`: a dry run, Import is off while any
  line is an error). Search (#6894): the Spools and Types tabs filter their loaded lists by
  `q=` (`FilamentSearchBox`, "/" focuses it) with `lib/filamentSearch.ts` (every token must
  match a field; `#87` is spool 87 only; a hex with or without `#`; SKUs from the suppliers'
  products, trays from `/filament/inventory`, each spool's open tag's UID and tray UUID from 4 hex digits
  (#7151, `tagsBySpool`), and each spool's and type's property values (#7026; a spool also by its type's, and a `key:value` token equals one key's whole value, GTIN-14 for digits, #8372; with `from=scan` the Spools tab shows no rows until the list and both property reads settle, then opens a single match),
  read by `readKindPropertyTexts` on every load of the tab's list; a failed reload keeps the values it had); when any search matches spools that the list filters hide, the tab names those filters ("Spools match, but these filters hide them: …") and offers a "Clear filters" button (#8411), and every Filament tab has the quick find
  (`FilamentQuickFind`, Ctrl+K or ⌘K: spools and types in groups, Enter opens one), which shares the
  colour picker's remembered filters (`lib/filamentPickerFilters.ts`). The agent's
  `filament_spool_list` and `filament_type_list` `q` run a copy (`agent/src/tools/filamentSearch.ts`), which matches property values too; both tests run `test-fixtures/filament/search-vectors.json`. The price book is edited on Filament → Types, not in Settings; the ledger
  tab asks `GET /filament/events` with `since` and `until`. Settings keeps a Filament section for
  configuration (`pages/settings/sections/filament.tsx`). The Catalog tab's Colours (#6907) adds
  `ColourSources` ("Sync from filamentcolors", "Add Bambu Lab colours", each run's progress read
  while it runs) and a server search (`q`, at most `SHOWN_MAX` rows drawn, no re-order); mocks in
  `mocks/features/filament/index.ts` (`MOCK_SYNC_COLOURS`). The type and spool pages'
  `RgbaColourField` has "From the catalog" (`CatalogColourPick`, #6912, R-6912-1 to 5): the
  same server search, and a pick sets the whole `#RRGGBBAA` and the extra colours. #6265 (plan
  `docs/superpowers/plans/2026-11-18-filament-bb-sync.md` Decisions 3, 16, 19): "Sync with
  Bambuddy" has one `ChoiceSelect` row per data type (spools and types, weights, tags,
  suppliers and prices, locations, presets and K values; `SYNC_ROWS`, `SYNC_KINDS` in
  `pages/settings/values.ts`) with Off, ScadBuddy → Bambuddy, Bambuddy → ScadBuddy or Both
  ways (newer change wins), saved with "Save Filament" as `filament_sync`; the sync status
  (`getSyncStatus`, `GET /filament/sync/status`: the last pass, why none runs, and each
  item whose last write failed or was held back, with `syncErrorText`); and "Sync now"
  (`runSync`, `POST /filament/sync/run`), after which the status is read again until a
  pass has started at or after the run's `requested_at` (R-T11-2, #6836). Under the status,
  "Recent passes" (#6905, `SyncPassesRow`, `listSyncPasses`, `GET /filament/sync/passes`) lists
  the passes newest first, 10 a page with "Show older passes", read again when the status's last
  pass moves, and "Keep sync history for (days)" is the stored `filament_sync_pass_ttl_days`
  (empty is 30). While the status says
  `instance_changed` it offers "Reset Bambuddy links" (#6773, `ResetLinks`: a dry run's
  counts in a confirm dialog, then `resetSyncLinks` with the dry run's `instance`; the mock's
  `linkOtherBambuddy` sets that state; with the dry run's `same_bambuddy` the dialog is "Keep the
  Bambuddy links?" and the reset sends `expect_kept` and deletes nothing, #6845,
  `linkOtherBambuddy(id, { same: true })`; a 409 `proof_changed` reads the dry run again and
  says why, `loseSameBambuddy`; a dry run with `changed_since_pull` says every linked spool (or, with no spool linked, every linked filament preset) ScadBuddy could compare (a time on both sides) changed in Bambuddy since ScadBuddy last saw it, and offers the delete, `linkOtherBambuddy(id, { changed: true })`; a dry run that cannot read Bambuddy notes it and offers the
  delete, `linkOtherBambuddy(id, { unreadable: true })`). It also has the "RFID override mode" switch with "Apply now", #6264, and above it the stored switch `filament_auto_add_unknown_rfid` (#7156; the mode's status line says when that setting holds Bambuddy's auto-add off, and both statuses are read again after "Save Filament"), and the drying defaults `filament_drying_temp_c` and `filament_drying_hours` (empty offers none; the spool page then says "the default in Settings");
  the mocks for the sync routes are `src/mocks/features/filament/sync.ts` (its spool import answers
  503 `import_unfinished` after `leaveImportUnfinished`, or with any value under `sessionStorage`
  `IMPORT_UNFINISHED_SEED_KEY` for the dev preview, #6261), the e2e is
  `e2e/filament-sync.spec.ts`; the old anchors `#native-spools` and
  `#inventory` land on it. Colour is `#RRGGBBAA` (`lib/rgba.ts`). Mocks:
  `src/mocks/features/filament/` (`seed.ts` has 12 spools that cover the cases; `rfid.ts` owns
  `/filament/rfid/*`; `tags.ts` owns the phone page's routes, `/filament/tags/reads` (a code in
  the records resolves through `index.ts` `resolveCodeText`, which `GET /codes/{code}` shares), the backups,
  `tag-records`, `tag-written` and the restore, and the agents' `/filament/nfc-jobs`, with
  `seedNfcJob`; its test `tags.test.ts` holds it to the real routes' order and answers).
  `/scan` (`pages/filament/ScanPage.tsx`, #7273, spec
  `docs/superpowers/specs/2026-11-23-web-nfc-tags-design.md`; the hand-held Scan mode, plan
  `docs/superpowers/plans/2026-11-25-scan-mode.md`) is the phone page, with its own nav entry
  "Scan" (`AppShell.tsx` `NAV`; `filamentLinks.scan`; the old `/filament/scan` redirects): Web NFC
  (`lib/webNfc.ts`, `NfcSession`; Chrome on Android, top level only), one tap's read posted to
  `POST /tags/reads`, which opens what the tag names: the spool that holds it (`KnownTag`), the
  thing a typed code or `scadbuddy:spool:<id>` text in its records names (`next` `open_code`,
  `resolved` in the `/codes/{code}` shape; `components/filament/scan/CodeCard.tsx`, with
  `lib/resolvedCode.ts` for the label "Place L-4" / "Spool S-142" / "Type T-4" / "Item I-7" / "Kind K-3" and the target page; a kind opens `inventoryPaths.kind`), or the
  New tag card. The switch "Open what I scan at once" (localStorage `scadbuddy.scan.openAtOnce`,
  `"1"` when on, read in try/catch) navigates to the thing on each `open_spool` or `open_code`
  read and starts the scan by itself when the page opens, falling back to the Scan button with
  the reason when the browser refuses. A spool's "Write tag", a backup's restore and "Clear tag" (#8362,
  `tagWrite.ts` `clearTag`: backup `clear`, one empty record, read back, then the unbind with
  `reason=cleared`; never a Bambu tag, only the tag on screen) go through
  `lib/tagWrite.ts` `writeWithBackup` (backup, write, read back; in the ScadBuddy app a record
  larger than the reading's `tag.max_ndef_bytes` stops before the write), and an agent's NFC job shows
  in `NfcJobBanner` (`components/filament/scan/`); tests stub Web NFC with `src/test/ndefStub.ts`.
  A read posts the app's chip, `max_ndef_bytes` and `writable` (`webNfc.ts` `chipFields`, #8366), and
  the Tags tab's Chip column shows the tag's last known ones; with none, "Read chip" queues an NFC `read`
  job (`POST /nfc-jobs`, as the agent's `nfc_probe` does), reads it every `PROBE_POLL_MS` and cancels it on
  Stop or when the tab goes. A Bambu tag shows "MIFARE Classic" and offers no read.
  "Scan code" (#8155, `components/filament/scan/ScanCode.tsx`) shows the rear camera's live
  preview, or a chosen photo where the browser gives no camera (inside Bambuddy's frame too).
  An unresolved code that `searchesSpools` allows gets a "Search spools for" link (#8372).
  `ScanCameraProvider` wraps the page; `useScanCamera()` (`scanCamera.ts`) gives any read the
  camera: `capture(source)` takes a still of the preview as a JPEG (canvas `toBlob`), stores it
  (`POST /scan/images`, the bytes as the body) and decodes it on the server (`POST /scan/codes`
  with the key, the source and a GPS fix that waits at most 3 s), and answers `{key, mediaType,
  scan}`. These routes are core's (Go), so `api/scan.ts` writes their shapes by hand; mocks
  `src/mocks/features/scan.ts` (`queueScanCodes`: the mock reads no image). e2e: `e2e/filament.spec.ts`, `e2e/filament-rfid.spec.ts`,
  `e2e/filament-sensors.spec.ts` (a place's sensor, its limit and history, #6268) and
  `e2e/filament-purchasing.spec.ts` (a supplier, a price, an invoice confirmed into spools, and a
  total that cannot be confirmed), plus `hot-path.spec.ts` (SB spool numbers in the trays, no Bambuddy
  call), `settings.spec.ts` and `page-titles.spec.ts`. Sensors and weights (#6268):
  `LocationSensors` and `SensorChart` on the Locations tab (#6646: the "Notify me through Bambuddy" box in the add and edit forms, and the newest `last_notification` under the sensor: sent, waiting (within about a minute while a Bambuddy URL is set, else "once Bambuddy is connected in Settings"; no promise before settings load), or not sent ("may not have been sent" for `interrupted`) with `NOTIFY_FAILURE`'s reason for each `error` code; the seed's shelf sent one, the dry box's failed `scope`), `NeedsDrying` and `outsideDry.ts`,
  and the Spools tab button "Sync weights from AMS". Type proposals (#6701): `TypeProposals` ("Create types") shows on the Needs-type filter, over
  `GET /filament/type-proposals` and its confirm; the mocks group the seed's untyped spools (#6 is one
  group, #7 lacks a label weight).
- A flow run's questions (#7364): `components/flows/PendingCard.tsx` draws `TaskCard` for an
  entry with a `task` (a `prompt`, or a `wait_for_human` since #8062) and the text answer for a
  `wait_for_human` from before #8062, on the run's
  page and the order's (`OrderFlowRuns`). The card's key is the run's execution and the task's
  id, so a Reset gives a new form. `TaskCard` reads the task again on `task:<id>`
  (`lib/flows.ts` `taskTopic`) and shows the newer of its read and the page's. Both answer
  forms are `USER_ONLY`. A question the flow asked of people only says so (`pendingParts.tsx`
  `PeopleOnly`); the run's steps and the task card name an assistant that answered and link its
  session (`AnsweredBy`, `lib/flows.ts` `answererLabel`), and say when the run's grant let it
  answer. Settings, Assistant, "Flows' questions" (`pages/settings/sections/flow-answerers.tsx`)
  is `flow_answerers` (#7471); it and a run grant's `answers` in `GrantEditor` share
  `components/flows/AnswerersPicker.tsx`.
  `SchemaForm` draws a form from a schema, for an answer (`answer`) and
  for Start run; `inputsForm.tsx` `useInputs` holds the fields, `lib/flowInputs.ts` the parse:
  an answer leaves out a blank field, Start run sends a cleared text. Mocks:
  `src/mocks/features/tasks.ts` (the task of `run-prompt`, and a scan task at the Workshop).
  e2e `e2e/workflows.spec.ts`.
  Task screens (#8062): `components/flows/TaskPanel.tsx` is a task's context and answer form,
  shared by `TaskCard`, the station and the code page. `ContextBlocks.tsx` draws the checked
  context blocks. `components/flows/widgets/` holds the controls of `x-sb-widget`
  (`ScanField`, `UploadField`, `LibraryField`, `Stepper`, `Segmented`); `lib/flowInputs.ts`
  reads `widget`, `expects` and `kind`, and `lib/scanMisfit.ts` says why a code does not fit.
  `pages/StationPage.tsx` is `/station` and `/station/:place` (a location's id; it lists the
  tasks whose place is that location's code, `L-7`), and `pages/CodePage.tsx` offers
  the task that waits for a scanned code before it opens the place or spool. The place's
  "Station for flow tasks" switch is in `pages/filament/LocationsTab.tsx`. e2e
  `e2e/flow-station.spec.ts`.
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
- Hosted plugins (#6937, spec 2026-11-21 §6.2, §6.3): Settings → Tools & plugins → Hosted
  plugins (`pages/settings/sections/hosted-plugins.tsx`, `components/settings/HostedPlugins.tsx`,
  `HostedPluginEditor.tsx`; client `api/hostedPlugins.ts`, file bytes as the raw body through
  `client.ts` `requestRaw`). A draft file opens in Monaco (`HostedFileEditor.tsx`, a lazy chunk;
  unit tests mock it with a textarea). `hostedDraft.ts` checks paths first (the API's refusal is
  shown as it is) and works out `offerState` from the package row. A Publish offers the new
  version at once (spec §6.3 step 5): the agent's package install with `{ kind: 'hosted', name }`,
  an unapproved install or a re-pin, never an approval. Offer runs the same install for a version
  a failed offer left unoffered. Publish, Offer and Delete are `USER_ONLY`. Close, or Edit on
  another plugin, asks before it drops unsaved files. The two
  package panels tell each other of a change (`packagesChanged.ts`), and both, with the open
  editor, read again on the realtime topic `plugin-packages` (`plugin_packages.changed`, #7460),
  so a publish, re-pin, approval or delete made elsewhere shows without a reload. Mocks
  `src/mocks/features/hostedPlugins.ts` (seeds `print-notes`, version 1 never offered), which
  tell `mocks/aiPlugins.ts` what an Offer installs (`setHostedLookup`). e2e `e2e/hosted-plugins.spec.ts`.
- `frontend/src/lib/appLinks.ts` (`appUrl`, `useAppUrl`, `components/AppLink.tsx`) — links
  to ScadBuddy's own pages use the host the user is on: a path, or a URL on a known origin
  (`public_url` plus `SCADBUDDY_ALLOWED_ORIGINS`), is put on `window.location.origin`
  (#6232). `public_url` is only for links that leave the browser. Bambuddy links:
  `lib/bambuddyLinks.ts`.

## See also

- Real-stack e2e: see `e2e-stack/CLAUDE.md`. Generated API files: see the root `CLAUDE.md`.
- Backend side of the Orders UI, Activity UI and `LanRunControls`: see `backend/CLAUDE.md` (orders, activity, hot path).
- Bambuddy iframe facts (`embed.ts`, `useFullscreen.ts`): see the root `CLAUDE.md`.
