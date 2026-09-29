"""Write ``backend/openapi.json``; the frontend and agent generate their clients from it.

The file is not committed (#492). ``pnpm gen:api`` in either package runs this first,
and the Dockerfile's ``api-spec`` stage runs it for the image builds.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from scadbuddy.core.settings import Settings
from scadbuddy.main import create_app

# scadbuddy/tools/export_openapi.py -> scadbuddy -> backend
DEFAULT_OUTPUT = Path(__file__).resolve().parents[2] / "openapi.json"
#: Satisfies the required ``SCADBUDDY_DATABASE_URL`` for an app that never starts.
UNUSED_DATABASE_URL = "postgresql://unused.invalid/scadbuddy"
#: And the required ``SCADBUDDY_TEMPORAL_ADDRESS``: nothing listens there.
UNUSED_TEMPORAL_ADDRESS = "127.0.0.1:1"


def export(out_path: Path) -> Path:
    # No frontend bundle and no data dir: the schema must not depend on the environment.
    # Building the app connects to nothing, so the database URL is never dialled.
    app = create_app(
        Settings(
            frontend_dir=Path("/nonexistent"),
            database_url=UNUSED_DATABASE_URL,
            temporal_address=UNUSED_TEMPORAL_ADDRESS,
        )
    )
    out_path.write_text(json.dumps(app.openapi(), indent=2, sort_keys=True) + "\n", "utf-8")
    return out_path


def main(argv: list[str]) -> int:
    target = Path(argv[1]) if len(argv) > 1 else DEFAULT_OUTPUT
    print(export(target))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
