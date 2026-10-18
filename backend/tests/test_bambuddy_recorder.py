"""The Bambuddy recorder (``e2e-stack/bambuddy-recorder/recorder.py``, #3818 tier A,
Task 12): the hot-path stack's ``bambuddy_url``. It answers the overlay refresh's two
inventory reads, fails everything else with 599, and lists every Bambuddy request."""

import importlib.util
import json
import sys
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

RECORDER_PY = (
    Path(__file__).resolve().parents[2] / "e2e-stack" / "bambuddy-recorder" / "recorder.py"
)
RECORDINGS = Path(__file__).parent / "bambuddy" / "recordings"
FAKE_REPORT = Path(__file__).resolve().parents[2] / "e2e-stack" / "fake-printer" / "report.json"

pytestmark = pytest.mark.skipif(
    not RECORDER_PY.is_file(), reason=f"{RECORDER_PY} is not in this tree"
)


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("bambuddy_recorder", RECORDER_PY)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["bambuddy_recorder"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def recorder() -> Iterator[str]:
    module = _load()
    server = module.make_server("127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def _call(url: str, method: str = "GET") -> tuple[int, Any]:
    data = b"" if method in ("POST", "PUT", "PATCH") else None
    request = urllib.request.Request(url, method=method, data=data)
    try:
        with urllib.request.urlopen(request, timeout=5) as answer:
            status, body = answer.status, answer.read()
    except urllib.error.HTTPError as error:
        status, body = error.code, error.read()
    return status, json.loads(body) if body else None


def test_the_inventory_reads_are_answered(recorder: str) -> None:
    status, spools = _call(f"{recorder}/api/v1/inventory/spools?include_archived=false")
    assert status == 200
    assert len(spools) == len(json.loads((RECORDINGS / "inventory-spools.json").read_text()))
    tagged = [s for s in spools if (s.get("tray_uuid") or "0" * 32) != "0" * 32]
    assert [s["id"] for s in tagged] == [2]
    module = sys.modules["bambuddy_recorder"]
    assert tagged[0]["tray_uuid"] == module.TAGGED_TRAY_UUID
    assert tagged[0]["tag_uid"] == module.TAGGED_TAG_UID

    status, assignments = _call(f"{recorder}/api/v1/inventory/assignments/")
    assert status == 200
    assert assignments == json.loads((RECORDINGS / "inventory-assignments.json").read_text())


def test_anything_else_fails_and_everything_is_listed(recorder: str) -> None:
    assert _call(f"{recorder}/api/v1/printers/")[0] == 599
    assert _call(f"{recorder}/api/v1/library/files/", "POST")[0] == 599
    _call(f"{recorder}/api/v1/inventory/spools")
    assert _call(f"{recorder}/api/v1/printers/", "OPTIONS")[0] == 599
    assert _call(f"{recorder}/api/v1/printers/1", "PURGE")[0] == 599

    status, recorded = _call(f"{recorder}/__recorded")
    assert status == 200
    assert [(r["method"], r["path"]) for r in recorded] == [
        ("GET", "/api/v1/printers/"),
        ("POST", "/api/v1/library/files/"),
        ("GET", "/api/v1/inventory/spools"),
        ("OPTIONS", "/api/v1/printers/"),
        ("PURGE", "/api/v1/printers/1"),
    ]
    assert all(r["at"] for r in recorded)

    assert _call(f"{recorder}/__reset", "POST")[0] == 204
    assert _call(f"{recorder}/__recorded") == (200, [])


def test_the_tagged_spool_is_the_fake_printers_tagged_tray() -> None:
    """The overlay links them by RFID only if the two fakes name the same spool."""
    module = _load()
    report = json.loads(FAKE_REPORT.read_text())["print"]
    trays = [tray for unit in report["ams"]["ams"] for tray in unit["tray"]]
    tagged = [t for t in trays if t.get("tray_uuid", "0" * 32) != "0" * 32]
    assert [(t["tray_uuid"], t["tag_uid"]) for t in tagged] == [
        (module.TAGGED_TRAY_UUID, module.TAGGED_TAG_UID)
    ]
