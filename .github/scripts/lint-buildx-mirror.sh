#!/usr/bin/env bash
# Usage: lint-buildx-mirror.sh [repo root, default this checkout]
#
# Every docker/setup-buildx-action step pulls BuildKit from Google's Docker Hub
# mirror (#3370): Docker Hub refuses some anonymous pulls from hosted runners, which
# failed jobs before any test ran. This fails naming each step that does not.
# It reads `driver-opts: image=…` on one line, or an `image=…` line inside a
# `driver-opts: |` block (one option per line, as the action reads them); a
# quoted value is flagged, so write it one of those ways.
set -euo pipefail
root="${1:-$(cd "$(dirname "$0")/../.." && pwd)}"
python3 - "$root" <<'PY'
import re
import sys
from pathlib import Path

IMAGE = r"image=mirror\.gcr\.io/moby/buildkit:buildx-stable-1\s*$"
INLINE = re.compile(r"^\s*driver-opts:\s*" + IMAGE)
BLOCK = re.compile(r"^(\s*)driver-opts:\s*\|[-+]?\s*$")
OPTION = re.compile(r"^\s*" + IMAGE)


def mirrored(step):
    """Whether the step's driver-opts names the mirror, inline or in a block."""
    for at, line in enumerate(step):
        if INLINE.match(line):
            return True
        block = BLOCK.match(line)
        if not block:
            continue
        depth = len(block.group(1))
        for option in step[at + 1 :]:
            if option.strip() and len(option) - len(option.lstrip()) <= depth:
                break
            if OPTION.match(option):
                return True
    return False
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
        if not mirrored(lines[start:end]):
            bad.append(f"{path.relative_to(root)}:{at + 1}")
for where in bad:
    print(f"FAIL: {where}: setup-buildx-action without driver-opts:"
          " image=mirror.gcr.io/moby/buildkit:buildx-stable-1 (#3370)")
sys.exit(1 if bad else 0)
PY
