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
  (spec §7). The mocks open one order with the token `tok`.
  Accounts (#5283, spec §5.2): `pages/SignInPage.tsx` (`/account/sign-in`; the address and
  the mailed code, the code filled from a link's `#code=` and then dropped from the
  address; Turnstile `sign_in`) and `pages/AccountPage.tsx` (`/account`; "Your orders",
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

- Customer messages (#5660): Settings → Orders' `components/settings/CustomerMessaging.tsx`
  (bind, check, test send, unbind) and the order page's `components/orders/OrderMessages.tsx`
  ("Mark sent", "Send again" through a preview). The list and "Mark sent" go through the
  backend (`api/client.ts` `orderMessages`, `markOrderMessageSent`); the binding's check and
  test send, and the preview and send, go through the agent's routes (`api/orderMessages.ts`).
  An open preview keeps one `Idempotency-Key` for all its sends, so a retry after a lost
  answer is served from the first claim. Mocks `src/mocks/features/orders/messages.ts`.

## See also

- Real-stack e2e: see `e2e-stack/CLAUDE.md`. Generated API files: see the root `CLAUDE.md`.
- Backend side of the Orders UI, Activity UI and `LanRunControls`: see `backend/CLAUDE.md` (orders, activity, hot path).
- Bambuddy iframe facts (`embed.ts`, `useFullscreen.ts`): see the root `CLAUDE.md`.
