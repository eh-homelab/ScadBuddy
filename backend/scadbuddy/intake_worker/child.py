"""``python -m scadbuddy.intake_worker.child <name> <bytes> <hide>``: one check (`check.py`)
in a process of its own, run by `runner.py` in its job's box (its working directory). It
caps its own address space at ``<bytes>`` before it reads a byte of the file, so a file
that would take more memory fails this process, not the worker. It then makes itself
non-dumpable and moves into new user, network and mount namespaces, an empty tmpfs over
``<hide>`` (the spool), so it reaches only its box (`hardening.py`; a refusal is reported in
the verdict's ``isolation`` and the check goes on). It reads ``upload.bin`` and writes the
verdict as JSON to ``verdict.json`` and, for a ready file, the preview PNG to
``preview.png``, all in its box; a crash writes nothing, which the runner answers as a
failed check."""

from __future__ import annotations

import json
import resource
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from scadbuddy.intake_worker.hardening import forbid_dumping, isolate

UPLOAD = "upload.bin"
PREVIEW = "preview.png"
VERDICT = "verdict.json"


def main(argv: Sequence[str]) -> int:
    name, limit, hide = argv
    cap = int(limit)
    resource.setrlimit(resource.RLIMIT_AS, (cap, cap))
    forbid_dumping()
    # Before any import that may start a thread: unshare(CLONE_NEWUSER) needs one.
    refused = isolate(hide or None)
    # Imported under the cap: numpy, trimesh and the readers are part of what it bounds.
    from scadbuddy.intake_worker.check import Refused, check

    try:
        checked = check(Path(UPLOAD), name)
    except Refused as refusal:
        verdict: dict[str, Any] = {"outcome": "refused", "reason": refusal.reason}
    else:
        Path(PREVIEW).write_bytes(checked.preview)
        verdict = {"outcome": "ready", "facts": checked.facts.model_dump(mode="json")}
    verdict["isolation"] = refused
    Path(VERDICT).write_text(json.dumps(verdict))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
