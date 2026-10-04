"""Installing a font as a ``library`` operation (#1054)."""

from __future__ import annotations

import asyncio
import threading
import uuid
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from scadbuddy.api.deps import STATE_ATTR
from scadbuddy.library.googlefonts import FamilyFiles
from tests.api.test_fonts import FakeBackedService, FakeClient
from tests.api.test_model_operations import _workflow_ids

pytestmark = [pytest.mark.requires_postgres]

INSTALL = "/api/v1/fonts/install"


@pytest.fixture
def fonts(app: FastAPI, data_dir: Path) -> FakeBackedService:
    """`test_fonts.py`'s service, stubbed underneath, as the operation's run reads it."""
    service = FakeBackedService(data_dir, client=FakeClient())
    getattr(app.state, STATE_ATTR).fonts = service
    return service


def test_font_install_is_an_operation(
    client: TestClient, app: FastAPI, fonts: FakeBackedService
) -> None:
    response = client.post(INSTALL, json={"family": "Pacifico"})
    assert response.status_code == 200, response.text
    assert response.json()["family"] == "Pacifico"
    assert _workflow_ids(app, "font_install")


def test_two_installs_of_one_family_with_one_key_download_once(
    client: TestClient, app: FastAPI, fonts: FakeBackedService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review focus 3: a second press of the same install, while the first downloads,
    joins it."""
    fetches: list[str] = []
    fetch = fonts.client.fetch_family_files

    async def slowly(family: str) -> FamilyFiles:
        fetches.append(family)
        await asyncio.sleep(2)
        return await fetch(family)

    monkeypatch.setattr(fonts.client, "fetch_family_files", slowly)
    key = uuid.uuid4().hex
    answers: list[Any] = []

    def install() -> None:
        answers.append(
            client.post(INSTALL, json={"family": "Pacifico"}, headers={"Idempotency-Key": key})
        )

    threads = [threading.Thread(target=install) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert [answer.status_code for answer in answers] == [200, 200], [a.text for a in answers]
    assert answers[0].json() == answers[1].json()
    assert fetches == ["Pacifico"]
    assert len(_workflow_ids(app, "font_install")) == 1


def test_a_family_not_in_the_catalogue_is_still_404(
    client: TestClient, app: FastAPI, fonts: FakeBackedService
) -> None:
    response = client.post(INSTALL, json={"family": "Comic Sans MS"})
    assert response.status_code == 404, response.text
    assert "not in the Google Fonts catalogue" in response.json()["detail"]
