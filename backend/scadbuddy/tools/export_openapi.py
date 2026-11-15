"""Write ``backend/openapi.json``; the frontend and agent generate their clients from it.

The file is not committed (#492). ``pnpm gen:api`` in either package runs this first,
and the Dockerfile's ``api-spec`` stage runs it for the image builds.

``--storefront <path>`` writes the storefront edge's spec instead (#5282): its routes
under ``/api/store/v1``, from which the frontend types the public page
(``src/storefront/api/schema.d.ts``). The edge relays each CRUD answer as it comes and
declares no shapes itself (it never imports the models, `test_edge.py`), so each edge
operation takes its request body and its answer's schema from the CRUD route at the same
place under ``/internal/crud/storefront`` (`api/crud_storefront.py`). Where the edge
reshapes an answer, the schema says what the edge sends: the estimate keeps only
`storefront/routes.py` `ESTIMATE_FIELDS`.
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi
from fastapi.routing import APIRoute

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


#: Where the CRUD side's storefront routes sit, as its schema names them.
CRUD_STOREFRONT = "/internal/crud/storefront"


def _refs(node: Any) -> set[str]:
    """Every schema name ``node`` refers to."""
    if isinstance(node, dict):
        found = set()
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/components/schemas/"):
            found.add(ref.rsplit("/", 1)[1])
        for value in node.values():
            found |= _refs(value)
        return found
    if isinstance(node, list):
        return set().union(*(_refs(item) for item in node)) if node else set()
    return set()


def storefront_spec() -> dict[str, Any]:
    from scadbuddy.api.crud_storefront import router as crud_router
    from scadbuddy.storefront import routes as edge_routes
    from scadbuddy.storefront.routes import visitor_router
    from scadbuddy.storefront.settings import STOREFRONT_PREFIX

    edge = FastAPI(title="ScadBuddy storefront", version="1")
    edge.include_router(visitor_router)
    spec = edge.openapi()
    # The CRUD routes are hidden from the API's schema; shown here, on copies.
    shown = []
    for route in crud_router.routes:
        assert isinstance(route, APIRoute)
        visible = copy.copy(route)
        visible.include_in_schema = True
        shown.append(visible)
    crud = get_openapi(title="crud", version="1", routes=shown)
    crud_ops = {
        (path.split(CRUD_STOREFRONT, 1)[1], method): op
        for path, ops in crud["paths"].items()
        for method, op in ops.items()
    }
    used: set[str] = set()
    for path, ops in spec["paths"].items():
        for method, op in ops.items():
            source = crud_ops.get((path.removeprefix(STOREFRONT_PREFIX), method))
            if source is None:
                raise LookupError(f"no CRUD route for the edge's {method.upper()} {path}")
            for code, response in op["responses"].items():
                if code.startswith("2") and "application/json" in response.get("content", {}):
                    answer = next(
                        (r for c, r in source["responses"].items() if c.startswith("2")), None
                    )
                    if answer is not None:
                        response["content"] = answer["content"]
            if "requestBody" in source:
                op["requestBody"] = source["requestBody"]
            used |= _refs(op)
    schemas = crud["components"]["schemas"]
    pending = set(used)
    while pending:
        name = pending.pop()
        pending |= _refs(schemas[name]) - used
        used |= _refs(schemas[name])
    shown_schemas = {name: copy.deepcopy(schemas[name]) for name in sorted(used) if name in schemas}
    # The edge sends on only these fields of an estimate (no cost, markup or rate).
    estimate = shown_schemas["StoreEstimate"]
    kept = edge_routes.ESTIMATE_FIELDS
    estimate["properties"] = {k: v for k, v in estimate["properties"].items() if k in kept}
    if "required" in estimate:
        estimate["required"] = [k for k in estimate["required"] if k in kept]
    spec.setdefault("components", {}).setdefault("schemas", {}).update(shown_schemas)
    return spec


def export_storefront(out_path: Path) -> Path:
    out_path.write_text(json.dumps(storefront_spec(), indent=2, sort_keys=True) + "\n", "utf-8")
    return out_path


def main(argv: list[str]) -> int:
    if len(argv) > 1 and argv[1] == "--storefront":
        if len(argv) != 3:
            print("usage: export_openapi --storefront <path>", file=sys.stderr)
            return 2
        print(export_storefront(Path(argv[2])))
        return 0
    target = Path(argv[1]) if len(argv) > 1 else DEFAULT_OUTPUT
    print(export(target))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
