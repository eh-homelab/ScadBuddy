"""The pipeline's own activities (spec 2026-09-27 §5.2): loading the template's
pipeline, packing parts onto plates, and writing an output."""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from temporalio import activity
from temporalio.exceptions import ApplicationError

from scadbuddy.library.catalogue import meta_from_raw
from scadbuddy.library.pipelines import (
    DEFAULT_PIPELINE_FILE,
    DEFAULT_PIPELINE_SOURCE,
    PIPELINE_API_SUPPORTED,
    inputs_version_of,
    pipeline_version_of,
)
from scadbuddy.render.job_models import PipelineOutput
from scadbuddy.render.jobs import resolve_source
from scadbuddy.render.plate import plate_for
from scadbuddy.store import BlobStore
from scadbuddy.store.cache import StaleBlobError
from scadbuddy.store.content import BlobScope
from scadbuddy.template import ACTIVITY_RESULT_NAME
from scadbuddy.workflows.activities import RenderActivities, WorkerDeps, _heartbeating
from scadbuddy.workflows.models import (
    Failure,
    Layout,
    LoadedPipeline,
    LoadRequest,
    MigrateRequest,
    MigrateResult,
    OutputRequest,
    PackRequest,
    PlateSize,
    TemplateCall,
)
from scadbuddy.workflows.outputs import build_output
from scadbuddy.workflows.packing import PackError, shelf_pack
from scadbuddy.workflows.template_process import TemplateError, run_template, template_out_key

#: `migrate`'s own bound: under the `SHORT` (60 s) that the activity's budget gives it
#: after the snapshot's transfer, so the subprocess's kill fires before Temporal's.
MIGRATE_SECONDS = 30.0


@dataclass
class _Call:
    """One `act-` key's lock and how many calls hold or wait on it."""

    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    users: int = 0


def _pipeline_sha(model_dir: Path, *, live: bool) -> str:
    """What the call's code is, for its `act-` key. A revision is exact already, so
    `activities.py` alone; the live template, every `*.py` under `pipeline/` (by path),
    since `activities.py` may import a sibling that is edited in between."""
    pipeline = model_dir / "pipeline"
    if not live:
        source = pipeline / "activities.py"
        return hashlib.sha256(source.read_bytes()).hexdigest() if source.is_file() else ""
    digest = hashlib.sha256()
    for path in sorted(pipeline.rglob("*.py")) if pipeline.is_dir() else []:
        digest.update(path.relative_to(pipeline).as_posix().encode() + b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def _refuse(message: str) -> ApplicationError:
    return ApplicationError(message, type="PipelineApiError", non_retryable=True)


def plate_size(model: str | None) -> PlateSize:
    plate = plate_for(model)
    return PlateSize(key=plate.key, width=plate.usable.width, depth=plate.usable.depth)


def pack_layout(req: PackRequest) -> Layout:
    if req.goal != "fewest_plates":
        raise ApplicationError(
            f"pack goal {req.goal!r} arrives with Arrange (phase 5); use 'fewest_plates'",
            type="PackError",
            non_retryable=True,
        )
    try:
        return shelf_pack(req.items, req.plate)
    except PackError as error:
        raise ApplicationError(str(error), type="PackError", non_retryable=True) from None


class PipelineActivities:
    def __init__(self, deps: WorkerDeps) -> None:
        self.deps = deps
        #: `prepare`'s snapshot step, shared: a worker on the bambuddy store has no
        #: source of its own until it materializes the revision.
        self._render = RenderActivities(deps)
        #: One run per `act-` key at a time on this worker: an identical call waits,
        #: then is answered from the blob the first one wrote.
        self._calls: dict[str, _Call] = {}

    def all(self) -> Sequence[Callable[..., Any]]:
        return [
            self.load_pipeline,
            self.pack,
            self.write_output,
            self.run_template_activity,
            self.migrate_inputs,
        ]

    async def model_dir(self, slug: str, revision: str | None) -> Path:
        """The template's directory at ``revision``: the live one, or its export."""
        d = self.deps
        await self._render.materialize(slug, revision)
        source = await resolve_source(
            slug, revision, paths=d.paths, history=d.history, fetcher=d.fetcher
        )
        return source.scad.parent

    @activity.defn(name="load_pipeline")
    async def load_pipeline(self, req: LoadRequest) -> LoadedPipeline:
        """The template's pipeline source at ``req.revision``, read and never run: the
        workflow runs it in its sandbox, where what raises is a nondeterministic call
        (`os.getpid()`, `random.random()`, `datetime.now()`, `open()`), not an
        import (spec §3.6)."""
        # Heartbeated: on the bambuddy store this downloads the revision's snapshot.
        directory = await _heartbeating(asyncio.create_task(self.model_dir(req.slug, req.revision)))
        try:
            raw = json.loads(await asyncio.to_thread((directory / "model.json").read_text, "utf-8"))
        except FileNotFoundError:
            raw = {}  # model.json is optional: the catalogue reads a missing one as empty
        except (OSError, ValueError) as error:
            raise _refuse(f"model.json could not be read: {error}") from None
        try:
            # Read as the catalogue reads it (`Catalogue.read_raw_meta`, `meta_from_raw`).
            meta = meta_from_raw(raw if isinstance(raw, dict) else {}, req.slug)
        except ValidationError as error:
            # The template's own data: a retry reads the same file.
            raise _refuse(f"model.json is not a template's: {error}") from None
        ui_api = meta.ui.api if meta.ui is not None else None
        plate = plate_size(None)
        if meta.pipeline_error is not None:
            raise _refuse(meta.pipeline_error)
        if meta.pipeline is None:
            return LoadedPipeline(
                source=DEFAULT_PIPELINE_SOURCE,
                file=DEFAULT_PIPELINE_FILE,
                api=1,
                version="default",
                inputs_version=0,
                ui_api=ui_api,
                plate=plate,
            )
        if meta.pipeline.api not in PIPELINE_API_SUPPORTED:
            majors = ", ".join(str(m) for m in PIPELINE_API_SUPPORTED)
            raise _refuse(
                f"{meta.pipeline.module} declares pipeline api {meta.pipeline.api}; "
                f"this ScadBuddy supports majors {majors}"
            )
        module = meta.pipeline.module
        try:
            # Resolved, as api/template_ui.py resolves ui/ files: a symlink out of the
            # template would put another file's contents into the workflow history.
            base = directory.resolve()
            path = (directory / module).resolve()
        except (OSError, RuntimeError) as error:  # a symlink loop is a RuntimeError
            raise _refuse(f"{module} could not be read: {error}") from None
        if not path.is_relative_to(base):
            raise _refuse(f"{module} points outside the template")
        try:
            source = await asyncio.to_thread(path.read_text, "utf-8")
        except FileNotFoundError:
            raise _refuse(f"{module} is missing from the template") from None
        except (OSError, UnicodeDecodeError) as error:
            # A directory with the module's name, or a file that is not UTF-8: the
            # template's fault, which a retry would only repeat.
            raise _refuse(f"{module} could not be read: {error}") from None
        return LoadedPipeline(
            source=source,
            file=module,
            api=meta.pipeline.api,
            version=pipeline_version_of(source),
            inputs_version=inputs_version_of(source),
            ui_api=ui_api,
            plate=plate,
        )

    @activity.defn(name="pack")
    async def pack(self, req: PackRequest) -> Layout:
        return pack_layout(req)

    @activity.defn(name="write_output")
    async def write_output(self, req: OutputRequest) -> PipelineOutput:
        """Meshes, thumbnails and a many-plate 3MF can outlast a short timeout, so it
        heartbeats, as the openscad stages do. Its budget in the workflow is
        `_openscad_timeout()` plus one `TRANSFER` per store move: each piece and each
        `Blob` it fetches, and the output it publishes."""
        model_dir = await self.model_dir(req.slug, req.record.revision)
        return await _heartbeating(
            asyncio.create_task(build_output(req, self.deps, model_dir=model_dir))
        )

    @activity.defn(name="migrate_inputs")
    async def migrate_inputs(self, req: MigrateRequest) -> MigrateResult:
        """The template's `migrate`, in the template process (§8.2): it runs template code,
        so it runs here on the worker, never in the API (§9)."""
        d = self.deps
        model_dir = await _heartbeating(asyncio.create_task(self.model_dir(req.slug, req.revision)))
        with tempfile.TemporaryDirectory(prefix="scadbuddy-migrate-") as out:
            try:
                migrated = await run_template(
                    model_dir,
                    {"mode": "migrate", "inputs": req.inputs},
                    out=Path(out),
                    out_key="",
                    python=d.template_python,
                    data_dir=d.config.data_dir,
                    timeout=MIGRATE_SECONDS,
                )
            except TemplateError as error:
                raise ApplicationError(
                    str(error), type="MigrateError", non_retryable=True
                ) from None
        return MigrateResult(
            inputs=migrated,
            from_version=int(req.inputs.get("v", 0)),
            to_version=int(migrated.get("v", 0)),
        )

    async def _localize(self, value: Any) -> Any:
        """Give a template activity the local path of every `Blob`/`Part` it is passed."""
        blobs = self.deps.blobs
        if isinstance(value, list):
            return [await self._localize(v) for v in value]
        if isinstance(value, dict):
            if value.get("kind") == "blob":
                await _fetched(blobs, value["key"])
                root = blobs.dir_for(value["key"]).resolve()
                path = (root / value["path"]).resolve()
                if not path.is_relative_to(root):
                    raise ApplicationError(
                        f"blob path {value['path']!r} leaves its blob",
                        type="TemplateActivityError",
                        non_retryable=True,
                    )
                return {**value, "local": str(path)}
            if value.get("kind") == "part":
                await _fetched(blobs, value["piece_key"])
                return {**value, "local": str(blobs.dir_for(value["piece_key"]))}
            return {k: await self._localize(v) for k, v in value.items()}
        return value

    @activity.defn(name="run_template_activity")
    async def run_template_activity(self, call: TemplateCall) -> Any:
        """One function of the template's `pipeline/activities.py`, in its own process
        group (`template_process`). The template's exception is non-retryable; a crash
        or kill of the process is retried.

        Identical calls share one `act-` blob holding the emitted files and the value
        returned (`ACTIVITY_RESULT_NAME`): one already in the store is the answer, on
        any worker, and runs nothing. The job holds a ref on the blob either way."""
        model_dir = await self.model_dir(call.slug, call.revision)
        source_sha = await asyncio.to_thread(_pipeline_sha, model_dir, live=call.revision is None)
        out_key = template_out_key(call, source_sha)
        entry = self._calls.setdefault(out_key, _Call())
        entry.users += 1
        try:
            async with entry.lock:
                return await self._run_once(call, model_dir, out_key)
        finally:
            # The last one out drops the lock: the map holds only calls in flight.
            entry.users -= 1
            if entry.users == 0 and self._calls.get(out_key) is entry:
                del self._calls[out_key]

    async def _run_once(self, call: TemplateCall, model_dir: Path, out_key: str) -> Any:
        d = self.deps
        stored = await self._stored_result(out_key)
        if stored is not None:
            await self._hold(out_key, call)
            return stored[0]
        value = await self._run_call(call, model_dir, out_key)
        await self._hold(out_key, call)
        scope = BlobScope(slug=call.slug)
        try:
            await d.blobs.publish_fresh(
                out_key, scope=scope, expected=await d.blobs.indexed_sha(out_key)
            )
        except StaleBlobError:
            # Another worker published the same call in between: its blob is the
            # answer (identical calls write identical files).
            stored = await self._stored_result(out_key)
            if stored is None:
                raise
            return stored[0]
        return value

    async def _stored_result(self, out_key: str) -> tuple[Any] | None:
        """The value a finished identical call returned, as a 1-tuple; None when the
        store holds no finished call (nothing, or files from before the result file)."""
        blobs = self.deps.blobs
        if not await blobs.fetch(out_key):
            return None
        result = blobs.dir_for(out_key) / ACTIVITY_RESULT_NAME
        if not result.is_file():
            return None
        return (json.loads(await asyncio.to_thread(result.read_text, encoding="utf-8")),)

    async def _hold(self, out_key: str, call: TemplateCall) -> None:
        # `dir_for` first (it touches the blob), as every claimant does before its ref.
        self.deps.blobs.dir_for(out_key)
        await asyncio.to_thread(self.deps.refs.add, out_key, "job", call.job_id)

    async def _run_call(self, call: TemplateCall, model_dir: Path, out_key: str) -> Any:
        d = self.deps
        # Nothing of an attempt that did not finish survives into this one.
        await asyncio.to_thread(shutil.rmtree, d.blobs.dir_for(out_key))
        out = d.blobs.dir_for(out_key)
        request = {
            "mode": "call",
            "name": call.name,
            "args": await self._localize(call.args),
            "kwargs": await self._localize(call.kwargs),
        }
        work = asyncio.create_task(
            run_template(
                model_dir,
                request,
                out=out,
                out_key=out_key,
                python=d.template_python,
                data_dir=d.config.data_dir,
                timeout=call.timeout_s,
            )
        )
        try:
            value = await _heartbeating(work)
        except TemplateError as error:
            raise ApplicationError(
                str(error),
                Failure(error=str(error), log_tail=error.log_tail),
                type="TemplateActivityError",
                non_retryable=not error.retryable,
            ) from None
        await asyncio.to_thread(
            (out / ACTIVITY_RESULT_NAME).write_text, json.dumps(value), encoding="utf-8"
        )
        return value


async def _fetched(blobs: BlobStore, key: str) -> None:
    """A blob passed to a template activity must be in the store: its absence is a
    store problem, not the template's line that would then fail to read it."""
    if not await blobs.fetch(key):
        raise ApplicationError(
            f"blob {key} is not in the store", type="TemplateActivityError", non_retryable=True
        )
