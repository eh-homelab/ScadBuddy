"""`POST /api/v1/lsp/diagnostics` (#252), against a stand-in that behaves as the pinned
openscad-lsp 2.0.1 was measured to: nothing on didOpen, diagnostics on didChange. One
test (`requires_openscad_lsp`) checks those measurements against the real binary."""

from __future__ import annotations

import asyncio
import os
import shutil
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from scadbuddy.core.paths import DataPaths
from scadbuddy.core.settings import Settings
from scadbuddy.library import lsp_diagnostics
from scadbuddy.main import create_app

# Publishes one diagnostic per line containing ERR (0-based LSP positions), plus one
# naming its cwd for a line containing CWD; HANG never publishes; CRASH exits.
FAKE_LSP = """#!/usr/bin/env python3
import json, os, pathlib, sys, time

stdin, stdout = sys.stdin.buffer, sys.stdout.buffer

def send(message):
    body = json.dumps(message).encode()
    stdout.write(b"Content-Length: %d\\r\\n\\r\\n" % len(body) + body)
    stdout.flush()

while True:
    length = None
    while True:
        line = stdin.readline()
        if not line:
            raise SystemExit(0)
        if line == b"\\r\\n":
            break
        name, _, value = line.partition(b":")
        if name.strip().lower() == b"content-length":
            length = int(value)
    message = json.loads(stdin.read(length))
    method = message.get("method")
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": message["id"], "result": {"capabilities": {}}})
    elif method == "textDocument/didChange":
        text = message["params"]["contentChanges"][0]["text"]
        if "HANG" in text:
            time.sleep(60)
        if "CRASH" in text:
            raise SystemExit(3)
        diags = []
        for number, line in enumerate(text.splitlines()):
            if "ERR" in line:
                start = line.index("ERR")
                diags.append({"range": {"start": {"line": number, "character": start},
                                        "end": {"line": number, "character": start + 3}},
                              "severity": 1, "message": "syntax error"})
            if "CWD" in line:
                diags.append({"range": {"start": {"line": number, "character": 0},
                                        "end": {"line": number, "character": 1}},
                              "severity": 2, "message": os.getcwd()})
        # Someone else's document first: the route must wait for its own.
        send({"jsonrpc": "2.0", "method": "textDocument/publishDiagnostics",
              "params": {"uri": "file:///elsewhere.scad", "diagnostics": diags}})
        send({"jsonrpc": "2.0", "method": "textDocument/publishDiagnostics",
              "params": {"uri": message["params"]["textDocument"]["uri"], "diagnostics": diags}})
"""


@pytest.fixture
def settings(settings: Settings, tmp_path: Path) -> Settings:
    binary = tmp_path / "fake-diagnostics-lsp"
    binary.write_text(FAKE_LSP, encoding="utf-8")
    binary.chmod(0o755)
    return settings.model_copy(update={"openscad_lsp": str(binary)})


def post(client: TestClient, source: str, slug: str | None = None) -> dict[str, object]:
    response = client.post("/api/v1/lsp/diagnostics", json={"source": source, "slug": slug})
    assert response.status_code == 200, response.text
    body: dict[str, object] = response.json()
    return body


def test_diagnostics_come_back_one_based_and_in_order(client: TestClient) -> None:
    body = post(client, "cube(1);\n  ERR here\nERR\n")
    assert body == {
        "available": True,
        "diagnostics": [
            {
                "line": 2,
                "column": 3,
                "end_line": 2,
                "end_column": 6,
                "severity": "error",
                "message": "syntax error",
            },
            {
                "line": 3,
                "column": 1,
                "end_line": 3,
                "end_column": 4,
                "severity": "error",
                "message": "syntax error",
            },
        ],
    }


def test_clean_source_has_none(client: TestClient) -> None:
    assert post(client, "cube(1);\n") == {"available": True, "diagnostics": []}


def test_a_slug_runs_it_in_the_models_directory(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    body = post(client, "CWD\n", slug=model)
    diagnostics = body["diagnostics"]
    assert isinstance(diagnostics, list)
    assert diagnostics[0]["message"] == str(paths.model_dir(model))
    assert diagnostics[0]["severity"] == "warning"


def test_an_unknown_slug_is_a_404(client: TestClient) -> None:
    response = client.post("/api/v1/lsp/diagnostics", json={"source": "x", "slug": "nope"})
    assert response.status_code == 404


def test_no_language_server_is_reported_not_raised(
    client: TestClient, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    which = shutil.which
    monkeypatch.setattr(
        shutil,
        "which",
        lambda name, *a, **k: None if name == settings.openscad_lsp else which(name, *a, **k),
    )
    response = client.post("/api/v1/lsp/diagnostics", json={"source": "ERR"})
    assert response.json() == {"available": False, "diagnostics": []}


def test_a_server_that_never_publishes_is_a_503(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(lsp_diagnostics, "DIAGNOSTICS_TIMEOUT", 0.5)
    response = client.post("/api/v1/lsp/diagnostics", json={"source": "HANG"})
    assert response.status_code == 503
    assert "published nothing" in response.json()["detail"]


def test_a_server_that_exits_is_a_503(client: TestClient) -> None:
    response = client.post("/api/v1/lsp/diagnostics", json={"source": "CRASH"})
    assert response.status_code == 503
    assert "openscad-lsp failed" in response.json()["detail"]


def test_a_full_budget_is_a_503_with_retry_after(settings: Settings) -> None:
    """It shares the editor's `SCADBUDDY_LSP_SESSIONS`: an open editor holds the one permit."""
    app: FastAPI = create_app(settings.model_copy(update={"lsp_sessions": 1}))
    with TestClient(app) as client, client.websocket_connect("/api/v1/lsp"):
        response = client.post("/api/v1/lsp/diagnostics", json={"source": "x"})
    assert response.status_code == 503
    assert response.headers["Retry-After"] == "2"


def test_requests_at_once_on_one_permit_fail_fast_rather_than_wait(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #750: the budget check and the acquire cannot be split by another
    request, so of several at once exactly one gets the server and the rest get
    the busy 503 straight away, not after the first one's timeout."""
    monkeypatch.setattr(lsp_diagnostics, "DIAGNOSTICS_TIMEOUT", 2.0)
    app: FastAPI = create_app(settings.model_copy(update={"lsp_sessions": 1}))
    start = threading.Barrier(4)
    answers: list[tuple[str, float]] = []

    def post_hang(client: TestClient) -> None:
        start.wait()
        began = time.monotonic()
        response = client.post("/api/v1/lsp/diagnostics", json={"source": "HANG"})
        assert response.status_code == 503
        answers.append((response.json()["detail"], time.monotonic() - began))

    with TestClient(app) as client:
        threads = [threading.Thread(target=post_hang, args=(client,)) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    busy = [elapsed for detail, elapsed in answers if "in use" in detail]
    assert len(answers) == 4
    assert len(busy) == 3
    assert max(busy) < 1.5


async def test_a_cancelled_request_leaves_no_server_behind(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #750: cancelled mid-exchange (a client gone, a server shutting
    down), the shielded cleanup still kills and reaps the child."""
    spawned: list[asyncio.subprocess.Process] = []
    spawn = asyncio.create_subprocess_exec

    async def recording(*args: Any, **kwargs: Any) -> asyncio.subprocess.Process:
        process = await spawn(*args, **kwargs)
        spawned.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", recording)
    call = asyncio.create_task(
        lsp_diagnostics.lsp_diagnostics(
            settings.openscad_lsp, tmp_path, "HANG", env={"PATH": os.environ["PATH"]}
        )
    )
    for _ in range(100):
        if spawned:
            break
        await asyncio.sleep(0.05)
    await asyncio.sleep(0.2)  # into the exchange: the fake is sleeping on HANG
    call.cancel()
    with pytest.raises(asyncio.CancelledError):
        await call
    [process] = spawned
    assert process.returncode is not None


@pytest.fixture
def real_client(settings: Settings) -> Iterator[TestClient]:
    # The real openscad-lsp, not this module's fake.
    app = create_app(
        settings.model_copy(update={"openscad_lsp": Settings.model_fields["openscad_lsp"].default})
    )
    with TestClient(app) as client:
        yield client


@pytest.mark.requires_openscad_lsp
def test_the_real_server_reports_a_syntax_error_and_a_missing_include(
    real_client: TestClient, model: str
) -> None:
    """The measurements the module docstring rests on, against the pinned binary: it
    publishes on didChange, a parse error is `syntax error` with a range, and a leading
    `include` of a missing file is `file not found!`. A bump that changes any of them
    fails here, not only in the fake."""
    assert post(real_client, "cube(1);\n", slug=model) == {"available": True, "diagnostics": []}

    broken = post(real_client, "cube(1);\ncube([1, 2;\n", slug=model)["diagnostics"]
    assert isinstance(broken, list) and broken
    assert all(d["severity"] == "error" for d in broken)
    assert any(d["line"] == 2 for d in broken), broken

    missing = post(real_client, "include <missing.scad>\ncube(1);\n", slug=model)["diagnostics"]
    assert isinstance(missing, list)
    assert any(d["message"] == "file not found!" and d["line"] == 1 for d in missing), missing
