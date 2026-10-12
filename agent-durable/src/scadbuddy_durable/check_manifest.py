"""Build-time assertion, run by the Dockerfile's `agent-durable` stage.

    python -m scadbuddy_durable.check_manifest <path>

Fails when the agent's tool manifest (agent `dist/tools.json`, copied from the same
build) is missing, does not parse, or names no tool, so the image never ships a
worker that would refuse to start (#1056, phase 5e).
"""

from __future__ import annotations

import sys

from scadbuddy_durable.session.tools import load_manifest


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: python -m scadbuddy_durable.check_manifest <path>", file=sys.stderr)
        return 2
    path = argv[0]
    try:
        entries = load_manifest(path)
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as err:
        print(f"ERROR: tool manifest {path}: {type(err).__name__}: {err}", file=sys.stderr)
        return 1
    if not entries:
        print(f"ERROR: tool manifest {path} names no tool", file=sys.stderr)
        return 1
    print(f"tool manifest {path}: {len(entries)} tools")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
