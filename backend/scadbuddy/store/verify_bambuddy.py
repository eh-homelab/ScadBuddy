"""Measure spec 2026-09-27 §6.3's facts against a live Bambuddy. Manual; never CI.

    SCADBUDDY_VERIFY_BAMBUDDY_URL=https://bambuddy.example \\
    SCADBUDDY_VERIFY_BAMBUDDY_KEY=<a Manage-Library-only key> \\
    SCADBUDDY_VERIFY_INBOX=<the inbox folder id> \\
    uv run python -m scadbuddy.store.verify_bambuddy

It works in `ScadBuddy verify/Work` under the inbox, deletes every file it uploaded,
and prints a Markdown table for tests/bambuddy/recordings/README.md. With
SCADBUDDY_VERIFY_RECORD_DIR set it also writes the upload and file responses there.
The folders stay: ScadBuddy never deletes folders.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import sys
import time
import zipfile
from pathlib import Path

from PIL import Image

from scadbuddy.bambuddy.client import BambuddyClient, BambuddyConfig
from scadbuddy.bambuddy.models import FolderCreate
from scadbuddy.core.problems import ApiError


def _samples() -> list[tuple[str, bytes, str]]:
    png = io.BytesIO()
    Image.new("L", (4, 4), 255).save(png, format="PNG")
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("piece.json", "{}")
    svg = b'<svg xmlns="http://www.w3.org/2000/svg"><rect width="1" height="1"/></svg>'
    return [
        ("verify.svg", svg, "image/svg+xml"),
        ("verify.png", png.getvalue(), "image/png"),
        ("verify.zip", archive.getvalue(), "application/zip"),
    ]


async def _folder(client: BambuddyClient, name: str, parent: int) -> int:
    for root in await client.folders():
        for folder in root.walk():
            if folder.parent_id == parent and folder.name == name:
                return folder.id
    return (await client.create_folder(FolderCreate(name=name, parent_id=parent))).id


async def _cleanup(client: BambuddyClient, file_ids: list[int]) -> list[str]:
    """Delete every file; one that fails is reported, and the rest still go."""
    failed: list[str] = []
    for file_id in file_ids:
        try:
            await client.delete_library_file(file_id)
        except ApiError as error:
            failed.append(f"{file_id}: {error.status} {error.detail}")
    return failed


async def main() -> int:
    url = os.environ["SCADBUDDY_VERIFY_BAMBUDDY_URL"].rstrip("/")
    key = os.environ.get("SCADBUDDY_VERIFY_BAMBUDDY_KEY")
    inbox = int(os.environ["SCADBUDDY_VERIFY_INBOX"])
    record = os.environ.get("SCADBUDDY_VERIFY_RECORD_DIR")
    rows: list[tuple[str, str]] = []
    uploaded: list[int] = []
    config = BambuddyConfig(base_url=url, api_key=key, upload_timeout=600.0)
    async with BambuddyClient(config) as client:
        work = await _folder(client, "Work", await _folder(client, "ScadBuddy verify", inbox))
        try:
            for name, data, media in _samples():
                file = await client.upload_library_file(
                    name, data, folder_id=work, media_type=media
                )
                uploaded.append(file.id)
                got = b"".join([c async for c in client.download_library_file(file.id)])
                detail = await client.library_file(file.id)
                same = "identical" if got == data else f"differ ({len(got)} vs {len(data)} bytes)"
                rows.append((f"{media} upload, download by id", f"accepted; bytes {same}"))
                is_sha = detail.file_hash == hashlib.sha256(data).hexdigest()
                rows.append(
                    (f"`file_hash` of {name}", "sha256" if is_sha else repr(detail.file_hash))
                )
                if record:
                    Path(record, f"store-file-{name}.json").write_text(
                        detail.model_dump_json(indent=2)
                    )
            again = await client.upload_library_file(
                "verify-again.zip", _samples()[2][1], folder_id=work, media_type="application/zip"
            )
            uploaded.append(again.id)
            rows.append(
                (
                    "re-upload of identical bytes",
                    f"new id {again.id}, duplicate_of={again.duplicate_of}",
                )
            )
            for mib in (8, 32, 128, 512):
                try:
                    big = await client.upload_library_file(
                        f"verify-{mib}.zip",
                        os.urandom(mib << 20),
                        folder_id=work,
                        media_type="application/zip",
                    )
                    uploaded.append(big.id)
                    rows.append((f"upload {mib} MiB", "accepted"))
                except ApiError as error:
                    rows.append((f"upload {mib} MiB", f"refused: {error.status} {error.detail}"))
                    break
            started = time.monotonic()
            for i in range(40):
                small = await client.upload_library_file(
                    f"verify-t{i}.zip",
                    os.urandom(64 << 10),
                    folder_id=work,
                    media_type="application/zip",
                )
                uploaded.append(small.id)
            rows.append(("40 uploads of 64 KiB, sequential", f"{time.monotonic() - started:.1f} s"))
            try:
                probe = await client.upload_library_file(
                    "x.zip", b"x", folder_id=2**31 - 1, media_type="application/zip"
                )
                uploaded.append(probe.id)
                rows.append(("upload into a folder that does not exist", "accepted (!)"))
            except ApiError as error:
                rows.append(("upload into a folder that does not exist", f"{error.status}"))
        finally:
            failed = await _cleanup(client, uploaded)
    print("| Measurement | Result |\n|---|---|")
    for what, result in rows:
        print(f"| {what} | {result} |")
    report = {"bambuddy": url, "files_cleaned": len(uploaded) - len(failed), "failed": failed}
    print(json.dumps(report), file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
