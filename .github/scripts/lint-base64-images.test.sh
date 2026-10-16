#!/usr/bin/env bash
# lint-base64-images.sh passes on this checkout, and fails on new code that builds a
# base64 image (in any of the four packages) or on an allowlist entry gone stale.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
root="$(cd "$here/../.." && pwd)"
work="$(mktemp -d)"
trap 'rm -rf -- "$work"' EXIT

"$here/lint-base64-images.sh" "$root" >/dev/null

# The files that mention base64 at all (so every allowlist entry matches), once.
mapfile -t files < <(
  git -C "$root" ls-files -z -- agent/src agent-durable/src frontend/src backend/scadbuddy |
    (
      cd "$root" || exit 1
      xargs -0 grep -lE "base64|btoa|DataURL" || true
    )
)

# A copy of those files, as a git checkout of their own.
copy() {
  rm -rf "${work:?}/r"
  mkdir -p "$work/r"
  for f in "${files[@]}"; do
    mkdir -p "$work/r/$(dirname "$f")"
    cp "$root/$f" "$work/r/$f"
  done
  git -C "$work/r" init -q
  git -C "$work/r" add -A
}

add() {
  mkdir -p "$work/r/$(dirname "$1")"
  printf '%s\n' "$2" >"$work/r/$1"
  git -C "$work/r" add -- "$1"
}

expect_fail() {
  local why="$1" grep_for="$2" out
  if out="$("$here/lint-base64-images.sh" "$work/r" 2>&1)" || ! grep -qF -- "$grep_for" <<<"$out"; then
    echo "FAIL: $why passed the lint: $out"
    exit 1
  fi
}

copy
"$here/lint-base64-images.sh" "$work/r" >/dev/null

add agent/src/sessions/new.ts "const block = { type: 'image', source: { type: 'base64', media_type: m, data } }"
expect_fail "a Messages API base64 image block outside the boundary" "agent/src/sessions/new.ts:1"

copy
add agent/src/routes/new.ts "return c.json({ image: bytes.toString('base64') })"
expect_fail "an image's base64 in a route's JSON" "agent/src/routes/new.ts:1"

copy
add frontend/src/components/New.tsx "reader.readAsDataURL(file)"
expect_fail "a data: URL read in the browser" "frontend/src/components/New.tsx:1"

copy
# shellcheck disable=SC2016 # the TypeScript template literal is the point
add frontend/src/lib/new.ts 'const src = `data:${type};base64,${data}`'
expect_fail "a data: URL built in the browser" "frontend/src/lib/new.ts:1"

copy
add backend/scadbuddy/api/new.py 'return {"png": base64.b64encode(png).decode()}'
expect_fail "base64 in a backend response" "backend/scadbuddy/api/new.py:1"

copy
add agent-durable/src/scadbuddy_durable/new.py 'image = {"type": "base64", "data": data}'
expect_fail "a base64 image block in agent-durable" "agent-durable/src/scadbuddy_durable/new.py:1"

copy
add agent/src/tools/new.ts "      thumbnail_base64: z.string(),"
expect_fail "a tool input field for base64" "agent/src/tools/new.ts:1"

copy
# An allowlisted line that is gone leaves its entry matching nothing.
sed -i '/type: .base64. as const/d' "$work/r/agent/src/sessions/images.ts"
expect_fail "a stale allowlist entry" "matches nothing"

copy
# Tests and the msw mocks are fixtures, not shipped code.
add agent/test/new.test.ts "const data = PNG.toString('base64')"
add frontend/src/mocks/features/new.ts "const data = btoa(bytes)"
"$here/lint-base64-images.sh" "$work/r" >/dev/null

echo "ok: new base64 images fail in every package, stale entries fail, tests and mocks pass"
