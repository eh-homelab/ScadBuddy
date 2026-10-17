#!/usr/bin/env bash
# Usage: lint-buildx-mirror.sh [repo root, default this checkout]
#
# Every docker/setup-buildx-action step pulls BuildKit from Google's Docker Hub
# mirror (#3370): Docker Hub refuses some anonymous pulls from hosted runners, which
# failed jobs before any test ran. This fails naming each step that does not.
set -euo pipefail
root="${1:-$(cd "$(dirname "$0")/../.." && pwd)}"
python3 - "$root" <<'PY'
import re
import sys
from pathlib import Path

WANT = "image=mirror.gcr.io/moby/buildkit:"
root = Path(sys.argv[1])
bad = []
for path in sorted((root / ".github" / "workflows").glob("*.y*ml")):
    lines = path.read_text(encoding="utf-8").splitlines()
    for at, line in enumerate(lines):
        if not re.search(r"\buses:\s*docker/setup-buildx-action@", line):
            continue
        # The step: back to its `- ` line, on to the next line no deeper than it.
        start = at
        while not lines[start].lstrip().startswith("- "):
            start -= 1
        depth = len(lines[start]) - len(lines[start].lstrip())
        end = start + 1
        while end < len(lines) and (
            not lines[end].strip() or len(lines[end]) - len(lines[end].lstrip()) > depth
        ):
            end += 1
        if not any(WANT in step for step in lines[start:end]):
            bad.append(f"{path.relative_to(root)}:{at + 1}")
for where in bad:
    print(f"FAIL: {where}: setup-buildx-action without driver-opts {WANT}buildx-stable-1 (#3370)")
sys.exit(1 if bad else 0)
PY
