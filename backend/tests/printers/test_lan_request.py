"""`LanPrinter.request` around its MQTT client (#7276 P30): connect, subscribe and publish
share one `MQTT_TIMEOUT`, a failure before the publish is raised, and after it silence, a
drop or the caller's deadline is no reply (``None``). The client is a stand-in; the wire
test is Task 3's, against the fake printer (P28)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import aiomqtt
import pytest

from scadbuddy.printers import lan
from scadbuddy.printers.lan import LanPrinter

SERIAL = "01P00A000000001"
NAME = "extrusion_cali_set"
SEQ = "77"
COMMAND = {"print": {"command": NAME, "sequence_id": SEQ}}


@dataclass
class Message:
    payload: bytes


class FakeClient:
    def __init__(
        self,
        *,
        hang: bool = False,
        publish_fails: BaseException | None = None,
        replies: list[dict[str, Any]] | None = None,
        drop: bool = False,
    ) -> None:
        self.hang = hang
        self.publish_fails = publish_fails
        self.replies = replies or []
        self.drop = drop
        self.published: list[dict[str, Any]] = []

    async def __aenter__(self) -> FakeClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        if self.drop:
            raise aiomqtt.MqttError("the connection was lost")

    async def subscribe(self, topic: str) -> None:
        if self.hang:
            await asyncio.Event().wait()

    async def publish(self, topic: str, payload: str) -> None:
        if self.publish_fails is not None:
            raise self.publish_fails
        self.published.append(json.loads(payload))

    @property
    def messages(self) -> AsyncIterator[Message]:
        return self._messages()

    async def _messages(self) -> AsyncIterator[Message]:
        for reply in self.replies:
            yield Message(json.dumps({"print": reply}).encode())
        if self.drop:
            raise aiomqtt.MqttError("the connection was lost")
        await asyncio.Event().wait()


def _printer(client: FakeClient, monkeypatch: pytest.MonkeyPatch) -> LanPrinter:
    printer = LanPrinter("10.0.0.9", SERIAL, "12345678", "ab" * 32)

    async def made() -> FakeClient:
        return client

    monkeypatch.setattr(printer, "_client", made)
    return printer


async def _request(printer: LanPrinter, *, wait: float = 5.0, until: float | None = None) -> Any:
    return await printer.request(COMMAND, name=NAME, seq=SEQ, wait=wait, until=until)


async def test_the_matching_reply_is_answered(monkeypatch: pytest.MonkeyPatch) -> None:
    ours = {"command": NAME, "sequence_id": SEQ, "result": "success"}
    client = FakeClient(replies=[{"command": NAME, "sequence_id": "1"}, ours])
    assert await _request(_printer(client, monkeypatch)) == ours
    assert client.published == [COMMAND]


async def test_connect_subscribe_and_publish_share_one_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(lan, "MQTT_TIMEOUT", 0.05)
    client = FakeClient(hang=True)
    with pytest.raises(TimeoutError):
        await _request(_printer(client, monkeypatch))
    assert client.published == []


async def test_a_failure_before_the_publish_is_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    client = FakeClient(publish_fails=aiomqtt.MqttError("not connected"))
    with pytest.raises(aiomqtt.MqttError):
        await _request(_printer(client, monkeypatch))


async def test_a_drop_after_the_publish_is_no_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    client = FakeClient(drop=True)
    assert await _request(_printer(client, monkeypatch)) is None
    assert client.published == [COMMAND]


async def test_a_reply_before_a_drop_is_kept(monkeypatch: pytest.MonkeyPatch) -> None:
    ours = {"command": NAME, "sequence_id": SEQ}
    client = FakeClient(replies=[ours], drop=True)
    assert await _request(_printer(client, monkeypatch)) == ours


async def test_the_deadline_ends_the_wait_before_its_seconds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeClient()
    loop = asyncio.get_running_loop()
    start = loop.time()
    assert await _request(_printer(client, monkeypatch), wait=30.0, until=start + 0.05) is None
    assert loop.time() - start < 5.0
