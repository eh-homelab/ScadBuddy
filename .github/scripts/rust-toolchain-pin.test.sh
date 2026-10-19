#!/usr/bin/env bash
# rust-toolchain-pin.sh --check against a stub gh: a ci.yml pinned to master passes, one
# pinned elsewhere fails naming its line, and a stale second pin (in another workflow)
# fails though the first is at master (#4264). No network.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
work="$(mktemp -d)"
trap 'rm -rf -- "$work"' EXIT

master=1111111111111111111111111111111111111111
mkdir "$work/bin"
cat >"$work/bin/gh" <<STUB
#!/bin/sh
[ "\$*" = "api repos/dtolnay/rust-toolchain/commits/master --jq .sha" ] || { echo "unexpected gh \$*" >&2; exit 2; }
echo $master
STUB
chmod +x "$work/bin/gh"

pinned_to() {
  printf 'jobs:\n  rust:\n    steps:\n      - uses: dtolnay/rust-toolchain@%s # master as of 2026-10-17\n' "$1" >"$work/ci.yml"
}

pinned_to "$master"
PATH="$work/bin:$PATH" "$here/rust-toolchain-pin.sh" --check "$work/ci.yml" >/dev/null

pinned_to 2222222222222222222222222222222222222222
if out="$(PATH="$work/bin:$PATH" "$here/rust-toolchain-pin.sh" --check "$work/ci.yml" 2>&1)"; then
  echo "FAIL: a stale pin passed the check"; exit 1
fi
if ! grep -qF "$work/ci.yml:4:" <<<"$out" || ! grep -qF "$master" <<<"$out"; then
  echo "FAIL: the check failed without naming the line and master:"; echo "$out"; exit 1
fi
# Without --check it only reports.
PATH="$work/bin:$PATH" "$here/rust-toolchain-pin.sh" "$work/ci.yml" >/dev/null

pinned_to "$master"
printf 'jobs:\n  a:\n    steps:\n      - uses: dtolnay/rust-toolchain@%s\n  b:\n    steps:\n      - uses: dtolnay/rust-toolchain@%s\n' \
  "$master" 3333333333333333333333333333333333333333 >"$work/other.yml"
if out="$(PATH="$work/bin:$PATH" "$here/rust-toolchain-pin.sh" --check "$work/ci.yml" "$work/other.yml" 2>&1)"; then
  echo "FAIL: a stale second pin passed the check"; exit 1
fi
if ! grep -qF "$work/other.yml:7:" <<<"$out" || grep -qF "$work/other.yml:4: dtolnay" <<<"$out" \
  || grep -qF "$work/ci.yml:4: dtolnay" <<<"$out"; then
  echo "FAIL: the check did not name only the stale second pin:"; echo "$out"; exit 1
fi
echo "ok: a pin at master passes, a stale pin fails naming its line, every pin is checked"
