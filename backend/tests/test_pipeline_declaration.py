"""model.json's `pipeline` and load_pipeline (spec 2026-09-27 §5.1, §3.4 step 1, §8.1)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from scadbuddy.core.config import Config
from scadbuddy.core.paths import DataPaths
from scadbuddy.library.assets import AssetStore
from scadbuddy.library.catalogue import ModelMeta
from scadbuddy.library.pipelines import (
    DEFAULT_PIPELINE_FILE,
    DEFAULT_PIPELINE_SOURCE,
    INPUTS_VERSION_PARSE_LIMIT,
    inputs_version_of,
    pipeline_version_of,
)
from scadbuddy.store.local import LocalBlobStore
from scadbuddy.workflows.activities import WorkerDeps
from scadbuddy.workflows.models import LoadRequest
from scadbuddy.workflows.pipeline_activities import PipelineActivities

PIPELINE = "INPUTS_VERSION = 3\n\nasync def run(ctx, inputs):\n    return None\n"


def _template(tmp_path: Path, meta: dict[str, object], files: dict[str, str]) -> DataPaths:
    paths = DataPaths(tmp_path / "data")
    paths.ensure()
    paths.model_dir("demo").mkdir(parents=True)
    paths.model_source("demo").write_text("cube();\n", encoding="utf-8")
    paths.model_meta("demo").write_text(json.dumps({"name": "Demo", **meta}), encoding="utf-8")
    for name, body in files.items():
        target = paths.model_dir("demo") / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
    return paths


def _acts(paths: DataPaths) -> PipelineActivities:
    deps = WorkerDeps(
        config=Config(data_dir=paths.root),
        paths=paths,
        assets=AssetStore(paths.assets),
        blobs=LocalBlobStore(paths.blobs),
        refs=None,  # type: ignore[arg-type]
        projection=None,  # type: ignore[arg-type]
    )
    return PipelineActivities(deps)


def test_inputs_version_is_read_without_running_the_source() -> None:
    assert inputs_version_of(PIPELINE) == 3
    assert inputs_version_of("import os\nos.system('false')\n") == 0
    assert inputs_version_of("INPUTS_VERSION = 'two'\n") == 0
    assert inputs_version_of("def (:\n") == 0
    # Measured on 3.12: ast.parse's own failures on deeply nested source, under the cap.
    assert inputs_version_of("-" * 200_000 + "1") == 0  # MemoryError: parser stack overflowed
    assert inputs_version_of("x" + ".y" * 100_000) == 0  # RecursionError during ast construction
    assert inputs_version_of("x = 1\0\n") == 0  # a NUL byte


def test_a_pipeline_past_the_cap_is_not_parsed() -> None:
    padding = "#" * INPUTS_VERSION_PARSE_LIMIT
    assert inputs_version_of("INPUTS_VERSION = 4\n" + padding) == 0
    assert inputs_version_of("INPUTS_VERSION = 4\n" + padding[:-100]) == 4


def test_inputs_version_is_what_the_module_ends_up_with() -> None:
    assert inputs_version_of("INPUTS_VERSION: int = 3\n") == 3
    assert inputs_version_of("INPUTS_VERSION = 1\nINPUTS_VERSION = 2\n") == 2
    assert inputs_version_of("INPUTS_VERSION = 5\nINPUTS_VERSION: int = 6\n") == 6
    assert inputs_version_of("INPUTS_VERSION: int\n") == 0  # annotated, never assigned
    # A last assignment that is not a literal int is what the module holds: not a version.
    assert inputs_version_of("INPUTS_VERSION = 2\nINPUTS_VERSION = 1 + 1\n") == 0


def test_a_malformed_pipeline_costs_only_the_pipeline() -> None:
    meta = ModelMeta.model_validate({"name": "Demo", "pipeline": {"module": "../x.py", "api": 1}})
    assert meta.pipeline is None
    assert meta.pipeline_error is not None and "module" in meta.pipeline_error
    dumped = meta.model_dump()
    assert "pipeline_error" not in dumped and "pipeline_raw" not in dumped
    assert dumped["pipeline"] is None  # the API never shows a malformed declaration
    assert meta.pipeline_raw == {"module": "../x.py", "api": 1}


def test_writing_model_json_keeps_a_malformed_pipeline(tmp_path: Path) -> None:
    from scadbuddy.library.catalogue import Catalogue, ModelPatch
    from scadbuddy.render.solids import WRAPPER_PREFIX

    paths = DataPaths(tmp_path / "data")
    paths.ensure()
    catalogue = Catalogue(paths, wrapper_prefix=WRAPPER_PREFIX)
    bad = {"module": "../x.py", "api": 1}
    catalogue.create(
        "demo", "cube();\n", ModelMeta.model_validate({"name": "Demo", "pipeline": bad})
    )
    assert json.loads(paths.model_meta("demo").read_text())["pipeline"] == bad
    catalogue.update("demo", ModelPatch(name="Renamed"))
    assert json.loads(paths.model_meta("demo").read_text())["pipeline"] == bad


def test_the_model_record_schema_keeps_its_properties() -> None:
    from scadbuddy.library.catalogue import ModelRecord

    properties = ModelRecord.model_json_schema(mode="serialization")["properties"]
    assert {"name", "pipeline", "pipeline_error", "inputs_version"} <= set(properties)


async def test_no_pipeline_loads_the_default(tmp_path: Path) -> None:
    loaded = await ActivityEnvironment().run(
        _acts(_template(tmp_path, {}, {})).load_pipeline, LoadRequest(slug="demo", revision=None)
    )
    assert loaded.source == DEFAULT_PIPELINE_SOURCE
    assert loaded.file == DEFAULT_PIPELINE_FILE
    assert loaded.version == "default"
    assert loaded.plate.key == "default"


async def test_a_declared_pipeline_loads_its_source_and_sha(tmp_path: Path) -> None:
    paths = _template(
        tmp_path,
        {
            "pipeline": {"module": "pipeline/pipeline.py", "api": 1},
            "ui": {"module": "ui/index.js", "api": 1},
        },
        {"pipeline/pipeline.py": PIPELINE},
    )
    loaded = await ActivityEnvironment().run(
        _acts(paths).load_pipeline, LoadRequest(slug="demo", revision=None)
    )
    assert loaded.source == PIPELINE
    assert loaded.file == "pipeline/pipeline.py"
    assert (
        loaded.version
        == hashlib.sha256(PIPELINE.encode()).hexdigest()
        == pipeline_version_of(PIPELINE)
    )
    assert (loaded.api, loaded.inputs_version, loaded.ui_api) == (1, 3, 1)


@pytest.mark.parametrize(
    ("meta", "files", "message"),
    [
        (
            {"pipeline": {"module": "pipeline/pipeline.py", "api": 7}},
            {"pipeline/pipeline.py": PIPELINE},
            "supports majors 1",
        ),
        (
            {"pipeline": {"module": "pipeline/pipeline.py", "api": 1}},
            {},
            "pipeline/pipeline.py is missing",
        ),
        ({"pipeline": {"module": "../x.py", "api": 1}}, {}, "module"),
    ],
)
async def test_an_unusable_pipeline_fails_the_job_at_load(
    tmp_path: Path, meta: dict[str, object], files: dict[str, str], message: str
) -> None:
    with pytest.raises(ApplicationError) as raised:
        await ActivityEnvironment().run(
            _acts(_template(tmp_path, meta, files)).load_pipeline,
            LoadRequest(slug="demo", revision=None),
        )
    assert raised.value.type == "PipelineApiError"
    assert raised.value.non_retryable
    assert message in raised.value.message


async def _refused(paths: DataPaths) -> ApplicationError:
    with pytest.raises(ApplicationError) as raised:
        await ActivityEnvironment().run(
            _acts(paths).load_pipeline, LoadRequest(slug="demo", revision=None)
        )
    assert raised.value.type == "PipelineApiError"
    assert raised.value.non_retryable
    return raised.value


DECLARED: dict[str, object] = {"pipeline": {"module": "pipeline/pipeline.py", "api": 1}}


@pytest.mark.parametrize(
    ("raw", "message"),
    [("[]", "model.json"), ('{"description": "no name"}', "model.json")],
)
async def test_a_model_json_that_is_no_template_is_refused(
    tmp_path: Path, raw: str, message: str
) -> None:
    paths = _template(tmp_path, {}, {})
    paths.model_meta("demo").write_text(raw, encoding="utf-8")
    assert message in (await _refused(paths)).message


async def test_a_pipeline_that_is_not_utf8_is_refused(tmp_path: Path) -> None:
    paths = _template(tmp_path, DECLARED, {"pipeline/pipeline.py": ""})
    (paths.model_dir("demo") / "pipeline" / "pipeline.py").write_bytes(b"x = '\xff'\n")
    assert "pipeline/pipeline.py could not be read" in (await _refused(paths)).message


async def test_a_directory_where_the_pipeline_should_be_is_refused(tmp_path: Path) -> None:
    paths = _template(tmp_path, DECLARED, {})
    (paths.model_dir("demo") / "pipeline" / "pipeline.py").mkdir(parents=True)
    assert "pipeline/pipeline.py could not be read" in (await _refused(paths)).message


async def test_a_pipeline_symlinked_out_of_the_template_is_refused(tmp_path: Path) -> None:
    outside = tmp_path / "secret.py"
    outside.write_text("TOKEN = 'do not record me'\n", encoding="utf-8")
    paths = _template(tmp_path, DECLARED, {})
    (paths.model_dir("demo") / "pipeline").mkdir()
    (paths.model_dir("demo") / "pipeline" / "pipeline.py").symlink_to(outside)
    error = await _refused(paths)
    assert "outside the template" in error.message
    assert "do not record me" not in error.message


async def test_a_pipeline_symlinked_within_the_template_loads(tmp_path: Path) -> None:
    paths = _template(tmp_path, DECLARED, {"pipeline/real.py": PIPELINE})
    (paths.model_dir("demo") / "pipeline" / "pipeline.py").symlink_to("real.py")
    loaded = await ActivityEnvironment().run(
        _acts(paths).load_pipeline, LoadRequest(slug="demo", revision=None)
    )
    assert loaded.source == PIPELINE and loaded.file == "pipeline/pipeline.py"
