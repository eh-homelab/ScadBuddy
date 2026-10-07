#!/usr/bin/env bash
#
# Branch coverage for openscad-bump.sh. No docker and no network: the tag
# listing is a local file and `openscad --version` is a stub.
#
# Runs in ci.yml's `lint` job.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
bump="$here/openscad-bump.sh"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

old_digest="sha256:$(printf 'a%.0s' {1..64})"
new_digest="sha256:$(printf 'b%.0s' {1..64})"
failures=0

fail() {
  echo "FAIL: $1" >&2
  failures=$((failures + 1))
}

# A repo root with the two lines the script edits and a neighbour it must not.
setup() {
  rm -rf "$work/repo"
  mkdir -p "$work/repo"
  cat > "$work/repo/Dockerfile" <<EOF
FROM ghcr.io/astral-sh/uv:0.12.19 AS uv
FROM openscad/openscad:dev.2026-09-23@$old_digest AS base
ARG OPENSCAD_VERSION=2026.09.23
EOF
  cat > "$work/repo/CLAUDE.md" <<'EOF'
- Base image is a pinned dated nightly, `openscad/openscad:dev.2026-09-23@sha256:…`
  Dockerfile also asserts `OPENSCAD_VERSION` (currently 2026.09.23). Bump
EOF
}

# `tags <name digest>...` writes a Docker Hub listing.
tags() {
  local entries=() name digest
  while [ $# -gt 0 ]; do
    name="$1" digest="$2"
    shift 2
    entries+=("{\"name\":\"$name\",\"digest\":\"$digest\"}")
  done
  local IFS=,
  printf '{"results":[%s]}' "${entries[*]}" > "$work/tags.json"
}

cat > "$work/version" <<'STUB'
#!/usr/bin/env bash
echo "$1" > "${STUB_SEEN:-/dev/null}"
if [ -n "${STUB_VERSION_FAILS:-}" ]; then echo "docker: pull failed"; exit 0; fi
echo "OpenSCAD version ${STUB_VERSION:-2026.09.30}"
STUB
chmod +x "$work/version"

run() {
  OPENSCAD_TAGS_URL="file://$work/tags.json" OPENSCAD_VERSION_CMD="$work/version" \
    "$bump" "$work/repo"
}

# 1. A newer dated tag: tag, digest and version move together.
setup
tags latest "$new_digest" dev "$new_digest" dev.2026-09-30 "$new_digest" dev.2026-09-23 "$old_digest"
out="$(STUB_SEEN="$work/seen" run)" || fail "bump exited non-zero"
grep -qx "bump=true" <<<"$out" || fail "bump: expected bump=true, got: $out"
grep -qx "tag=dev.2026-09-30" <<<"$out" || fail "bump: tag"
grep -qx "version=2026.09.30" <<<"$out" || fail "bump: version"
grep -qx "FROM openscad/openscad:dev.2026-09-30@$new_digest AS base" "$work/repo/Dockerfile" || fail "bump: FROM line"
grep -qx "ARG OPENSCAD_VERSION=2026.09.30" "$work/repo/Dockerfile" || fail "bump: ARG line"
grep -qx "FROM ghcr.io/astral-sh/uv:0.12.19 AS uv" "$work/repo/Dockerfile" || fail "bump: touched another FROM"
grep -q 'dev.2026-09-30@sha256:…' "$work/repo/CLAUDE.md" || fail "bump: CLAUDE.md tag"
# shellcheck disable=SC2016 # literal backticks
grep -q '`OPENSCAD_VERSION` (currently 2026.09.30)' "$work/repo/CLAUDE.md" || fail "bump: CLAUDE.md version"
grep -qx "openscad/openscad:dev.2026-09-30@$new_digest" "$work/seen" || fail "bump: version read from the wrong image"

# 2. Already on the newest: nothing changes.
setup
tags dev.2026-09-23 "$old_digest" dev.2026-01-19 "$new_digest"
cp "$work/repo/Dockerfile" "$work/before"
out="$(run)" || fail "up to date exited non-zero"
[ "$out" = "bump=false" ] || fail "up to date: got: $out"
cmp -s "$work/before" "$work/repo/Dockerfile" || fail "up to date: Dockerfile changed"

# 3. Newest by name, not by listing order; odd names are not dated tags.
setup
tags dev.2026-10-01-rc "$new_digest" dev.2026-09-24 "$new_digest" dev.2026-09-30 "$new_digest"
out="$(run)" || fail "by name exited non-zero"
grep -qx "tag=dev.2026-09-30" <<<"$out" || fail "by name: got: $out"

# 4. Failures change nothing and exit non-zero.
setup
cp "$work/repo/Dockerfile" "$work/before"
tags latest "$new_digest"
if run >/dev/null 2>&1; then fail "no dated tag: exited zero"; fi
tags dev.2026-09-30 "not-a-digest"
if run >/dev/null 2>&1; then fail "bad digest: exited zero"; fi
tags dev.2026-09-30 "$new_digest"
if STUB_VERSION_FAILS=1 run >/dev/null 2>&1; then fail "no version: exited zero"; fi
if STUB_VERSION='1|x' run >/dev/null 2>&1; then fail "odd version: exited zero"; fi
rm -f "$work/tags.json"
if run >/dev/null 2>&1; then fail "no listing: exited zero"; fi
cmp -s "$work/before" "$work/repo/Dockerfile" || fail "a failure changed the Dockerfile"

# 5. A Dockerfile without the pinned form is refused, not rewritten.
setup
sed -i 's|^FROM openscad/openscad:.*|FROM openscad/openscad:dev AS base|' "$work/repo/Dockerfile"
tags dev.2026-09-30 "$new_digest"
if run >/dev/null 2>&1; then fail "unpinned: exited zero"; fi

if [ "$failures" -gt 0 ]; then
  echo "$failures failure(s)" >&2
  exit 1
fi
echo "openscad-bump.sh: all cases pass"
