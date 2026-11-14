"""The intake worker's activities against a fake CRUD service and bucket (#6133, spec
2026-11-13 §7): the read, the download, the child's verdict, the preview's PUT and the
verdict's post."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from temporalio.testing import ActivityEnvironment

from scadbuddy.auth.service_auth import ServiceAuth
from scadbuddy.crud.client import CrudClient
from scadbuddy.intake_worker.activities import BASE, IntakeActivities
from tests.crud.test_client import Source
from tests.intake_worker.test_check import box_stl

HEADER = {"X-ScadBuddy-Crud": "1"}
SOURCE = "0f0e0d0c-0000-4000-8000-000000000001"
GET_URL = "https://bucket.example/shop/staging/abc?X-Amz-Signature=s"
PUT_URL = "https://bucket.example/shop/staging/abc.png?X-Amz-Signature=s"


class Fake:
    """The CRUD service's intake routes and the bucket, answering from what they hold."""

    def __init__(self, data: bytes, *, declared: int | None = None, status: int = 200) -> None:
        self.data = data
        self.declared = len(data) if declared is None else declared
        self.read_status = status
        self.posted: list[tuple[dict[str, Any], str | None]] = []
        self.preview: bytes | None = None
        self.preview_headers: dict[str, str] = {}

    def crud(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == f"{BASE}/{SOURCE}/read":
            if self.read_status != 200:
                problem = {"type": "x/not_checking", "status": self.read_status, "detail": "d"}
                return httpx.Response(self.read_status, json=problem, headers=HEADER)
            body = {
                "source_id": SOURCE,
                "filename": "box.stl",
                "size": self.declared,
                "media_type": "model/stl",
                "url": GET_URL,
            }
            return httpx.Response(200, json=body, headers=HEADER)
        if path == f"{BASE}/{SOURCE}/preview":
            headers = {"Content-Type": "image/png", "x-amz-acl": "private"}
            return httpx.Response(200, json={"url": PUT_URL, "headers": headers}, headers=HEADER)
        if path == f"{BASE}/{SOURCE}/check":
            self.posted.append(
                (json.loads(request.content), request.headers.get("Idempotency-Key"))
            )
            return httpx.Response(200, json={"recorded": True}, headers=HEADER)
        return httpx.Response(404)

    def bucket(self, request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, content=self.data)
        self.preview = request.content
        self.preview_headers = dict(request.headers)
        return httpx.Response(200)


def run(fake: Fake, tmp_path: Path, which: str = "check") -> str:
    crud = CrudClient(
        "http://api",
        "intake",
        ServiceAuth(Source()),
        transport=httpx.MockTransport(fake.crud),
        unavailable_budget=0.2,
        backoff=0.01,
    )
    http = httpx.AsyncClient(transport=httpx.MockTransport(fake.bucket))
    activities = IntakeActivities(crud, tmp_path, http)
    env = ActivityEnvironment()

    async def go() -> str:
        try:
            method = activities.check if which == "check" else activities.failed
            result: str = await env.run(method, SOURCE)
            return result
        finally:
            await activities.aclose()
            await crud.aclose()

    return asyncio.run(go())


def test_a_ready_file_puts_its_preview_and_posts_its_facts(tmp_path: Path) -> None:
    fake = Fake(box_stl())
    assert run(fake, tmp_path) == "ready"
    [(posted, key)] = fake.posted
    assert posted["outcome"] == "ready" and posted["facts"]["triangles"] == 12
    assert len(posted["sha256"]) == 64 and key
    assert fake.preview is not None and fake.preview.startswith(b"\x89PNG")
    assert fake.preview_headers["x-amz-acl"] == "private"
    assert list(tmp_path.iterdir()) == []  # the scratch directory is gone


def test_a_file_not_its_declared_size_is_refused(tmp_path: Path) -> None:
    fake = Fake(box_stl(), declared=100)
    assert run(fake, tmp_path) == "refused"
    [(posted, _)] = fake.posted
    assert posted == {
        "outcome": "refused",
        "reason": "The file is not the size it was sent as.",
        "sha256": None,
        "facts": None,
    }
    assert fake.preview is None


def test_a_file_that_is_not_a_model_is_refused(tmp_path: Path) -> None:
    fake = Fake(b"just some text that is not solid")
    assert run(fake, tmp_path) == "refused"
    assert fake.posted[0][0]["reason"] == "The file is not a 3MF or an STL."


@pytest.mark.parametrize("status", [404, 409])
def test_a_source_that_no_longer_waits_is_skipped(tmp_path: Path, status: int) -> None:
    fake = Fake(box_stl(), status=status)
    assert run(fake, tmp_path) == "skipped"
    assert fake.posted == []


def test_failed_posts_failed(tmp_path: Path) -> None:
    fake = Fake(b"")
    assert run(fake, tmp_path, "failed") == "failed"
    assert fake.posted[0][0]["outcome"] == "failed"
