from __future__ import annotations

import json
from pathlib import Path

from scadbuddy.tools.export_openapi import DEFAULT_OUTPUT, export

EXPECTED_PATHS = {
    "/healthz",
    "/api/v1/models",
    "/api/v1/models/check",
    "/api/v1/models/{slug}",
    "/api/v1/models/{slug}/source",
    "/api/v1/models/{slug}/schema",
    "/api/v1/models/{slug}/thumbnail",
    "/api/v1/models/{slug}/render",
    "/api/v1/models/{slug}/versions",
    "/api/v1/models/{slug}/versions/{commit}/source",
    "/api/v1/models/{slug}/versions/{commit}/schema",
    "/api/v1/models/{slug}/versions/{commit}/diff",
    "/api/v1/models/{slug}/versions/{commit}/restore",
    "/api/v1/models/{slug}/outputs",
    "/api/v1/jobs/{job_id}",
    "/api/v1/jobs/{job_id}/preview.glb",
    "/api/v1/outputs/{output_id}",
    "/api/v1/outputs/{output_id}/edit",
    "/api/v1/outputs/{output_id}/model.3mf",
    "/api/v1/outputs/{output_id}/thumbnail",
    "/api/v1/outputs/{output_id}/send",
    "/api/v1/print/presets",
    "/api/v1/print/projects",
    "/api/v1/print/outputs/{output_id}/project",
    "/api/v1/print/pipelines",
    "/api/v1/print/models/{slug}/pipelines",
    "/api/v1/print/models/{slug}/pipeline",
    "/api/v1/print/outputs/{output_id}/eligibility",
    "/api/v1/print/outputs/{output_id}/filaments",
    "/api/v1/print/outputs/{output_id}/progress",
    "/api/v1/print/outputs/{output_id}/run",
    "/api/v1/settings",
    "/api/v1/settings/print-options",
    "/api/v1/settings/test",
    "/api/v1/settings/targets",
    "/api/v1/settings/register-sidebar",
    "/api/v1/fonts",
    "/api/v1/fonts/catalogue",
    "/api/v1/fonts/install",
}


def test_every_route_in_the_spec_is_published(tmp_path: Path) -> None:
    schema = json.loads(export(tmp_path / "openapi.json").read_text(encoding="utf-8"))
    assert set(schema["paths"]) == EXPECTED_PATHS


def test_the_committed_schema_is_up_to_date(tmp_path: Path) -> None:
    """The frontend generates its client from the committed file; regenerate it with
    ``uv run python -m scadbuddy.tools.export_openapi``."""
    fresh = export(tmp_path / "openapi.json").read_text(encoding="utf-8")
    assert DEFAULT_OUTPUT.read_text(encoding="utf-8") == fresh
