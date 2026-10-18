"""A Bambuddy that fails every call and lists it, for the hot-path stack (#3818 tier A,
Task 12).

The stack's ``bambuddy_url`` points here, so a hot-path call to Bambuddy both fails
(``599``, no status Bambuddy sends) and is listed. Two reads are answered, because the
spool overlay's refresh makes them on its own Schedule, off the hot path (plan D1,
Task 6b), and the stack's seed runs it once before resetting the list:

- ``GET /api/v1/inventory/spools`` from ``answers/inventory-spools.json``, with spool
  ``RECORDER_TAGGED_SPOOL_ID`` (2) given the fake printer's tagged tray's ``tray_uuid``
  and ``tag_uid`` (``e2e-stack/fake-printer/report.json``, AMS 0 tray 0), so the overlay
  links it by RFID;
- ``GET /api/v1/inventory/assignments`` from ``answers/inventory-assignments.json``.

Both files are verbatim copies of ``backend/tests/bambuddy/recordings/`` (Bambuddy
1.2.5.5, recorded 2026-09-23; that folder's README says how), copied because this
image's build context is its own folder. A trailing slash and a query string do not
change which path answers.

Its own paths are never Bambuddy's ``/api/v1/*``: ``GET /__recorded`` answers
``[{"method", "path", "at"}...]`` for every other request since the last
``POST /__reset``. A request's query string, headers and body are never kept.

Usage: ``python recorder.py`` (the image's entrypoint), on ``RECORDER_PORT`` (8000).
"""

import json
import logging
import os
import threading
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

log = logging.getLogger("bambuddy-recorder")

ANSWERS = Path(__file__).with_name("answers")
#: The fake printer's tagged tray (``e2e-stack/fake-printer/report.json``).
TAGGED_TRAY_UUID = "5CADB0DDF1A7E5000000000000000001"
TAGGED_TAG_UID = "FA4E0DE1C0FFEE01"
#: The status every unanswered request gets.
FORBIDDEN_STATUS = 599


def _answers(tagged_spool_id: int) -> dict[str, Any]:
    spools = json.loads((ANSWERS / "inventory-spools.json").read_text())
    for spool in spools:
        if spool["id"] == tagged_spool_id:
            spool.update(tray_uuid=TAGGED_TRAY_UUID, tag_uid=TAGGED_TAG_UID)
    assignments = json.loads((ANSWERS / "inventory-assignments.json").read_text())
    return {
        "/api/v1/inventory/spools": spools,
        "/api/v1/inventory/assignments": assignments,
    }


class Recorded:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._requests: list[dict[str, str]] = []

    def add(self, method: str, path: str) -> None:
        with self._lock:
            self._requests.append({"method": method, "path": path, "at": _now()})

    def all(self) -> list[dict[str, str]]:
        with self._lock:
            return list(self._requests)

    def reset(self) -> None:
        with self._lock:
            self._requests.clear()


def _now() -> str:
    return datetime.now(UTC).isoformat()


def make_server(host: str, port: int) -> ThreadingHTTPServer:
    answers = _answers(int(os.environ.get("RECORDER_TAGGED_SPOOL_ID", "2")))
    recorded = Recorded()

    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: int, body: Any = None) -> None:
            data = b"" if body is None else json.dumps(body).encode()
            self.send_response(status)
            if body is not None:
                self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _handle(self) -> None:
            path = urlsplit(self.path).path
            if self.command == "GET" and path == "/__recorded":
                self._send(200, recorded.all())
                return
            if self.command == "POST" and path == "/__reset":
                recorded.reset()
                self._send(204)
                return
            recorded.add(self.command, path)
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                self.rfile.read(length)
            answer = answers.get(path.rstrip("/")) if self.command == "GET" else None
            if answer is not None:
                self._send(200, answer)
            else:
                self._send(FORBIDDEN_STATUS, {"detail": "the recorder answers no hot-path call"})

        def do_GET(self) -> None:
            self._handle()

        def do_POST(self) -> None:
            self._handle()

        def do_PUT(self) -> None:
            self._handle()

        def do_PATCH(self) -> None:
            self._handle()

        def do_DELETE(self) -> None:
            self._handle()

        def do_HEAD(self) -> None:
            recorded.add(self.command, urlsplit(self.path).path)
            self.send_response(FORBIDDEN_STATUS)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, format: str, *args: Any) -> None:
            log.info(format, *args)

    return ThreadingHTTPServer((host, port), Handler)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    server = make_server("0.0.0.0", int(os.environ.get("RECORDER_PORT", "8000")))
    log.info("listening on %s", server.server_port)
    server.serve_forever()


if __name__ == "__main__":
    main()
