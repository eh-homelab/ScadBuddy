#!/usr/bin/env bash
#
# The login against a real stack in three browsers (#4975 phase 7, spec 2026-11-01 §11):
# `run.sh --auth [--keep] [--no-build]` runs this. ScadBuddy in `observe` with its login
# on, behind an https edge, signing in through the fake issuer (compose.auth.yaml), and
# frontend/e2e/real-auth.spec.ts in the Playwright image's Chromium, Firefox and WebKit;
# then the same app restarted in `enforce` and the spec's enforce tests (#4975 phase 8).
#
# It builds `runtime` as SB_HOTPATH_APP_IMAGE and `test` as SB_HOTPATH_TOOLS_IMAGE (unless
# --no-build), as run.sh does, and needs docker with compose v2, curl, python3 and the
# frontend's dependencies installed (`pnpm install --frozen-lockfile` in frontend/; no
# browsers: the Playwright image brings its own). Teardown is project `sb-auth`'s
# `down -v`; to tear down a stack left by --keep:
#
#   SB_HOTPATH_KEK_FILE=/dev/null SB_AUTH_CLIENT_SECRET=x SB_AUTH_STATE_DIR=/tmp \
#     docker compose -p sb-auth -f e2e-stack/compose.yaml -f e2e-stack/compose.auth.yaml down -v
#
# The agent's service credential, which the spec presents to mint delegated sign-in links,
# is read from the app's own view into a 0600 file in a throwaway directory, never printed.
set -euo pipefail

keep=0
build=1
for arg in "$@"; do
  case "$arg" in
    --keep) keep=1 ;;
    --no-build) build=0 ;;
    *)
      echo "usage: run.sh --auth [--keep] [--no-build]" >&2
      exit 2
      ;;
  esac
done

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo="$(cd "$here/.." && pwd)"
export SB_HOTPATH_APP_IMAGE="${SB_HOTPATH_APP_IMAGE:-scadbuddy:hot-path-app}"
export SB_HOTPATH_TOOLS_IMAGE="${SB_HOTPATH_TOOLS_IMAGE:-scadbuddy:hot-path-test}"
compose=(docker compose -p sb-auth -f "$here/compose.yaml" -f "$here/compose.auth.yaml")

state="$(mktemp -d)"
chmod 0755 "$state"
export SB_AUTH_STATE_DIR="$state"
export SB_HOTPATH_KEK_FILE="$state/kek"
python3 -c 'import base64, os; print(base64.b64encode(os.urandom(32)).decode())' >"$SB_HOTPATH_KEK_FILE"
chmod 0644 "$SB_HOTPATH_KEK_FILE"
SB_AUTH_CLIENT_SECRET="$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')"
export SB_AUTH_CLIENT_SECRET

up=0
finish() {
  status=$?
  if [ "$status" -ne 0 ] && [ "$up" = 1 ]; then
    mkdir -p "$repo/frontend/test-results/auth"
    "${compose[@]}" ps -a || true
    "${compose[@]}" logs app oidc edge >"$repo/frontend/test-results/auth/stack.log" 2>&1 || true
    echo "--- the stack's last logs (frontend/test-results/auth/stack.log)"
    tail -n 200 "$repo/frontend/test-results/auth/stack.log" || true
  fi
  if [ "$keep" = 0 ]; then
    [ "$up" = 1 ] && "${compose[@]}" down -v --remove-orphans >/dev/null 2>&1
    rm -rf "$state"
  else
    echo "kept: project sb-auth; its files are in $state"
  fi
  exit "$status"
}
trap finish EXIT

if [ "$build" = 1 ]; then
  docker build --target runtime -t "$SB_HOTPATH_APP_IMAGE" "$repo"
  docker build --target test -t "$SB_HOTPATH_TOOLS_IMAGE" "$repo"
fi
for image in $("${compose[@]}" --profile playwright config --images); do
  case "$image" in
    mirror.gcr.io/* | mcr.microsoft.com/*) docker pull --quiet "$image" >/dev/null ;;
  esac
done

up=1
"${compose[@]}" up -d --wait --wait-timeout 300 postgres temporal s3 oidc edge app

# The login on, for the fake issuer's one user, in `observe`, which lets anyone write the
# config: the README's order (configure the login before `enforce`, which then needs a
# person's session for it).
api="http://127.0.0.1:${SB_AUTH_APP_PORT:-18090}/api/v1"
body="$(python3 -c 'import json, os; print(json.dumps({"enabled": True, "client_secret": os.environ["SB_AUTH_CLIENT_SECRET"], "allowed_subjects": ["alice"], "rp_logout": True}))')"
curl --fail --silent --show-error --output /dev/null -X PUT -H 'Content-Type: application/json' \
  --data "$body" "$api/auth/config"

# The agent's credential, which the API issued at its start.
umask 077
"${compose[@]}" exec -T app /opt/venv/bin/python -c '
import os, sys, time
from pathlib import Path
from scadbuddy.auth.service_credentials import TokenReader
from scadbuddy.core.secrets import load_kek
kek = load_kek(Path(os.environ["SCADBUDDY_SECRET_KEY_FILE"]))
for _ in range(60):
    # A new reader each time: one that found none waits a minute before it looks again.
    token = TokenReader("agent", os.environ["SCADBUDDY_DATABASE_URL"], kek).token()
    if token:
        sys.stdout.write(token)
        break
    time.sleep(1)
else:
    sys.exit("no agent credential was issued")
' >"$state/agent-token"
umask 022
# The Playwright container runs as this user and reads it through the mount.
chmod 0600 "$state/agent-token"

engines=(--project=auth-chromium --project=auth-firefox --project=auth-webkit --workers=1)
export SB_AUTH_MODE=observe
"${compose[@]}" run --rm --user "$(id -u):$(id -g)" playwright \
  ./node_modules/.bin/playwright test e2e/real-auth.spec.ts "${engines[@]}" --output test-results/auth

# #4975 phase 8: the same app in `enforce`, the login saved above. Its start checks the
# login (spec 2026-11-01 §9.1), so `--wait` failing here is that check refusing.
export SB_AUTH_MODE=enforce
"${compose[@]}" up -d --wait --wait-timeout 300 app
"${compose[@]}" run --rm --user "$(id -u):$(id -g)" playwright \
  ./node_modules/.bin/playwright test e2e/real-auth.spec.ts "${engines[@]}" --output test-results/auth-enforce
