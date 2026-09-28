"""The backend's origin serves no open redirect (#349).

The agent's headless browser may only open the backend's own origin, and the
origin allow-list of the pinned ``@playwright/mcp`` does not apply to redirects
(measured: a same-origin URL that redirects elsewhere reaches the other origin,
agent-actor marker included; ``docs/ai/headless-browser.md``). So nothing the
backend serves may redirect off its origin. Two checks:

- at runtime, the app (with a built SPA mounted) answers paths shaped to provoke a
  redirect, and every ``Location`` it sends resolves to the request's own origin;
- in the source, no module builds a redirect by hand, so a new one is reviewed here
  first (add it to :data:`REVIEWED` with the reason it cannot leave the origin).
"""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import pytest
from fastapi.testclient import TestClient

from scadbuddy.core.settings import Settings
from scadbuddy.main import create_app

ORIGIN = "https://scadbuddy.example"

#: Paths that try to turn a trailing-slash or directory redirect into an off-origin one.
PROBES = [
    "/api/v1/models/",
    "/api/v1/settings/",
    "//evil.example/",
    "//evil.example/api/v1/models/",
    "/\\evil.example/",
    "/%2F%2Fevil.example/",
    "/%5Cevil.example/",
    "/assets",
    "/assets/",
    "//evil.example/assets",
    "/https://evil.example/",
    "/api/v1/models/..%2F..%2F/",
]


@pytest.fixture
def spa_client(settings: Settings, tmp_path: Path) -> TestClient:
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>ScadBuddy</title>", encoding="utf-8")
    (dist / "assets" / "index.html").write_text("asset dir", encoding="utf-8")
    app = create_app(settings.model_copy(update={"frontend_dir": dist}))
    return TestClient(app, base_url=ORIGIN)


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


@pytest.mark.parametrize("path", PROBES)
@pytest.mark.parametrize("method", ["GET", "HEAD", "POST"])
def test_no_redirect_leaves_the_origin(spa_client: TestClient, method: str, path: str) -> None:
    response = spa_client.request(method, path, follow_redirects=False)
    location = response.headers.get("location")
    if location is None:
        return
    target = urljoin(f"{ORIGIN}{path}", location)
    assert _origin(target) == ORIGIN, f"{method} {path} -> {response.status_code} {location}"


#: Modules allowed to build a redirect, each with why it stays on the origin. Empty today.
REVIEWED: dict[str, str] = {}

REDIRECT = re.compile(
    r"RedirectResponse|status_code\s*=\s*30[1278]|[\"']Location[\"']\s*:|headers\[[\"']location[\"']\]\s*="
)


def test_no_module_builds_a_redirect_without_review() -> None:
    root = Path(__file__).resolve().parents[2] / "scadbuddy"
    found = sorted(
        str(path.relative_to(root))
        for path in root.rglob("*.py")
        if REDIRECT.search(path.read_text(encoding="utf-8"))
    )
    assert [f for f in found if f not in REVIEWED] == [], (
        "a module builds an HTTP redirect; the headless browser follows redirects off the "
        "origin (#349), so review it and add it to REVIEWED"
    )


def test_the_probes_do_reach_the_redirecting_code(spa_client: TestClient) -> None:
    """Not vacuous: some probes are redirected (trailing slash, directory), on the origin."""
    statuses = {p: spa_client.get(p, follow_redirects=False).status_code for p in PROBES}
    assert any(300 <= s < 400 for s in statuses.values()), statuses
