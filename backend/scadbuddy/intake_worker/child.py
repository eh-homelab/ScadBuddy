"""``python -m scadbuddy.intake_worker.child <name> <bytes>``: one check (`check.py`) in a
process of its own, started by its launcher (`confine.py`: new user, network and mount
namespaces with the spool hidden, no capabilities, no new privileges, a seccomp filter) in
its job's box (its working directory). It caps its own address space at ``<bytes>`` before
it reads a byte of the file, so a file that would take more memory fails this process, not
the worker, and makes itself non-dumpable. It reads ``upload.bin`` and writes the verdict
as JSON to ``verdict.json`` and, for a ready file, the preview PNG to ``preview.png``, all
in its box; a crash writes nothing, which the runner answers as a failed check. A step the
launcher could not take (only without ``--strict``, in development) is reported in the
verdict's ``isolation``."""

from __future__ import annotations

import json
import os
import resource
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from scadbuddy.intake_worker.hardening import forbid_dumping

UPLOAD = "upload.bin"
PREVIEW = "preview.png"
VERDICT = "verdict.json"
#: `confine.ISOLATION_ENV`, named here so the child imports nothing of the launcher.
ISOLATION_ENV = "SCADBUDDY_INTAKE_ISOLATION"


def main(argv: Sequence[str]) -> int:
    name, limit = argv
    cap = int(limit)
    resource.setrlimit(resource.RLIMIT_AS, (cap, cap))
    forbid_dumping()
    # Imported under the cap: numpy, trimesh and the readers are part of what it bounds.
    from scadbuddy.intake_worker.check import Refused, check

    try:
        checked = check(Path(UPLOAD), name)
    except Refused as refusal:
        verdict: dict[str, Any] = {"outcome": "refused", "reason": refusal.reason}
    else:
        Path(PREVIEW).write_bytes(checked.preview)
        verdict = {"outcome": "ready", "facts": checked.facts.model_dump(mode="json")}
    verdict["isolation"] = os.environ.get(ISOLATION_ENV)
    Path(VERDICT).write_text(json.dumps(verdict))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
