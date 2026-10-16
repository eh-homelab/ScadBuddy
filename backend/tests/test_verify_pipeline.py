"""verify.sh's pipeline check (spec 2026-09-27 §5.5)."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path

import pytest

from scadbuddy.core.paths import DataPaths
from scadbuddy.workflows.verify_pipeline import dev_server, verify
from tests.support.openscad import install_fake_openscad
from tests.test_template_pipeline import NEVER_YIELDS

pytestmark = [
    pytest.mark.requires_temporal,
    # A Temporal address alone is not enough: verify starts its own dev server.
    pytest.mark.skipif(dev_server() is None, reason="no temporal CLI"),
]

SOURCE = """\
async def run(ctx, inputs):
    a = await ctx.render("model.scad", width=inputs["params"]["width"])
    await ctx.output(plates=await ctx.pack([(a, inputs["n"])]), name="n")
"""


def _template(tmp_path: Path, source: str) -> Path:
    template = tmp_path / "demo"
    (template / "pipeline").mkdir(parents=True)
    (template / "model.scad").write_text("cube();\n")
    (template / "pipeline" / "pipeline.py").write_text(source)
    (template / "model.json").write_text(
        json.dumps({"name": "Demo", "pipeline": {"module": "pipeline/pipeline.py", "api": 1}})
    )
    return template


async def test_every_case_that_renders_passes(tmp_path: Path) -> None:
    config = install_fake_openscad(tmp_path, DataPaths(tmp_path / "unused"))
    failures = await verify(
        _template(tmp_path, SOURCE),
        [{"params": {"width": 3}, "n": 1}, {"params": {"width": 3}, "n": 2}],
        config=config,
    )
    assert failures == []


async def test_a_failing_case_is_reported(tmp_path: Path) -> None:
    config = install_fake_openscad(tmp_path, DataPaths(tmp_path / "unused"))
    failures = await verify(_template(tmp_path, SOURCE), [{"params": {"width": 3}}], config=config)
    assert len(failures) == 1 and "pipeline/pipeline.py:3: KeyError" in failures[0]


async def test_a_pipeline_that_never_yields_is_that_cases_failure(tmp_path: Path) -> None:
    """Bounded as production bounds it (`Config.pipeline_timeout`), then the next case."""
    config = replace(
        install_fake_openscad(tmp_path, DataPaths(tmp_path / "unused")),
        template_activity_max_timeout=2.0,
    )
    failures = await asyncio.wait_for(
        verify(_template(tmp_path, NEVER_YIELDS), [{"params": {}}], config=config), timeout=90
    )
    assert failures == [f"case 1: timed out after {config.pipeline_timeout:g} s"]


V_IS_STAMPED = """\
async def run(ctx, inputs):
    if inputs.get("v") != 0:
        raise RuntimeError(f"v is {inputs.get('v')!r}")
    a = await ctx.render("model.scad", width=inputs["params"]["width"])
    await ctx.output(plates=await ctx.pack([a]), name="n")
"""


async def test_a_case_runs_in_the_shape_the_api_gives_it(tmp_path: Path) -> None:
    config = install_fake_openscad(tmp_path, DataPaths(tmp_path / "unused"))
    failures = await verify(
        _template(tmp_path, V_IS_STAMPED),
        [{"params": {"width": 3}}, {"params": {"width": [3]}}],
        config=config,
    )
    # The first gets `v: 0` as the API stamps it; the second is refused as the API would.
    assert failures == ["case 2: inputs.params.width must be a number, string or boolean"]
