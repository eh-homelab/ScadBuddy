"""``python -m scadbuddy.intake_worker.child <file> <name> <preview> <result> <bytes>``: one
check (`check.py`) in a process of its own, run by `runner.py`. It caps its own address
space at ``<bytes>`` before it reads a byte of the file, so a file that would take more
memory fails this process, not the worker. It then makes itself non-dumpable and moves
into new user and network namespaces (`hardening.py`; a refusal is reported in the verdict's
``isolation`` and the check goes on). It writes the verdict as JSON to ``<result>``
and, for a ready file, the preview PNG to ``<preview>``; a crash writes nothing, which the
runner answers as a failed check."""

from __future__ import annotations

import json
import resource
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from scadbuddy.intake_worker.hardening import forbid_dumping, isolate


def main(argv: Sequence[str]) -> int:
    file, name, preview, result, limit = argv
    cap = int(limit)
    resource.setrlimit(resource.RLIMIT_AS, (cap, cap))
    forbid_dumping()
    # Before any import that may start a thread: unshare(CLONE_NEWUSER) needs one.
    refused = isolate()
    # Imported under the cap: numpy, trimesh and the readers are part of what it bounds.
    from scadbuddy.intake_worker.check import Refused, check

    try:
        checked = check(Path(file), name)
    except Refused as refusal:
        verdict: dict[str, Any] = {"outcome": "refused", "reason": refusal.reason}
    else:
        Path(preview).write_bytes(checked.preview)
        verdict = {"outcome": "ready", "facts": checked.facts.model_dump(mode="json")}
    verdict["isolation"] = refused
    Path(result).write_text(json.dumps(verdict))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
