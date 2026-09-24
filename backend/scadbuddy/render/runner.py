from __future__ import annotations

import asyncio
import json
import tempfile
import time
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from scadbuddy.core.config import Config
from scadbuddy.core.fontconfig import env_for
from scadbuddy.render.schema import (
    CustomizerSchema,
    Parameter,
    ParamValue,
    build_schema,
    load_cached_schema,
    source_sha256,
    store_cached_schema,
)

LOG_TAIL_LINES = 50

_ESCAPES = {"\\": "\\\\", '"': '\\"', "\n": "\\n", "\r": "\\r", "\t": "\\t"}


class OpenSCADError(RuntimeError):
    def __init__(self, message: str, log_tail: Sequence[str], returncode: int | None = None):
        super().__init__(message)
        self.log_tail = list(log_tail)
        self.returncode = returncode


class RenderTimeoutError(OpenSCADError):
    pass


class UnknownParameterError(ValueError):
    pass


@dataclass(frozen=True)
class ProcessOutput:
    returncode: int
    log_tail: list[str]
    duration_s: float


def quote_string(value: str) -> str:
    return '"' + "".join(_ESCAPES.get(ch, ch) for ch in value) + '"'


def _format_number(value: ParamValue) -> str:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"expected a number, got {value!r}")
    return repr(float(value))


def format_scad_value(parameter: Parameter, value: ParamValue) -> str:
    if parameter.type == "boolean":
        if not isinstance(value, bool):
            raise ValueError(f"parameter {parameter.name!r} expects a boolean, got {value!r}")
        return "true" if value else "false"
    if parameter.type in ("string", "color", "font"):
        if not isinstance(value, str):
            raise ValueError(f"parameter {parameter.name!r} expects a string, got {value!r}")
        return quote_string(value)
    if parameter.type == "integer":
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ValueError(f"parameter {parameter.name!r} expects a number, got {value!r}")
        return str(int(value))
    if parameter.type == "select":
        if any(isinstance(option.value, str) for option in parameter.options):
            if not isinstance(value, str):
                raise ValueError(f"parameter {parameter.name!r} expects a string, got {value!r}")
            return quote_string(value)
        return _format_number(value)
    return _format_number(value)


def build_defines(schema: CustomizerSchema, params: Mapping[str, ParamValue]) -> list[str]:
    by_name = {p.name: p for p in schema.parameters}
    unknown = sorted(set(params) - set(by_name))
    if unknown:
        raise UnknownParameterError(f"unknown parameters: {', '.join(unknown)}")
    defines: list[str] = []
    for parameter in schema.parameters:
        if parameter.name not in params:
            continue
        formatted = format_scad_value(parameter, params[parameter.name])
        defines += ["-D", f"{parameter.name}={formatted}"]
    return defines


async def _drain(stream: asyncio.StreamReader, tail: deque[str]) -> None:
    async for line in stream:
        tail.append(line.decode("utf-8", "replace").rstrip("\n"))


async def run_openscad(args: Sequence[str], *, cwd: Path, config: Config) -> ProcessOutput:
    started = time.monotonic()
    # FONTCONFIG_FILE, so `text(font = ...)` resolves the families downloaded onto
    # the data volume and not only the ones baked into the image (issue #82).
    process = await asyncio.create_subprocess_exec(
        config.openscad,
        *args,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=env_for(config.data_dir),
    )
    tail: deque[str] = deque(maxlen=LOG_TAIL_LINES)
    assert process.stdout is not None
    drain = asyncio.create_task(_drain(process.stdout, tail))
    try:
        returncode = await asyncio.wait_for(process.wait(), timeout=config.render_timeout)
    except TimeoutError:
        process.kill()
        await process.wait()
        drain.cancel()
        raise RenderTimeoutError(
            f"openscad timed out after {config.render_timeout:g}s", tail
        ) from None
    except asyncio.CancelledError:
        # A cancelled caller (a superseded parse check, a shutting-down worker) must not
        # leave the subprocess running: it would hold the CPU the next one needs.
        process.kill()
        await process.wait()
        drain.cancel()
        raise
    await drain
    duration = time.monotonic() - started
    if returncode != 0:
        raise OpenSCADError(f"openscad exited with {returncode}", tail, returncode)
    return ProcessOutput(returncode=returncode, log_tail=list(tail), duration_s=duration)


async def export_param_json(scad_path: Path, *, config: Config) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="scadbuddy-param-") as tmp:
        target = Path(tmp) / "model.param"
        await run_openscad(["-o", str(target), scad_path.name], cwd=scad_path.parent, config=config)
        try:
            data: dict[str, Any] = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            # Exit 0 and an export nothing can read: still the model's problem, so it
            # travels as OpenSCADError and reaches the same handlers a failed run does.
            raise OpenSCADError(
                f"openscad wrote no usable parameter export: {error}", []
            ) from error
    return data


async def export_schema(scad_path: Path, *, config: Config) -> CustomizerSchema:
    """Every caller of this gets a schema or an OpenSCADError, never a raw KeyError.

    `build_schema` subscripts the export's dicts directly, so an entry without a `name`
    — or an option without a `value` — raises `KeyError`/`TypeError`. Uncaught, that is
    a 500 from a route and an unhandled-error log line for a condition a client can
    reach on purpose (a `force`d save of source whose export is unusable).
    """
    source = scad_path.read_text(encoding="utf-8")
    data = await export_param_json(scad_path, config=config)
    try:
        return build_schema(data, source)
    except (ValueError, KeyError, TypeError) as error:
        raise OpenSCADError(f"the customizer schema could not be derived: {error}", []) from error


async def cached_schema(scad_path: Path, meta_path: Path, *, config: Config) -> CustomizerSchema:
    source = scad_path.read_text(encoding="utf-8")
    cached = load_cached_schema(meta_path, source_sha256(source))
    if cached is not None:
        return cached
    schema = await export_schema(scad_path, config=config)
    store_cached_schema(meta_path, schema)
    return schema


async def render_3mf(
    scad_path: Path,
    schema: CustomizerSchema,
    params: Mapping[str, ParamValue],
    out_path: Path,
    *,
    config: Config,
    extra_defines: Sequence[str] = (),
) -> ProcessOutput:
    args = [
        "--backend=Manifold",
        "--summary",
        "all",
        *build_defines(schema, params),
        *extra_defines,
        "-o",
        str(out_path.resolve()),
        scad_path.name,
    ]
    return await run_openscad(args, cwd=scad_path.parent, config=config)
