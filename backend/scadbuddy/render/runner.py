from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import tempfile
import time
from collections import deque
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from scadbuddy.core.config import Config
from scadbuddy.core.fontconfig import env_for
from scadbuddy.core.tracing import span
from scadbuddy.render.confinement import escaping_includes, sandboxed
from scadbuddy.render.diagnostics import Diagnostic, DiagnosticCollector, parse_diagnostics
from scadbuddy.render.schema import (
    CustomizerSchema,
    Parameter,
    ParamValue,
    build_schema,
    is_bare_filename,
    load_cached_schema,
    source_sha256,
    store_cached_schema,
)

LOG_TAIL_LINES = 50
#: A template echoing a note inside a loop must not grow a job without bound.
MAX_NOTES = 20

_ESCAPES = {"\\": "\\\\", '"': '\\"', "\n": "\\n", "\r": "\\r", "\t": "\\t"}

#: OpenSCAD reports a file it could not read and carries on: `import()` logs an
#: ERROR, `surface()` a WARNING, and the run still exits 0 whenever anything else
#: rendered. Measured on 2026.09.23.
_MISSING_FILE = re.compile(
    r"^(?:ERROR: Can't open file '(?P<imported>[^']*)'"
    r"|WARNING: The file '(?P<surface>[^']*)' couldn't be opened)"
)

#: A template's plate count, as `echo(plates = N)` logs it (spec §6.4, #289).
_PLATES = re.compile(r"^ECHO: plates = (?P<count>\d+)$")


#: A message a template echoes for the person customizing it (#285): `NOTE:` by
#: convention, `WARNING:` in the templates that predate it. A single string, so
#: `echo("NOTE:", x)` -- and every debug echo -- stays in the log only. OpenSCAD
#: prints the string raw, embedded quotes and backslashes unescaped (measured on
#: 2026.09.23), so the text is everything between the first and the last quote.
_TEMPLATE_NOTE = re.compile(r'^ECHO: "(?:NOTE|WARNING): (?P<text>.*)"$')


class OpenSCADError(RuntimeError):
    def __init__(
        self,
        message: str,
        log_tail: Sequence[str],
        returncode: int | None = None,
        diagnostics: Sequence[Diagnostic] = (),
        diagnostics_dropped: int = 0,
        missing_files: Sequence[str] = (),
        warnings: Sequence[str] = (),
    ):
        super().__init__(message)
        self.log_tail = list(log_tail)
        #: Base names of the files the run could not open (see `ProcessOutput`).
        self.missing_files = tuple(missing_files)
        #: ScadBuddy's own warnings about the failed render (#408), the ones a
        #: successful render puts on `JobResult.warnings`; set by `render_job`.
        self.warnings = list(warnings)
        self.returncode = returncode
        #: The run's ERROR/WARNING lines, parsed (#252). Read off the whole log.
        self.diagnostics = list(diagnostics)
        #: The diagnostics past the cap that ``diagnostics`` does not hold.
        self.diagnostics_dropped = diagnostics_dropped


class RenderTimeoutError(OpenSCADError):
    pass


class UnknownParameterError(ValueError):
    pass


class ParameterValueError(ValueError):
    """A value a parameter does not take: the wrong type, outside its customizer
    range, or not one of its options (#432). Names the parameter, for the 422."""

    def __init__(self, parameter: str, message: str) -> None:
        super().__init__(message)
        self.parameter = parameter


@dataclass(frozen=True)
class ProcessOutput:
    returncode: int
    log_tail: list[str]
    duration_s: float
    #: Base names of the files the run could not open, in first-seen order. Read off
    #: the whole log, not the tail: the message comes early and a long log drops it.
    missing_files: tuple[str, ...] = ()
    #: Every ERROR/WARNING-class line, parsed (#252). Also read off the whole log:
    #: a parser error is the first thing OpenSCAD prints.
    diagnostics: tuple[Diagnostic, ...] = ()
    #: How many more there were past the cap (``MAX_DIAGNOSTICS``).
    diagnostics_dropped: int = 0
    #: What the template echoed for the user (`template_note`), in first-seen order
    #: and once each. Read off the whole log for the same reason as `missing_files`.
    notes: tuple[str, ...] = ()
    #: The last `echo(plates = N)` the run logged, or ``None`` when it logged none.
    #: Also read off the whole log: an echo at the top of a long model is not in
    #: the tail.
    plates: int | None = None


def plate_count(line: str) -> int | None:
    """The plate count ``line`` states, if it is a template's `echo(plates = N)`."""
    match = _PLATES.match(line)
    return int(match["count"]) if match else None


def template_note(line: str) -> str | None:
    """The text of a `NOTE:`/`WARNING:` echo on ``line``, without prefix or quoting."""
    match = _TEMPLATE_NOTE.match(line)
    if match is None:
        return None
    return match["text"].strip() or None


def missing_file(line: str) -> str | None:
    """The base name of the file ``line`` says OpenSCAD could not open, if any."""
    match = _MISSING_FILE.match(line)
    if match is None:
        return None
    return Path(match["imported"] or match["surface"] or "").name


def quote_string(value: str) -> str:
    return '"' + "".join(_ESCAPES.get(ch, ch) for ch in value) + '"'


def _format_number(value: ParamValue) -> str:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"expected a number, got {value!r}")
    return repr(float(value))


def _format_value(value: ParamValue) -> str:
    return f'"{value}"' if isinstance(value, str) else f"{value:g}"


def _require_in_range(parameter: Parameter, value: int | float) -> None:
    """The customizer's ``[min:max]``, inclusive (#432). The step is not enforced: it
    is the widget's increment, OpenSCAD renders any value, and a bundled default
    (plant-label ``thickness = 2.5`` on ``[1.6:0.2:5]``) sits off its grid."""
    low, high = parameter.min, parameter.max
    if (low is not None and value < low) or (high is not None and value > high):
        bounds = (
            f"between {low:g} and {high:g}"
            if low is not None and high is not None
            else f"at least {low:g}"
            if low is not None
            else f"at most {high:g}"
        )
        raise ParameterValueError(
            parameter.name, f"parameter {parameter.name!r} must be {bounds}, got {value:g}"
        )


def _require_option(parameter: Parameter, value: ParamValue) -> None:
    """One of the select's options, or a value the template retired (#432)."""
    allowed = [option.value for option in parameter.options]
    if any(value == candidate for candidate in (*allowed, *parameter.retired)):
        return
    raise ParameterValueError(
        parameter.name,
        f"parameter {parameter.name!r} must be one of "
        f"{', '.join(_format_value(candidate) for candidate in allowed)}, got {value!r}",
    )


def path_like(value: str) -> str | None:
    """Why ``value`` would take ``import()``/``surface()`` out of the model's
    directory, or None when it would not.

    OpenSCAD opens whatever path a string hands those two calls, relative to the
    calling file or absolute (#281). A value with neither a leading ``/`` nor a
    ``..`` component can only name something at or below the directory of the file
    that reads it, so those are the two shapes refused. Judged by path component,
    not substring, so ordinary text such as ``"Wait..."`` or ``"3/4 inch"`` passes;
    the false positives left are text that genuinely starts with ``/`` (``"/r/foo"``)
    or has ``..`` between slashes.
    """
    if value.startswith("/"):
        return "an absolute path"
    if ".." in value.split("/"):
        return "a '..' path component"
    return None


def _refuse_path_like(parameter: Parameter, value: str) -> None:
    # The template's own values are its business, as for a file parameter: only a
    # value the template did not write itself is judged.
    if value == parameter.initial or any(option.value == value for option in parameter.options):
        return
    reason = path_like(value)
    if reason is not None:
        raise ValueError(
            f"parameter {parameter.name!r} looks like a file path ({reason}), which a "
            f"template could read outside its own directory; got {value!r}"
        )


def format_scad_value(parameter: Parameter, value: ParamValue) -> str:
    """``value`` as an OpenSCAD literal for ``-D``; raises `ParameterValueError`
    unless ``parameter`` takes it."""
    try:
        return _format_checked(parameter, value)
    except ParameterValueError:
        raise
    except ValueError as error:
        raise ParameterValueError(parameter.name, str(error)) from None


def _format_checked(parameter: Parameter, value: ParamValue) -> str:
    if parameter.type == "boolean":
        if not isinstance(value, bool):
            raise ValueError(f"parameter {parameter.name!r} expects a boolean, got {value!r}")
        return "true" if value else "false"
    if parameter.type == "file":
        # The model's own default is its business; anything else (a staged upload, a
        # sample the template ships) must be a bare name, so no value -- whatever
        # the route checked -- reaches import() as a path.
        if not isinstance(value, str) or (
            value not in ("", parameter.initial) and not is_bare_filename(value)
        ):
            raise ValueError(
                f"parameter {parameter.name!r} expects an uploaded or sample file, got {value!r}"
            )
        return quote_string(value)
    if parameter.type in ("string", "color", "font"):
        if not isinstance(value, str):
            raise ValueError(f"parameter {parameter.name!r} expects a string, got {value!r}")
        _refuse_path_like(parameter, value)
        return quote_string(value)
    if parameter.type == "select":
        if any(isinstance(option.value, str) for option in parameter.options):
            if not isinstance(value, str):
                raise ValueError(f"parameter {parameter.name!r} expects a string, got {value!r}")
            _refuse_path_like(parameter, value)
            _require_option(parameter, value)
            return quote_string(value)
        formatted = _format_number(value)
        _require_option(parameter, value)
        return formatted
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"parameter {parameter.name!r} expects a number, got {value!r}")
    _require_in_range(parameter, value)
    if parameter.type == "integer":
        return str(int(value))
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


async def _drain(
    stream: asyncio.StreamReader,
    tail: deque[str],
    missing: list[str],
    notes: list[str],
    diagnostics: DiagnosticCollector,
    plates: list[int],
) -> None:
    async for raw in stream:
        line = raw.decode("utf-8", "replace").rstrip("\n")
        tail.append(line)
        diagnostics.feed(line)
        name = missing_file(line)
        if name is not None and name not in missing:
            missing.append(name)
        note = template_note(line)
        if note is not None and note not in notes and len(notes) < MAX_NOTES:
            notes.append(note)
        count = plate_count(line)
        if count is not None:
            plates.append(count)


def _kill_group(process: asyncio.subprocess.Process) -> None:
    """Kill the child and everything it spawned: a plain kill() on the parent would
    orphan its children (spec 2026-09-27 §3.4, a phase-1 requirement)."""
    with suppress(ProcessLookupError):
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)


async def _run_openscad(args: Sequence[str], *, cwd: Path, config: Config) -> ProcessOutput:
    started = time.monotonic()
    # FONTCONFIG_FILE, so `text(font = ...)` resolves the families downloaded onto
    # the data volume and not only the ones baked into the image (issue #82).
    # Built from an allowlist, not copied (#281): a template can read
    # /proc/self/environ, so nothing the backend holds may be in it.
    env = env_for(config.data_dir)
    # Set or absent, never inherited (the allowlist drops it): a model sees exactly
    # the libraries it declares (#93), so one that forgot to declare BOSL2 fails
    # here the same way it would on a fresh install, instead of working by accident.
    if config.library_path:
        env["OPENSCADPATH"] = os.pathsep.join(str(path) for path in config.library_path)
    # #994: a target outside the model and its libraries is refused before the run,
    # and the run itself is confined to them; see render/confinement.py.
    escaping = await asyncio.to_thread(escaping_includes, cwd, args[-1]) if args else []
    if escaping:
        log = [include.log_line() for include in escaping]
        raise OpenSCADError(
            "the model includes a file outside its directory and libraries",
            log,
            diagnostics=parse_diagnostics(log),
        )
    process = await asyncio.create_subprocess_exec(
        *sandboxed(config.openscad, args, cwd=cwd, config=config, env=env),
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=env,
        start_new_session=True,
    )
    tail: deque[str] = deque(maxlen=LOG_TAIL_LINES)
    missing: list[str] = []
    notes: list[str] = []
    plates: list[int] = []
    collector = DiagnosticCollector(roots=(cwd, *config.library_path))
    assert process.stdout is not None
    drain = asyncio.create_task(_drain(process.stdout, tail, missing, notes, collector, plates))
    try:
        returncode = await asyncio.wait_for(process.wait(), timeout=config.render_timeout)
    except TimeoutError:
        _kill_group(process)
        await process.wait()
        drain.cancel()
        raise RenderTimeoutError(
            f"openscad timed out after {config.render_timeout:g}s",
            tail,
            diagnostics=collector.diagnostics,
            diagnostics_dropped=collector.dropped,
            missing_files=missing,
        ) from None
    except asyncio.CancelledError:
        # A cancelled caller (a superseded parse check, a shutting-down worker) must not
        # leave the subprocess running: it would hold the CPU the next one needs.
        _kill_group(process)
        await process.wait()
        drain.cancel()
        raise
    await drain
    duration = time.monotonic() - started
    if returncode != 0:
        raise OpenSCADError(
            f"openscad exited with {returncode}",
            tail,
            returncode,
            collector.diagnostics,
            collector.dropped,
            missing_files=missing,
        )
    return ProcessOutput(
        returncode=returncode,
        log_tail=list(tail),
        duration_s=duration,
        missing_files=tuple(missing),
        diagnostics=tuple(collector.diagnostics),
        diagnostics_dropped=collector.dropped,
        notes=tuple(notes),
        plates=plates[-1] if plates else None,
    )


def _export_attributes(args: Sequence[str]) -> dict[str, str]:
    """What the call renders, never its defines: those carry parameter values."""
    attributes: dict[str, str] = {}
    if "-o" in args:
        index = args.index("-o")
        if index + 1 < len(args):
            attributes["scadbuddy.openscad.format"] = Path(args[index + 1]).suffix.lstrip(".")
    for arg in args:
        if arg.startswith("--backend="):
            attributes["scadbuddy.openscad.backend"] = arg.removeprefix("--backend=")
    return attributes


async def run_openscad(
    args: Sequence[str], *, cwd: Path, config: Config, failure_is_fallback: bool = False
) -> ProcessOutput:
    """``failure_is_fallback``: the caller handles an `OpenSCADError` as a fallback, not
    a failure (a colour's solid, spec 09-22 §6.3), so the span records the exit code and
    ends without ERROR: a trace's spans are ERROR exactly when its job fails (spec
    2026-10-01 §6)."""
    fallback: OpenSCADError | None = None
    with span("openscad.export", attributes=_export_attributes(args)) as current:
        try:
            output = await _run_openscad(args, cwd=cwd, config=config)
        except OpenSCADError as error:
            if error.returncode is not None:
                current.set_attribute("scadbuddy.openscad.exit_code", error.returncode)
            if not failure_is_fallback:
                raise
            fallback = error
        else:
            current.set_attribute("scadbuddy.openscad.exit_code", output.returncode)
            return output
    raise fallback


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


async def cached_schema(scad_path: Path, cache_path: Path, *, config: Config) -> CustomizerSchema:
    source = scad_path.read_text(encoding="utf-8")
    cached = load_cached_schema(cache_path, source_sha256(source), library_path=config.library_path)
    if cached is not None:
        return cached
    schema = await export_schema(scad_path, config=config)
    store_cached_schema(cache_path, schema, library_path=config.library_path)
    return schema


async def render_3mf(
    scad_path: Path,
    schema: CustomizerSchema,
    params: Mapping[str, ParamValue],
    out_path: Path,
    *,
    config: Config,
    extra_defines: Sequence[str] = (),
    failure_is_fallback: bool = False,
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
    return await run_openscad(
        args, cwd=scad_path.parent, config=config, failure_is_fallback=failure_is_fallback
    )
