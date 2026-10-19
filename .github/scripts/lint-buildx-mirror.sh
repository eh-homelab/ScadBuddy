#!/usr/bin/env bash
# Usage: lint-buildx-mirror.sh [repo root, default this checkout]
#
# Every docker/setup-buildx-action step pulls BuildKit from Google's Docker Hub
# mirror (#3370): Docker Hub refuses some anonymous pulls from hosted runners, which
# failed jobs before any test ran. This fails naming each step that does not.
# It reads `driver-opts: image=…` on one line, or an `image=…` line inside a
# `driver-opts: |` block (one option per line, as the action reads them; its
# indicators and a trailing comment are fine). A folded `>` block joins its
# options onto one line, and a quoted value is not read either: both are
# flagged, so write it one of the two ways above.
# The daemon it starts pulls the Dockerfiles' Docker Hub bases through the same
# mirror (#4012): each step also needs a `buildkitd-config-inline: |` block whose
# `[registry."docker.io"]` table says `mirrors = ["mirror.gcr.io"]`. BuildKit
# falls back to Docker Hub for an image the mirror does not hold, and a pinned
# digest is checked either way, so the Dockerfiles keep their plain references
# (which Dependabot and openscad-bump.yml read).
set -euo pipefail
root="${1:-$(cd "$(dirname "$0")/../.." && pwd)}"
python3 - "$root" <<'PY'
import re
import sys
from pathlib import Path

IMAGE = r"image=mirror\.gcr\.io/moby/buildkit:buildx-stable-1\s*$"
INLINE = re.compile(r"^\s*driver-opts:\s*" + IMAGE)
BLOCK = re.compile(r"^(\s*)driver-opts:\s*\|[-+0-9]*\s*(#.*)?$")
OPTION = re.compile(r"^\s*" + IMAGE)
CONFIG = re.compile(r"^(\s*)buildkitd-config-inline:\s*\|[-+0-9]*\s*(#.*)?$")
TABLE = re.compile(r"^\s*\[")
DOCKER_IO = re.compile(r'^\s*\[registry\."docker\.io"\]\s*(#.*)?$')
MIRRORS = re.compile(r'^\s*mirrors\s*=\s*\[\s*"mirror\.gcr\.io"\s*\]\s*(#.*)?$')


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


def registry_mirrored(step):
    """Whether the step's daemon config sends docker.io pulls to the mirror."""
    for at, line in enumerate(step):
        block = CONFIG.match(line)
        if not block:
            continue
        depth = len(block.group(1))
        in_docker_io = False
        for option in step[at + 1 :]:
            if option.strip() and len(option) - len(option.lstrip()) <= depth:
                break
            if TABLE.match(option):
                in_docker_io = bool(DOCKER_IO.match(option))
            elif in_docker_io and MIRRORS.match(option):
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
        where = f"{path.relative_to(root)}:{at + 1}"
        if not mirrored(lines[start:end]):
            bad.append(f"{where}: setup-buildx-action without driver-opts:"
                       " image=mirror.gcr.io/moby/buildkit:buildx-stable-1 (#3370)")
        if not registry_mirrored(lines[start:end]):
            bad.append(f"{where}: setup-buildx-action without a buildkitd-config-inline"
                       ' [registry."docker.io"] mirrors = ["mirror.gcr.io"] (#4012)')
for problem in bad:
    print(f"FAIL: {problem}")
sys.exit(1 if bad else 0)
PY
