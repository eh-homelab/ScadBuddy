"""A page on another origin cannot change anything through a LAN user's browser (#962).

A ``<form>`` POST (``text/plain``, urlencoded or multipart, or no body at all) needs no
CORS preflight, so the route runs before the browser ever looks at the response. The
``Origin`` the browser always sends on such a request is what tells it apart: the same
`origin_allowed` rule the realtime socket uses. No ``Origin`` (curl, the agent's
server-side calls) is not a browser page, and passes.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from scadbuddy.api.deps import STATE_ATTR

SOURCE = "cube(6);"
FOREIGN = ["https://evil.example", "http://scad.example.com", "null", "not a url"]


def _paste(client: TestClient, name: str, headers: dict[str, str]) -> int:
    response = client.post(
        "/api/v1/models",
        content=SOURCE.encode(),
        headers={"Content-Type": "text/plain", "X-Model-Name": name, **headers},
    )
    status: int = response.status_code
    return status


@pytest.fixture
def public(client: TestClient) -> TestClient:
    response = client.put("/api/v1/settings", json={"public_url": "https://scad.example.com/"})
    assert response.status_code == 200, response.text
    return client


@pytest.mark.parametrize("origin", FOREIGN)
def test_a_foreign_origin_cannot_create_a_model(public: TestClient, origin: str) -> None:
    response = public.post(
        "/api/v1/models",
        content=SOURCE.encode(),
        headers={
            "Content-Type": "text/plain",
            "X-Model-Name": "csrf",
            "Origin": origin,
            "Sec-Fetch-Site": "cross-site",
        },
    )
    assert response.status_code == 403, response.text
    assert response.headers["content-type"].startswith("application/problem+json")
    assert public.get("/api/v1/models/csrf").status_code == 404


def test_a_foreign_multipart_form_is_refused(public: TestClient) -> None:
    response = public.post(
        "/api/v1/models",
        files={"file": ("csrf.scad", SOURCE.encode(), "application/octet-stream")},
        headers={"Origin": "https://evil.example"},
    )
    assert response.status_code == 403, response.text
    assert public.get("/api/v1/models/csrf").status_code == 404


def test_a_foreign_bodyless_post_is_refused(public: TestClient) -> None:
    assert _paste(public, "Kept", {}) == 201
    response = public.post(
        "/api/v1/models/kept/upstream/detach",
        headers={
            "Origin": "https://evil.example",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    assert response.status_code == 403, response.text


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("PUT", "/api/v1/models/kept/readme"),
        ("PATCH", "/api/v1/models/kept"),
        ("DELETE", "/api/v1/models/kept"),
    ],
)
def test_every_unsafe_method_is_checked(public: TestClient, method: str, path: str) -> None:
    assert _paste(public, "Kept", {}) == 201
    response = public.request(method, path, headers={"Origin": "https://evil.example"})
    assert response.status_code == 403, response.text
    assert public.get("/api/v1/models/kept").status_code == 200


def test_reads_are_not_checked(public: TestClient) -> None:
    assert (
        public.get("/api/v1/models", headers={"Origin": "https://evil.example"}).status_code == 200
    )


def test_no_origin_passes(public: TestClient) -> None:
    """curl and the agent's server-side calls send none."""
    assert _paste(public, "Plain", {}) == 201


@pytest.mark.parametrize(
    "origin",
    ["https://scad.example.com", "https://scad.example.com:443", "http://localhost:5173"],
)
def test_the_ui_own_origins_pass(public: TestClient, origin: str) -> None:
    assert _paste(public, "Mine", {"Origin": origin, "Sec-Fetch-Site": "same-origin"}) == 201


def test_an_allowed_origin_passes(app: FastAPI, public: TestClient) -> None:
    state = getattr(app.state, STATE_ATTR)
    state.settings = state.settings.model_copy(
        update={"allowed_origins": "https://scad.internal.example"}
    )
    assert _paste(public, "Lan", {"Origin": "https://scad.internal.example"}) == 201
    assert _paste(public, "Other", {"Origin": "https://scad.lan"}) == 403


def test_without_a_public_url_only_loopback_passes(client: TestClient) -> None:
    assert _paste(client, "Loop", {"Origin": "http://127.0.0.1:8080"}) == 201
    assert _paste(client, "Far", {"Origin": "https://scad.example.com"}) == 403
