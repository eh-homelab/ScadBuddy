# Settings, sign-in and access tokens

## What it does

Settings is grouped by area (general, printing, library, assistant, tools, MCP, audits,
system): the Bambuddy connection, printers, spools, print defaults, the blob store,
fonts and libraries, the git remote, the assistant's credentials and limits, tool access,
payments and storefront mail. Sign-in is ScadBuddy's own OIDC login with a break-glass
code; personal access tokens give scripts and `bambuddy-fs` API access; service
credentials are listed per caller. User guide: [Connecting Bambuddy](../user-guide.md#connecting-bambuddy);
README ["Signing in"](../../README.md#signing-in-4975),
["Authentication on the deployment"](../../README.md#authentication-on-the-deployment-4975).

## How it works

Settings are rows in `settings`, seeded from `SCADBUDDY_*` environment variables;
secrets (API keys, S3 keys, the OIDC client secret) are sealed under the KEK. The auth
mode `off | observe | enforce` decides whether a route without a valid caller is refused
(`backend/scadbuddy/auth/check.py:67-93`); the default is `off`
(`backend/scadbuddy/core/settings.py:335`). Design: the
[backend auth spec](../superpowers/specs/2026-11-01-backend-auth-design.md) and the
[service credentials spec](../superpowers/specs/2026-11-05-service-credentials-without-db-design.md).

## Code

- UI: `frontend/src/pages/SettingsPage.tsx`, areas `frontend/src/pages/settings/areas.ts:17-26`,
  sections `frontend/src/pages/settings/sections/*.tsx`.
- Routes: `backend/scadbuddy/api/settings.py:87`, `backend/scadbuddy/api/auth.py:50`,
  `access_tokens.py:25`, `service_credentials.py:27`, `service_token.py:38`,
  `git_remote.py:13`, `store.py:31`.
- Auth: `backend/scadbuddy/auth/` (`gate.py`, `check.py`, `routes.py:22-40`, `oidc.py`,
  `personal_tokens.py`, `service_credentials.py:99-106`, `break_glass.py`).
- Settings store: `backend/scadbuddy/library/settings_store.py`, `backend/scadbuddy/core/settings.py`.

## Data

- Tables: `settings`, `auth_sessions`, `auth_login_config`, `auth_break_glass_codes`,
  `personal_access_tokens`, `service_credentials`, `service_caller_suspensions`.

## Tests

- Backend: `backend/tests/auth/`, `backend/tests/api/test_auth_*.py`,
  `test_service_token.py`, `backend/tests/test_settings_*.py`.
- e2e: `frontend/e2e/settings.spec.ts`, `settings-tap-targets.spec.ts`, `real-auth.spec.ts`
  (with the `e2e-stack` auth overlay).

## Limits and known issues

- Enforcing auth on the deployment: [#5199](https://github.com/eh-homelab/ScadBuddy/issues/5199)
  and [#5297](https://github.com/eh-homelab/ScadBuddy/issues/5297); the epic is
  [#4975](https://github.com/eh-homelab/ScadBuddy/issues/4975).
- Service credentials do not yet show proof and pod
  ([#5499](https://github.com/eh-homelab/ScadBuddy/issues/5499)); the KEK only in the CRUD
  service is [#5342](https://github.com/eh-homelab/ScadBuddy/issues/5342).
