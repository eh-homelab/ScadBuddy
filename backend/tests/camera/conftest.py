from __future__ import annotations

import hashlib
import os
import platform
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path

import pytest

from scadbuddy.core.secrets import Kek
from tests.printers.conftest import KEK

#: The MediaMTX release the camera e2e runs, the version the deploy configs were measured
#: on (deploy/camera/*.yml). Release: https://github.com/bluenviron/mediamtx/releases/tag/v1.21.4
#: Each digest is the release's own `checksums.sha256` line, and equals `sha256sum` of the
#: tarball downloaded on 2026-11-23. The Dockerfile's `test` stage pins the same two.
MEDIAMTX_VERSION = "v1.21.4"
MEDIAMTX_SHA256 = {
    "amd64": "be0c05d9c8af8338116977e7f1c3fcb409fbf882075e5ab075cf8baad76a59d2",
    "arm64": "47784081da54c0608ea4dc359f5cfb19880209434769c532b3ed3a67ea99c29d",
}
#: Set to ``1`` to make the camera e2e fail, not skip, when MediaMTX or ffmpeg is missing.
MEDIA_REQUIRED_ENV = "SCADBUDDY_TEST_MEDIA_REQUIRED"


@pytest.fixture
def kek() -> Kek:
    return KEK


def _missing(what: str) -> None:
    if os.environ.get(MEDIA_REQUIRED_ENV) == "1":
        pytest.fail(f"{what} ({MEDIA_REQUIRED_ENV}=1)")
    pytest.skip(what)


def _fetch_mediamtx() -> Path | str:
    """The pinned MediaMTX in the user's cache, downloaded once and checked by sha256; or
    why it cannot be had."""
    arch = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(
        platform.machine().lower()
    )
    if arch is None or platform.system() != "Linux":
        return f"no pinned MediaMTX for {platform.system()} {platform.machine()}"
    folder = Path.home() / ".cache" / "scadbuddy-test" / f"mediamtx-{MEDIAMTX_VERSION}-{arch}"
    binary = folder / "mediamtx"
    if binary.is_file():
        return binary
    name = f"mediamtx_{MEDIAMTX_VERSION}_linux_{arch}.tar.gz"
    url = f"https://github.com/bluenviron/mediamtx/releases/download/{MEDIAMTX_VERSION}/{name}"
    try:
        with urllib.request.urlopen(url, timeout=60) as response:
            body = response.read()
    except OSError as error:
        return f"MediaMTX is not on PATH and {url} did not download ({type(error).__name__})"
    if hashlib.sha256(body).hexdigest() != MEDIAMTX_SHA256[arch]:
        return f"{url} does not match its pinned sha256"
    folder.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=folder) as scratch:
        tarball = Path(scratch) / name
        tarball.write_bytes(body)
        with tarfile.open(tarball) as archive:
            archive.extract("mediamtx", scratch, filter="data")
        # A rename, so an xdist worker never runs a half-written binary of another's.
        (Path(scratch) / "mediamtx").replace(binary)
    return binary


@dataclass(frozen=True)
class Media:
    mediamtx: Path
    ffmpeg: Path


@pytest.fixture(scope="session")
def media() -> Media:
    """MediaMTX (on PATH, else the pinned release) and ffmpeg (on PATH); skips naming what
    is missing."""
    ffmpeg = shutil.which("ffmpeg")
    found = shutil.which("mediamtx")
    mediamtx: Path | str = Path(found) if found else _fetch_mediamtx()
    missing = [] if ffmpeg else ["ffmpeg is not on PATH"]
    if isinstance(mediamtx, str):
        missing.append(mediamtx)
    if missing:
        _missing("; ".join(missing))
    assert ffmpeg is not None and isinstance(mediamtx, Path)
    return Media(mediamtx=mediamtx, ffmpeg=Path(ffmpeg))


#: Starts a process with its log in the test's folder; the fixture kills it at teardown.
Spawn = Callable[[str, list[str], Mapping[str, str]], subprocess.Popen[bytes]]


@pytest.fixture
def spawn(tmp_path: Path) -> Iterator[Spawn]:
    procs: list[subprocess.Popen[bytes]] = []

    def start(name: str, argv: list[str], env: Mapping[str, str]) -> subprocess.Popen[bytes]:
        log = (tmp_path / f"{name}.log").open("wb")
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            env={"PATH": os.environ.get("PATH", ""), "TZ": "UTC", **env},
            cwd=tmp_path,
        )
        log.close()
        procs.append(proc)
        return proc

    yield start
    for proc in procs:
        proc.kill()
    for proc in procs:
        proc.wait(timeout=10)
