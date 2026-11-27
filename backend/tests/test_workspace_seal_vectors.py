"""The workspace files' sealing vectors (`agent_data/workspace.py` `seal_file`, `open_file`,
`file_key_context`, and the payload key's context), which Go core's copy reads too
(`tools/go/core/internal/workspace/vectors_test.go`, #8131): each segment's and each file
key's context, each sealed file and each wrapped file key written here by Python's own
functions, so Python and Go open each other's files.

A seal takes its IVs from ``os.urandom``; the vectors take them from a counter instead
(`_counter`), which Go's test runs again, so Go's sealing must write these bytes exactly.

`UPDATE_WORKSPACE_SEAL_VECTORS=1 uv run --frozen pytest tests/test_workspace_seal_vectors.py`
writes the file again; without it the test checks that the file is what these functions
write."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import pytest

from scadbuddy.agent_data import workspace as ws
from scadbuddy.core.secrets import kek_from_base64, open_bytes, seal_bytes
from scadbuddy.workflows.payload_codec import payload_key_context

FILE = Path(__file__).parent / "fixtures" / "workspace-seal-vectors.json"
KEK_B64 = "WlpaWlpaWlpaWlpaWlpaWlpaWlpaWlpaWlpaWlpaWlo="
SESSION = "0b0e6c1e-5d2a-4f43-9a51-4c3cbd3f2e01"
#: The session's payload data key.
DEK = bytes(range(32))
#: Files are written in full below this size, and by their sha256 above it.
INLINE_MAX = 64 * 1024
#: The sizes: empty, small, one whole segment, one byte past it, and three segments.
SIZES = [0, 5, ws.SEGMENT_BYTES, ws.SEGMENT_BYTES + 1, 2 * ws.SEGMENT_BYTES + 17]
#: The rows whose file keys are wrapped: a workspace a writer could spell two ways, a path,
#: and the file (an index into SIZES) whose key and blob the row names.
WRAPS = [
    (f"session:{SESSION}", "notes/a.txt", 1),
    (f"session:{SESSION}", "downloads/part.3mf", 3),
    ("principal:oidc:https://idp.example/#sub é", "a.txt", 1),
    ("principal:bearer:token:1", "x_y-z.bin", 4),
]


def _counter(start: int) -> Callable[[int], bytes]:
    state = {"n": start}

    def random(size: int) -> bytes:
        out = bytes((state["n"] + i) & 0xFF for i in range(size))
        state["n"] += size
        return out

    return random


def plaintext(size: int, seed: int) -> bytes:
    """The vector's plaintext: byte ``i`` is ``(i * 7 + seed) & 0xff``."""
    return bytes((i * 7 + seed) & 0xFF for i in range(size))


def _opened(key: bytes, blob: bytes, digest: str) -> bytes:
    async def chunks() -> AsyncIterator[bytes]:
        for i in range(0, max(len(blob), 1), 65536):
            yield blob[i : i + 65536]

    async def run() -> bytes:
        return b"".join([p async for p in ws.open_file(key, chunks(), digest)])

    return asyncio.run(run())


def _vectors(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    kek = kek_from_base64(KEK_B64)
    subject = f"session-{SESSION}"
    files = []
    for n, size in enumerate(SIZES):
        data = plaintext(size, n)
        digest = hashlib.sha256(data).hexdigest()
        file_key = bytes((n * 32 + i) & 0xFF for i in range(32))
        monkeypatch.setattr(os, "urandom", _counter(n * 4096))
        sealed = ws.seal_file(file_key, data, digest)
        monkeypatch.undo()
        segments = max(1, -(-size // ws.SEGMENT_BYTES))
        entry: dict[str, Any] = {
            "size": size,
            "seed": n,
            "iv_counter": n * 4096,
            "sha256": digest,
            "file_key_b64": base64.b64encode(file_key).decode(),
            "contexts": [
                ws._segment_context(digest, i, i == segments - 1) for i in range(segments)
            ],
            "sealed_size": len(sealed),
            "sealed_sha256": hashlib.sha256(sealed).hexdigest(),
        }
        if size <= INLINE_MAX:
            entry["sealed_b64"] = base64.b64encode(sealed).decode()
        files.append(entry)
    wraps = []
    for n, (workspace, path, file) in enumerate(WRAPS):
        # A blob's key is its sealed bytes' sha256 (`crud_agent_workspace.py` `_place`).
        blob_key = f"workspace-{files[file]['sealed_sha256']}"
        file_key = base64.b64decode(files[file]["file_key_b64"])
        context = ws.file_key_context(workspace, path, blob_key)
        session = ws.session_of(workspace)
        key = DEK if session is not None else kek.key
        iv = bytes((0xC0 + n * 12 + i) & 0xFF for i in range(12))
        wraps.append(
            {
                "workspace": workspace,
                "path": path,
                "file": file,
                "blob_key": blob_key,
                "context": context,
                "under": "session" if session is not None else "kek",
                "file_key_b64": base64.b64encode(file_key).decode(),
                "iv_b64": base64.b64encode(iv).decode(),
                "wrapped_b64": base64.b64encode(seal_bytes(key, file_key, context, iv=iv)).decode(),
            }
        )
    dek_iv = bytes(range(100, 112))
    return {
        "kek_b64": KEK_B64,
        "kek_id": kek.id,
        "segment_bytes": ws.SEGMENT_BYTES,
        "session": SESSION,
        "payload_key": {
            "subject": subject,
            "context": payload_key_context(subject),
            "dek_b64": base64.b64encode(DEK).decode(),
            "iv_b64": base64.b64encode(dek_iv).decode(),
            "dek_sealed_b64": base64.b64encode(
                seal_bytes(kek.key, DEK, payload_key_context(subject), iv=dek_iv)
            ).decode(),
        },
        "files": files,
        "wraps": wraps,
    }


def test_the_vectors_are_what_python_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    want = json.dumps(_vectors(monkeypatch), indent=2, ensure_ascii=False) + "\n"
    if os.environ.get("UPDATE_WORKSPACE_SEAL_VECTORS"):
        FILE.write_text(want, encoding="utf-8")
    assert FILE.read_text(encoding="utf-8") == want


def test_every_vector_opens_with_its_context() -> None:
    doc = json.loads(FILE.read_text(encoding="utf-8"))
    kek = kek_from_base64(doc["kek_b64"])
    pk = doc["payload_key"]
    dek = open_bytes(kek.key, base64.b64decode(pk["dek_sealed_b64"]), pk["context"])
    assert dek == base64.b64decode(pk["dek_b64"])
    for v in doc["files"]:
        if "sealed_b64" not in v:
            continue
        key = base64.b64decode(v["file_key_b64"])
        sealed = base64.b64decode(v["sealed_b64"])
        assert _opened(key, sealed, v["sha256"]) == plaintext(v["size"], v["seed"])
    for v in doc["wraps"]:
        key = dek if v["under"] == "session" else kek.key
        wrapped = base64.b64decode(v["wrapped_b64"])
        assert open_bytes(key, wrapped, v["context"]) == base64.b64decode(v["file_key_b64"])
