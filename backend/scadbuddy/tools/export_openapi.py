"""Write ``backend/openapi.json``; the frontend generates its client from it."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from scadbuddy.core.settings import Settings
from scadbuddy.main import create_app

# scadbuddy/tools/export_openapi.py -> scadbuddy -> backend
DEFAULT_OUTPUT = Path(__file__).resolve().parents[2] / "openapi.json"


def export(out_path: Path) -> Path:
    # No frontend bundle and no data dir: the schema must not depend on the environment.
    app = create_app(Settings(frontend_dir=Path("/nonexistent")))
    out_path.write_text(json.dumps(app.openapi(), indent=2, sort_keys=True) + "\n", "utf-8")
    return out_path


def main(argv: list[str]) -> int:
    target = Path(argv[1]) if len(argv) > 1 else DEFAULT_OUTPUT
    print(export(target))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
