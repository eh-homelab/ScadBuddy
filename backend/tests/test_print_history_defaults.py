"""Print history's default values (#1245): a template whose schema cannot be built is a
fallback the history handles, so its ``openscad.export`` span is no failure. And the
output's files and plate names, read from the blob store (#7256)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

import scadbuddy.api.print_history as print_history
from scadbuddy.api.print_history import _Defaults, _output_files, _output_plate_names
from scadbuddy.core.config import Config
from scadbuddy.core.paths import DataPaths
from scadbuddy.library.history import ModelHistory
from scadbuddy.library.outputs import MODEL_NAME, PREVIEW_NAME
from scadbuddy.render.bambu3mf import PlateEntry
from scadbuddy.render.solids import WRAPPER_PREFIX
from tests.support.output_blobs import blob_output


@pytest.mark.requires_postgres
async def test_print_history_sizes_from_index(tmp_path: Path, pg_conninfo: str) -> None:
    async with blob_output(tmp_path, pg_conninfo) as output:
        sizes = {
            name: (output.directory / name).stat().st_size for name in (MODEL_NAME, PREVIEW_NAME)
        }
        # The files are gone: the sizes come from the blob index.
        for name in sizes:
            (output.directory / name).unlink()
        files = await asyncio.to_thread(_output_files, output.store, output.meta)
        assert {(file.kind, file.size) for file in files} == {
            ("output_3mf", sizes[MODEL_NAME]),
            ("preview_glb", sizes[PREVIEW_NAME]),
        }
        assert {file.url for file in files} == {
            f"/api/v1/outputs/{output.meta.id}/model.3mf",
            f"/api/v1/outputs/{output.meta.id}/preview.glb",
        }


@pytest.mark.requires_postgres
async def test_print_history_plate_names_from_model_blob(
    tmp_path: Path, pg_conninfo: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    read: list[Path] = []

    def plates_of(path: Path) -> list[PlateEntry]:
        read.append(path)
        assert path.is_file()
        return [PlateEntry(1, None, "Tags"), PlateEntry(2, None, None)]

    monkeypatch.setattr(print_history, "plates_of", plates_of)
    async with blob_output(tmp_path, pg_conninfo) as output:
        (output.directory / MODEL_NAME).unlink()
        assert await _output_plate_names(output.store, output.meta.id) == {1: "Tags"}
        assert read and read[0].parent != output.directory
        assert await _output_plate_names(output.store, "0" * 32) == {}


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
