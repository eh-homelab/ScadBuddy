from __future__ import annotations

import json
from pathlib import Path

from scadbuddy.tools.export_openapi import DEFAULT_OUTPUT, export

EXPECTED_PATHS = {
    "/healthz",
    "/api/v1/models",
    "/api/v1/models/check",
    "/api/v1/models/import",
    "/api/v1/models/{slug}",
    "/api/v1/models/{slug}/duplicate",
    "/api/v1/models/{slug}/upstream",
    "/api/v1/models/{slug}/upstream/merge",
    "/api/v1/models/{slug}/upstream/dismiss",
    "/api/v1/models/{slug}/upstream/detach",
    "/api/v1/models/{slug}/source",
    "/api/v1/models/{slug}/schema",
    "/api/v1/models/{slug}/thumbnail",
    "/api/v1/models/{slug}/readme",
    "/api/v1/models/{slug}/presets",
    "/api/v1/models/{slug}/presets/{preset_id}",
    "/api/v1/models/{slug}/presets/{preset_id}/duplicate",
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
    "/api/v1/outputs/{output_id}/plates",
    "/api/v1/outputs/{output_id}/plates/{index}/thumbnail",
    "/api/v1/outputs/{output_id}/send",
    "/api/v1/print/presets",
    "/api/v1/print/projects",
    "/api/v1/print/outputs/{output_id}/project",
    "/api/v1/print/pipelines",
    "/api/v1/print/models/{slug}/pipelines",
    "/api/v1/print/models/{slug}/pipeline",
    "/api/v1/print/models/{slug}/choices",
    "/api/v1/print/printers/{printer_id}/bed-type",
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
    "/api/v1/plate",
    "/api/v1/plate/fit",
    "/api/v1/plates",
    "/api/v1/libraries",
    "/api/v1/models/{slug}/assets",
    "/api/v1/models/{slug}/assets/{asset_id}",
    "/api/v1/models/{slug}/assets/{asset_id}/content",
    "/api/v1/models/{slug}/samples/{name}",
    "/api/v1/models/{slug}/libraries/{name}",
}


def test_every_route_in_the_spec_is_published(tmp_path: Path) -> None:
    schema = json.loads(export(tmp_path / "openapi.json").read_text(encoding="utf-8"))
    assert set(schema["paths"]) == EXPECTED_PATHS


def test_the_committed_schema_is_up_to_date(tmp_path: Path) -> None:
    """The frontend generates its client from the committed file; regenerate it with
    ``uv run python -m scadbuddy.tools.export_openapi``."""
    fresh = export(tmp_path / "openapi.json").read_text(encoding="utf-8")
    assert DEFAULT_OUTPUT.read_text(encoding="utf-8") == fresh


def test_the_pasted_source_body_is_a_named_schema(tmp_path: Path) -> None:
    """Named, so the frontend's `PastedSource` is generated rather than hand-written."""
    schema = json.loads(export(tmp_path / "openapi.json").read_text(encoding="utf-8"))
    body = schema["paths"]["/api/v1/models"]["post"]["requestBody"]["content"]
    assert body["application/json"]["schema"] == {"$ref": "#/components/schemas/PastedSource"}
    assert schema["components"]["schemas"]["PastedSource"]["required"] == ["name", "source"]


def test_the_new_model_file_routes_document_only_what_they_answer(tmp_path: Path) -> None:
    """#179: a response class with a media type of its own adds it to the documented
    200 beside the declared one, so a route could claim a type it never sends."""
    paths = json.loads(export(tmp_path / "openapi.json").read_text(encoding="utf-8"))["paths"]

    def success_types(path: str, method: str) -> set[str]:
        return set(paths[path][method]["responses"]["200"]["content"])

    assert success_types("/api/v1/models/{slug}/readme", "get") == {"text/markdown"}
    assert success_types("/api/v1/models/{slug}/thumbnail", "get") == {"image/png"}
    assert success_types("/api/v1/models/{slug}/source", "get") == {"text/plain"}
    for path in ("/api/v1/models/{slug}/readme", "/api/v1/models/{slug}/thumbnail"):
        for method in ("put", "delete"):
            assert success_types(path, method) == {"application/json"}, (method, path)
