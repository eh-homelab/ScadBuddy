"""`python -m scadbuddy.workflows.template_runner <model_dir> <result.json>`: one template
function (or `migrate`) in its own process (spec 2026-09-27 §5.2, §8.2, §9). Reads the
request as JSON on stdin; writes {"ok": value} or {"error": …, "line": …} to result.json."""

from __future__ import annotations

import asyncio
import importlib.util
import inspect
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

from pydantic import BaseModel

from scadbuddy.template import Blob, Part


def _load(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path.name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _decode(value: Any) -> Any:
    if isinstance(value, list):
        return [_decode(v) for v in value]
    if isinstance(value, dict):
        if value.get("kind") == "blob":
            return Blob.model_validate(value)
        if value.get("kind") == "part":
            return Part.model_validate(value)
        return {k: _decode(v) for k, v in value.items()}
    return value


def _encode(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, list | tuple):
        return [_encode(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _encode(v) for k, v in value.items()}
    if isinstance(value, bytes):
        raise TypeError("return scadbuddy.template.emit(name, data) for bytes")
    return value


def _line(error: BaseException, path: Path) -> int | None:
    line = None
    tb = error.__traceback__
    while tb is not None:
        if Path(tb.tb_frame.f_code.co_filename) == path:
            line = tb.tb_lineno
        tb = tb.tb_next
    return line


def _migrate(module: ModuleType, inputs: dict[str, Any]) -> dict[str, Any]:
    current = int(getattr(module, "INPUTS_VERSION", 0))
    version = int(inputs.get("v", 0))
    if version > current:
        raise ValueError(f"these inputs are v{version}; the template's INPUTS_VERSION is {current}")
    if version == current:
        return inputs
    migrate = getattr(module, "migrate", None)
    if not callable(migrate):
        raise LookupError(
            f"inputs are v{version} and pipeline.py defines no migrate(inputs, from_version)"
        )
    migrated = dict(migrate(dict(inputs), version))
    migrated["v"] = current
    return migrated


def main(argv: list[str]) -> int:
    model_dir, result_path = Path(argv[1]), Path(argv[2])
    request = json.loads(sys.stdin.read())
    migrate = request["mode"] == "migrate"
    path = model_dir / "pipeline" / ("pipeline.py" if migrate else "activities.py")
    sys.path.insert(0, str(path.parent))
    try:
        if migrate:
            value: Any = _migrate(_load(path, "scadbuddy_pipeline"), request["inputs"])
        else:
            name = request["name"]
            fn = getattr(_load(path, "scadbuddy_template_activities"), name, None)
            if name.startswith("_") or not callable(fn):
                raise LookupError(f"pipeline/activities.py has no function {name!r}")
            value = fn(*_decode(request["args"]), **_decode(request["kwargs"]))
            if inspect.isawaitable(value):
                value = asyncio.run(_await(value))
        reply: dict[str, Any] = {"ok": _encode(value)}
    except Exception as error:
        reply = {
            "error": f"{type(error).__name__}: {error}",
            "line": _line(error, path),
            "file": f"pipeline/{path.name}",
        }
    result_path.write_text(json.dumps(reply), encoding="utf-8")
    return 0


async def _await(value: Any) -> Any:
    return await value


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
