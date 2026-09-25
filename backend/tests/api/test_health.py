from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from scadbuddy.core.settings import Settings
from scadbuddy.main import create_app


def test_healthz_reports_openscad_and_a_writable_data_dir(client: TestClient) -> None:
    body = client.get("/healthz").json()
    assert body == {
        "status": "ok",
        "openscad_version": "OpenSCAD version 2099.01.01",
        "data_dir_writable": True,
        "revision": "unknown",
        "version": "dev",
    }


def test_healthz_is_degraded_without_openscad(data_dir: Path, seed_dir: Path) -> None:
    settings = Settings(
        openscad="definitely-not-installed",
        data_dir=data_dir,
        seed_models_dir=seed_dir,
        frontend_dir=Path("/nonexistent"),
    )
    with TestClient(create_app(settings)) as client:
        body = client.get("/healthz").json()
    assert body["status"] == "degraded"
    assert body["openscad_version"] is None
    assert body["data_dir_writable"] is True


def test_startup_seeds_bundled_models(settings: Settings, seed_dir: Path, data_dir: Path) -> None:
    bundled = seed_dir / "seeded-model"
    bundled.mkdir()
    (bundled / "model.scad").write_text("cube(1);\n", encoding="utf-8")
    (bundled / "model.json").write_text(json.dumps({"name": "Seeded"}), encoding="utf-8")
    (bundled / ".gitignore").write_text("ignored\n", encoding="utf-8")

    with TestClient(create_app(settings)) as client:
        body = client.get("/api/v1/models").json()

    assert [row["slug"] for row in body] == ["seeded-model"]
    assert body[0]["name"] == "Seeded"
    # Dotfiles are repo furniture, not model content.
    assert not (data_dir / "models" / "seeded-model" / ".gitignore").exists()


def test_seeding_never_overwrites_an_existing_model(
    settings: Settings, seed_dir: Path, model: str, data_dir: Path
) -> None:
    bundled = seed_dir / model
    bundled.mkdir()
    (bundled / "model.scad").write_text("// from the seed dir\n", encoding="utf-8")

    with TestClient(create_app(settings)):
        pass

    assert "from the seed dir" not in (data_dir / "models" / model / "model.scad").read_text(
        encoding="utf-8"
    )


def test_an_explicit_directory_must_exist_to_be_used(tmp_path: Path) -> None:
    assert Settings(seed_models_dir=tmp_path).resolve_seed_models_dir() == tmp_path
    assert Settings(seed_models_dir=tmp_path / "gone").resolve_seed_models_dir() is None
    assert Settings(frontend_dir=tmp_path).resolve_frontend_dir() == tmp_path
    assert Settings(frontend_dir=tmp_path / "gone").resolve_frontend_dir() is None


def test_the_environment_overrides_every_field(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCADBUDDY_DATA_DIR", "/srv/scad")
    monkeypatch.setenv("SCADBUDDY_RENDER_TIMEOUT", "7.5")
    monkeypatch.setenv("SCADBUDDY_RENDER_CONCURRENCY", "4")
    monkeypatch.setenv("SCADBUDDY_BAMBUDDY_URL", "https://bambuddy.test")
    monkeypatch.setenv("SCADBUDDY_BAMBUDDY_API_KEY", "k")
    monkeypatch.setenv("SCADBUDDY_REVISION", "0123456789abcdef0123456789abcdef01234567")
    monkeypatch.setenv("SCADBUDDY_VERSION", "1.2.3")

    config = Settings().to_config()
    assert config.data_dir == Path("/srv/scad")
    assert config.render_timeout == 7.5
    assert config.render_concurrency == 4
    assert Settings().bambuddy_url == "https://bambuddy.test"
    assert Settings().revision == "0123456789abcdef0123456789abcdef01234567"
    assert Settings().version == "1.2.3"
