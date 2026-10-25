#!/usr/bin/env bash
# Merges the backend-pytest shards' own timings into one backend/.test_durations
# (pytest-split's format: {"<node id>": seconds}, keys sorted, indent 4, no
# trailing newline, as `pytest --store-durations` writes it).
#
#   merge-test-durations.sh <out> <expected tests> <shard file>...
#
# Each shard file is what one shard stored with `--store-durations
# --clean-durations`: only the tests that shard ran, timed on CI's own runner.
# A test in two files, or a total that is not <expected tests> (what every shard
# collected), is an error: the shards between them must time the whole suite
# exactly once, or the merged file would balance the next split on a partial one.
# Prints each shard's test count and summed seconds, then the merged total.
set -euo pipefail
[ "$#" -ge 3 ] || { echo "usage: $0 <out> <expected tests> <shard file>..." >&2; exit 2; }
out=$1
expected=$2
shift 2
python3 - "$out" "$expected" "$@" <<'PY'
import json
import sys

out, expected, paths = sys.argv[1], int(sys.argv[2]), sys.argv[3:]
merged: dict[str, float] = {}
for path in paths:
    with open(path) as f:
        shard = json.load(f)
    if not isinstance(shard, dict):
        sys.exit(f"{path}: not a JSON object of test durations")
    twice = sorted(set(shard) & set(merged))
    if twice:
        sys.exit(f"{path}: {len(twice)} test(s) also timed by another shard, e.g. {twice[0]}")
    print(f"{path}: {len(shard)} tests, {sum(shard.values()):.0f} s")
    merged.update(shard)
if len(merged) != expected:
    sys.exit(f"the shards timed {len(merged)} tests, not the {expected} collected")
with open(out, "w") as f:
    json.dump(merged, f, sort_keys=True, indent=4)
print(f"{out}: {len(merged)} tests, {sum(merged.values()):.0f} s")
PY
