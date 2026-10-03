"""Render stages and openscad calls are spans (spec 2026-10-01 §5.1)."""

from __future__ import annotations

from pathlib import Path

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from scadbuddy.core.config import Config
from scadbuddy.core.metrics import Metrics
from scadbuddy.render.jobs import timed_stage
from scadbuddy.render.runner import OpenSCADError, run_openscad


def test_a_timed_stage_is_a_span_and_still_timed(spans: InMemorySpanExporter) -> None:
    metrics = Metrics()
    with timed_stage(metrics)("render"):
        pass
    with timed_stage(None)("thumbnail"):
        pass
    assert [s.name for s in spans.get_finished_spans()] == ["render.render", "render.thumbnail"]
    timed = metrics.registry.get_sample_value(
        "scadbuddy_render_stage_seconds_count", {"stage": "render"}
    )
    assert timed == 1


def test_a_failed_stage_is_an_error_with_its_class(spans: InMemorySpanExporter) -> None:
    with pytest.raises(OpenSCADError), timed_stage(None)("render"):
        raise OpenSCADError("openscad exited with 1", ["SECRET LINE"], 1, [], 0)
    (failed,) = spans.get_finished_spans()
    assert (failed.attributes or {})["scadbuddy.failure_class"] == "OpenSCADError"


async def test_an_openscad_call_is_an_export_span(
    spans: InMemorySpanExporter, fake_openscad: str, tmp_path: Path
) -> None:
    scad = tmp_path / "m.scad"
    scad.write_text("cube(1);")
    config = Config(openscad=fake_openscad, data_dir=tmp_path / "data")
    await run_openscad(
        ["--backend=Manifold", "-o", str(tmp_path / "out.3mf"), scad.name],
        cwd=tmp_path,
        config=config,
    )
    (export,) = [s for s in spans.get_finished_spans() if s.name == "openscad.export"]
    attributes = export.attributes or {}
    assert attributes["scadbuddy.openscad.format"] == "3mf"
    assert attributes["scadbuddy.openscad.backend"] == "Manifold"
    assert attributes["scadbuddy.openscad.exit_code"] == 0
    assert "cube" not in repr(attributes)


async def test_a_failed_export_records_its_exit_code(
    spans: InMemorySpanExporter, tmp_path: Path
) -> None:
    binary = tmp_path / "failing-openscad"
    binary.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    binary.chmod(0o755)
    config = Config(openscad=str(binary), data_dir=tmp_path / "data")
    with pytest.raises(OpenSCADError):
        await run_openscad(["-o", str(tmp_path / "out.3mf")], cwd=tmp_path, config=config)
    (export,) = [s for s in spans.get_finished_spans() if s.name == "openscad.export"]
    attributes = export.attributes or {}
    assert attributes["scadbuddy.openscad.exit_code"] == 1
    assert attributes["scadbuddy.failure_class"] == "OpenSCADError"
