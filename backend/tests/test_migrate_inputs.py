"""migrate(inputs, from_version) (spec 2026-09-27 §8.2)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from scadbuddy.core.config import Config
from scadbuddy.core.paths import DataPaths
from scadbuddy.library.assets import AssetStore
from scadbuddy.store.local import LocalBlobStore
from scadbuddy.workflows import pipeline_activities
from scadbuddy.workflows.activities import WorkerDeps
from scadbuddy.workflows.models import MigrateRequest
from scadbuddy.workflows.pipeline_activities import PipelineActivities

PIPELINE = """\
INPUTS_VERSION = 2

def migrate(inputs, from_version):
    if from_version == 0:
        inputs = {**inputs, "house": {"cols": inputs["params"].get("cols", 1)}}
    if from_version <= 1:
        inputs = {**inputs, "pitch": 35}
    return inputs

async def run(ctx, inputs):
    pass
"""


def _acts(tmp_path: Path, source: str) -> PipelineActivities:
    paths = DataPaths(tmp_path / "data")
    paths.ensure()
    paths.model_dir("demo").mkdir(parents=True)
    paths.model_source("demo").write_text("cube();\n", encoding="utf-8")
    paths.model_meta("demo").write_text(
        '{"name": "Demo", "pipeline": {"module": "pipeline/pipeline.py", "api": 1}}',
        encoding="utf-8",
    )
    (paths.model_dir("demo") / "pipeline").mkdir()
    (paths.model_dir("demo") / "pipeline" / "pipeline.py").write_text(source, encoding="utf-8")
    return PipelineActivities(
        WorkerDeps(
            config=Config(data_dir=paths.root),
            paths=paths,
            assets=AssetStore(paths.assets),
            blobs=LocalBlobStore(paths.blobs),
            refs=None,  # type: ignore[arg-type]
            projection=None,  # type: ignore[arg-type]
            template_python=sys.executable,
        )
    )


async def test_old_inputs_are_migrated_and_stamped(tmp_path: Path) -> None:
    result = await ActivityEnvironment().run(
        _acts(tmp_path, PIPELINE).migrate_inputs,
        MigrateRequest(slug="demo", revision=None, inputs={"params": {"cols": 3}, "v": 0}),
    )
    assert (result.from_version, result.to_version) == (0, 2)
    assert result.inputs == {"params": {"cols": 3}, "house": {"cols": 3}, "pitch": 35, "v": 2}


async def test_current_inputs_come_back_unchanged(tmp_path: Path) -> None:
    inputs = {"params": {}, "v": 2, "x": 1}
    result = await ActivityEnvironment().run(
        _acts(tmp_path, PIPELINE).migrate_inputs,
        MigrateRequest(slug="demo", revision=None, inputs=inputs),
    )
    assert result.inputs == inputs


@pytest.mark.parametrize(
    ("source", "inputs", "message"),
    [
        (
            PIPELINE,
            {"params": {}, "v": 5},
            "these inputs are v5; the template's INPUTS_VERSION is 2",
        ),
        (
            "INPUTS_VERSION = 1\n\nasync def run(ctx, inputs):\n    pass\n",
            {"params": {}, "v": 0},
            "defines no migrate",
        ),
        (
            "INPUTS_VERSION = 1\n\ndef migrate(inputs, v):\n    return inputs['nope']\n",
            {"params": {}, "v": 0},
            "pipeline/pipeline.py:4: KeyError",
        ),
    ],
)
async def test_inputs_newer_than_the_template_are_refused(
    tmp_path: Path, source: str, inputs: dict[str, object], message: str
) -> None:
    with pytest.raises(ApplicationError) as raised:
        await ActivityEnvironment().run(
            _acts(tmp_path, source).migrate_inputs,
            MigrateRequest(slug="demo", revision=None, inputs=inputs),
        )
    assert raised.value.type == "MigrateError" and raised.value.non_retryable
    assert message in raised.value.message


async def test_a_migrate_that_hangs_is_killed_and_named(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pipeline_activities, "MIGRATE_SECONDS", 2.0)
    source = "import time\nINPUTS_VERSION = 1\n\ndef migrate(inputs, v):\n    time.sleep(300)\n"
    with pytest.raises(ApplicationError) as raised:
        await ActivityEnvironment().run(
            _acts(tmp_path, source).migrate_inputs,
            MigrateRequest(slug="demo", revision=None, inputs={"params": {}, "v": 0}),
        )
    assert raised.value.type == "MigrateError" and raised.value.non_retryable
    assert "pipeline/pipeline.py:migrate timed out after 2s" in raised.value.message
