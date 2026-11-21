"""Checks the bodies the agent's CrudRelayRows sent against the API's own mcp-relay routes
(backend scadbuddy/api/crud_agent_mcp.py, #5299 step 11), for test/mcpSessions.pg.test.ts.
Stdin: a JSON list of {path, body}, the path under ``/crud/agent``. Stdout: one result per
request: ``{"ok": true}``, with ``payload`` (the NOTIFY the route would send) for a send, or
``{"ok": false, "error": ...}`` for a body the route refuses."""

import json
import sys
from typing import Any, get_type_hints

from pydantic import BaseModel, ValidationError

from scadbuddy.api import crud_agent_mcp
from scadbuddy.core.problems import ApiError

PREFIX = "/internal/crud/agent"


def models() -> dict[str, type[BaseModel]]:
    out: dict[str, type[BaseModel]] = {}
    for route in crud_agent_mcp.router.routes:
        endpoint = getattr(route, "endpoint")
        out[getattr(route, "path").removeprefix(PREFIX)] = get_type_hints(endpoint)["body"]
    return out


def main() -> None:
    by_path = models()
    results: list[dict[str, Any]] = []
    for request in json.load(sys.stdin):
        model = by_path.get(request["path"])
        if model is None:
            results.append({"ok": False, "error": f"no route {request['path']}"})
            continue
        try:
            body = model.model_validate(request["body"])
        except ValidationError as err:
            results.append({"ok": False, "error": str(err)})
            continue
        result: dict[str, Any] = {"ok": True}
        if isinstance(body, crud_agent_mcp.Send):
            try:
                result["payload"] = crud_agent_mcp.checked_payload(body)
            except ApiError as err:
                result = {"ok": False, "error": str(err)}
        results.append(result)
    json.dump(results, sys.stdout)


main()
