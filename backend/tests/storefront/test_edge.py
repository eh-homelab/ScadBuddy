"""The storefront edge process (#5282, spec 2026-11-02 §5.1, §10): it holds no database."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any, NoReturn

import psycopg
import psycopg_pool
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from scadbuddy.storefront.__main__ import main
from scadbuddy.storefront.app import create_app
from scadbuddy.storefront.settings import (
    FORBIDDEN_ENV,
    EdgeRefusal,
    StorefrontSettings,
    check_environment,
)

BACKEND = Path(__file__).resolve().parents[2]


def load(**overrides: object) -> StorefrontSettings:
    """The settings from the environment, the way the process reads them."""
    return StorefrontSettings(**overrides)  # type: ignore[arg-type]


@pytest.fixture
def edge_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bootstrap = tmp_path / "bootstrap"
    bootstrap.write_text("proof")
    cookie_key = tmp_path / "cookie.key"
    cookie_key.write_bytes(os.urandom(32))
    for name in FORBIDDEN_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("SCADBUDDY_SERVICE_TOKEN_FILE", raising=False)
    monkeypatch.setenv("SCADBUDDY_API_INTERNAL_URL", "http://api.internal:8000")
    monkeypatch.setenv("SCADBUDDY_SERVICE_BOOTSTRAP_FILE", str(bootstrap))
    monkeypatch.setenv("SCADBUDDY_STOREFRONT_COOKIE_KEY_FILE", str(cookie_key))
    monkeypatch.setenv("SCADBUDDY_STOREFRONT_PUBLIC_URL", "https://shop.example.com")


@pytest.fixture
def edge_app(edge_env: None) -> FastAPI:
    return create_app(load())


def route_paths(app: FastAPI) -> list[str]:
    """Every path the app serves, looking inside an included router (FastAPI 0.162)."""
    paths: list[str] = []
    routes: list[Any] = list(app.routes)
    for route in routes:
        if hasattr(route, "original_router"):
            prefix = route.include_context.prefix
            paths += [prefix + r.path for r in route.original_router.routes]
        else:
            paths.append(route.path)
    return paths


def test_no_back_office_route_is_mounted(edge_app: FastAPI) -> None:
    paths = route_paths(edge_app)
    assert "/healthz" in paths
    assert [p for p in paths if p.startswith("/api/") and not p.startswith("/api/store/v1")] == []
    with TestClient(edge_app) as c:
        assert c.get("/api/v1/orders").status_code == 404
        assert c.get("/healthz").json() == {"status": "ok"}


@pytest.mark.parametrize("name", FORBIDDEN_ENV)
def test_a_database_or_key_variable_refuses_the_start(name: str) -> None:
    with pytest.raises(EdgeRefusal) as refused:
        check_environment({name: "x"})
    assert refused.value.reasons == [
        f"{name} is set; the storefront holds no database secret or key"
    ]


def test_a_clean_environment_is_accepted() -> None:
    check_environment({"PATH": "/usr/bin"})


def test_main_exits_2_with_a_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCADBUDDY_DATABASE_URL", "postgresql://x")
    assert main([]) == 2


def test_the_edge_starts_and_opens_no_database_connection(
    edge_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*_a: object, **_k: object) -> NoReturn:
        raise AssertionError("the storefront opened a database connection")

    monkeypatch.setattr(psycopg.Connection, "connect", refuse)
    monkeypatch.setattr(psycopg_pool.ConnectionPool, "__init__", refuse)
    with TestClient(create_app(load())) as c:
        assert c.get("/healthz").status_code == 200


def test_the_edge_imports_nothing_that_touches_the_database() -> None:
    out = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            "import scadbuddy.storefront.app, sys;"
            " forbidden = {'psycopg_pool', 'scadbuddy.orders', 'scadbuddy.render',"
            " 'scadbuddy.workflows', 'scadbuddy.store', 'scadbuddy.library', 'scadbuddy.main'};"
            " print(sorted({'.'.join(m.split('.')[:2]) for m in sys.modules}"
            " & forbidden | {m for m in sys.modules if m == 'psycopg_pool'}))",
        ],
        capture_output=True,
        text=True,
        check=True,
        cwd=BACKEND,
    )
    assert out.stdout.strip() == "[]"


def test_a_short_cookie_key_is_refused(edge_env: None, tmp_path: Path) -> None:
    short = tmp_path / "short.key"
    short.write_bytes(os.urandom(31))
    with pytest.raises(ValidationError, match="at least 32 bytes"):
        load(storefront_cookie_key_file=short)


@pytest.mark.parametrize(
    "url", ["http://shop.example.com", "https://shop.example.com/shop", "shop.example.com"]
)
def test_the_public_url_is_an_https_origin(edge_env: None, url: str) -> None:
    with pytest.raises(ValidationError, match="STOREFRONT_PUBLIC_URL"):
        load(storefront_public_url=url)


def test_exactly_one_credential_proof_is_required(
    edge_env: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proof = tmp_path / "proof"
    proof.write_text("p")
    with pytest.raises(ValidationError, match="exactly one"):
        load(service_token_file=proof)
    monkeypatch.delenv("SCADBUDDY_SERVICE_BOOTSTRAP_FILE")
    with pytest.raises(ValidationError, match="exactly one"):
        load()
    assert load(service_token_file=proof).service_token_file == proof


def test_the_trusted_proxies_are_cidrs(edge_env: None) -> None:
    with pytest.raises(ValidationError, match="TRUSTED_PROXIES"):
        load(trusted_proxies="nonsense")
