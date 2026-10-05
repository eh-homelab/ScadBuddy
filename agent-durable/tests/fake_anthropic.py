"""The fake Anthropic endpoint (agent/test/support/fakeAnthropicServer.ts) as a pytest
fixture: `fake_anthropic(script)` starts it with that list of replies and returns it."""

from __future__ import annotations

import json
import shutil
import subprocess
import urllib.request
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

SERVER = Path(__file__).parents[2] / "agent" / "test" / "support" / "fakeAnthropicServer.ts"


@dataclass
class FakeAnthropic:
    url: str

    def requests(self) -> list[dict[str, Any]]:
        with urllib.request.urlopen(f"{self.url}/__requests", timeout=10) as resp:
            out: list[dict[str, Any]] = json.load(resp)
        return out

    def message_calls(self) -> list[dict[str, Any]]:
        return [r for r in self.requests() if r["method"] == "POST" and r["path"] == "/v1/messages"]


@pytest.fixture
def fake_anthropic(tmp_path: Path) -> Iterator[Callable[[list[dict[str, Any]]], FakeAnthropic]]:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not on PATH (the fake Anthropic endpoint)")
    procs: list[subprocess.Popen[str]] = []

    def start(script: list[dict[str, Any]]) -> FakeAnthropic:
        path = tmp_path / f"fake-script-{len(procs)}.json"
        path.write_text(json.dumps(script))
        proc = subprocess.Popen([node, str(SERVER), str(path)], stdout=subprocess.PIPE, text=True)
        procs.append(proc)
        assert proc.stdout is not None
        line = proc.stdout.readline()
        if not line:
            raise RuntimeError(f"the fake Anthropic endpoint exited ({proc.wait()})")
        return FakeAnthropic(json.loads(line)["url"])

    try:
        yield start
    finally:
        for proc in procs:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
