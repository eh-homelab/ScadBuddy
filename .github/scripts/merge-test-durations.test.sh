#!/usr/bin/env bash
# Tests for merge-test-durations.sh: the shards' timings merge into one file in
# pytest-split's format, and a test timed twice or missing is refused.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
fail=0
merge() { "$here/merge-test-durations.sh" "$@" > "$tmp/log" 2>&1; }

printf '{"tests/b.py::test_b": 2.5}' > "$tmp/1.json"
printf '{"tests/a.py::test_a[x]": 1.0, "tests/c.py::test_c": 0.25}' > "$tmp/2.json"

if merge "$tmp/out.json" 3 "$tmp/1.json" "$tmp/2.json"; then
  want=$(printf '{\n    "tests/a.py::test_a[x]": 1.0,\n    "tests/b.py::test_b": 2.5,\n    "tests/c.py::test_c": 0.25\n}')
  [[ "$(cat "$tmp/out.json")" == "$want" ]] || { echo "FAIL: merged file is"; cat "$tmp/out.json"; fail=1; }
  [[ "$(tail -c 1 "$tmp/out.json")" == "}" ]] || { echo "FAIL: merged file ends with a newline"; fail=1; }
  grep -q "3 tests, 4 s" "$tmp/log" || { echo "FAIL: no merged total in"; cat "$tmp/log"; fail=1; }
else
  echo "FAIL: a clean merge was refused"; cat "$tmp/log"; fail=1
fi

printf '{"tests/b.py::test_b": 3.0}' > "$tmp/dup.json"
if merge "$tmp/out2.json" 3 "$tmp/1.json" "$tmp/2.json" "$tmp/dup.json"; then
  echo "FAIL: a test timed by two shards was merged"; fail=1
else
  grep -q "also timed by another shard, e.g. tests/b.py::test_b" "$tmp/log" || { echo "FAIL: wrong refusal"; cat "$tmp/log"; fail=1; }
fi

if merge "$tmp/out3.json" 4 "$tmp/1.json" "$tmp/2.json"; then
  echo "FAIL: a merge short of the collected count was accepted"; fail=1
else
  grep -q "timed 3 tests, not the 4 collected" "$tmp/log" || { echo "FAIL: wrong refusal"; cat "$tmp/log"; fail=1; }
fi
[ ! -e "$tmp/out3.json" ] || { echo "FAIL: a refused merge still wrote its file"; fail=1; }

exit "$fail"
