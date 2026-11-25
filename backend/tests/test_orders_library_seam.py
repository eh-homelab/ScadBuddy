"""The orders library seam (#7467): what reads, prices, prints and restores a library file
needs no Bambuddy. ``orders/served_library.py`` does not load ``bambuddy.client``, and
Bambuddy's adapter (``orders/bambuddy.py``) is loaded only where Bambuddy's branch is
taken."""

from __future__ import annotations

import subprocess
import sys

ADAPTER = "scadbuddy.orders.bambuddy"
CLIENT = "scadbuddy.bambuddy.client"


def _loaded(code: str) -> set[str]:
    """Which of the adapter and the client a fresh process that runs ``code`` has loaded."""
    script = f"{code}\nimport sys\nprint([m for m in {[ADAPTER, CLIENT]!r} if m in sys.modules])"
    ran = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=False
    )
    assert ran.returncode == 0, ran.stderr
    last = ran.stdout.splitlines()[-1]
    return {name for name in (ADAPTER, CLIENT) if repr(name) in last}


def test_the_seam_loads_no_bambuddy_client() -> None:
    assert _loaded("import scadbuddy.orders.served_library") == set()


def test_the_app_loads_no_bambuddy_adapter() -> None:
    """The whole API's import, every caller of the seam included: the client's module is
    loaded (the print path shares its package), orders' adapter is not."""
    assert ADAPTER not in _loaded("import scadbuddy.main")


def test_a_native_read_and_restore_load_no_bambuddy_client() -> None:
    """The native branch, run: a file read, downloaded and its colours, and the shelf. The
    settings are never read."""
    code = """
import asyncio
from types import SimpleNamespace
from scadbuddy.bambuddy.models import FilamentRequirements, LibraryFile
from scadbuddy.orders.served_library import NativeShelf, ServedLibrary, served_shelf
from scadbuddy.print_library.ids import NATIVE_ID_BASE

class Native:
    async def library_file(self, file_id):
        return LibraryFile(id=file_id, filename="a.3mf")
    async def download_library_file(self, file_id):
        yield b"bytes"
    async def filament_requirements(self, file_id, *, plate_id=None):
        return FilamentRequirements(filaments=[])

async def main():
    native = Native()
    async def serving():
        return native
    library = ServedLibrary(None, serving)
    assert (await library.file(NATIVE_ID_BASE)).filename == "a.3mf"
    assert await library.file(89) is None
    assert [c async for c in library.download(NATIVE_ID_BASE)] == [b"bytes"]
    assert await library.colours(NATIVE_ID_BASE) == {}
    async with served_shelf(None, native, None) as shelf:
        assert isinstance(shelf, NativeShelf)

asyncio.run(main())
"""
    assert _loaded(code) == set()


def test_bambuddys_branch_loads_the_adapter() -> None:
    """The check sees the adapter where it is taken."""
    code = """
import asyncio
from scadbuddy.core.problems import ApiError
from scadbuddy.library.settings_store import StoredSettings
from scadbuddy.orders.served_library import ServedLibrary

class Settings:
    def load(self):
        return StoredSettings()

async def none():
    return None

try:
    asyncio.run(ServedLibrary(Settings(), none).file(89))
except ApiError as error:
    assert error.status == 409
else:
    raise AssertionError("no URL is stored")
"""
    assert _loaded(code) == {ADAPTER, CLIENT}
