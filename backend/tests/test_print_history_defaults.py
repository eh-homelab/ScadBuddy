"""Print history's default values (#1245): a template whose schema cannot be built is a
fallback the history handles, so its ``openscad.export`` span is no failure."""

from __future__ import annotations

from pathlib import Path

from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from scadbuddy.api.print_history import _Defaults
from scadbuddy.core.config import Config
from scadbuddy.core.paths import DataPaths
from scadbuddy.library.history import ModelHistory
from scadbuddy.render.solids import WRAPPER_PREFIX


async def test_a_template_whose_schema_fails_has_no_defaults_and_no_error_span(
    tmp_path: Path, spans: InMemorySpanExporter
) -> None:
    paths = DataPaths(tmp_path / "data")
    paths.model_dir("box").mkdir(parents=True)
    paths.model_source("box").write_text("cube(", encoding="utf-8")
    binary = tmp_path / "fake-openscad"
    binary.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    binary.chmod(0o755)
    defaults = _Defaults(
        paths=paths,
        history=ModelHistory(paths.models, wrapper_prefix=WRAPPER_PREFIX),
        config=Config(openscad=str(binary), data_dir=paths.root),
        fetcher=None,  # type: ignore[arg-type]
    )

    assert await defaults._defaults("box", None) is None

    exports = [s for s in spans.get_finished_spans() if s.name == "openscad.export"]
    assert exports
    assert {s.status.status_code for s in exports} == {StatusCode.UNSET}
