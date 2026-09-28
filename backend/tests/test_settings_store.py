"""``SettingsStore`` under concurrent writers (PR #335 review 1).

The print picker fires two remember PUTs back to back and FastAPI runs each sync
handler on its own threadpool thread, so two setters really do overlap.
"""

from __future__ import annotations

import contextlib
import threading
from pathlib import Path

import pytest

from scadbuddy.core.settings import Settings
from scadbuddy.library.settings_store import (
    KEY_FILE_MODE,
    ModelPrintChoices,
    SettingsStore,
    StoredSettings,
)


def test_two_setters_at_once_both_persist(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Each setter's ``load`` waits for the other's, which is exactly the interleaving that
    lost a write: both read the empty file, and whichever wrote second overwrote the
    first. With the store's lock the second ``load`` cannot start until the first
    setter has written, so the wait times out and the writes land in turn."""
    path = tmp_path / "settings.json"
    store = SettingsStore(path, Settings(data_dir=tmp_path))
    real_load = store.load
    both_loaded = threading.Barrier(2)

    def load_then_wait() -> StoredSettings:
        loaded = real_load()
        with contextlib.suppress(threading.BrokenBarrierError):
            both_loaded.wait(timeout=0.5)
        return loaded

    monkeypatch.setattr(store, "load", load_then_wait)
    threads = [
        threading.Thread(
            target=store.set_model_choices, args=("gear", ModelPrintChoices(tier="fine"))
        ),
        threading.Thread(target=store.set_printer_bed_type, args=(1, "Cool Plate")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    saved = SettingsStore(path, Settings(data_dir=tmp_path)).load()
    assert saved.model_print_choices["gear"].tier == "fine"
    assert saved.printer_bed_types == {"1": "Cool Plate"}


def test_a_write_replaces_the_file_whole_and_leaves_no_temporary_behind(tmp_path: Path) -> None:
    store = SettingsStore(tmp_path / "settings.json", Settings(data_dir=tmp_path))
    store.set_printer_bed_type(1, "Cool Plate")
    store.set_printer_bed_type(2, "Supertack Plate")

    assert [p.name for p in tmp_path.iterdir()] == ["settings.json"]
    assert (tmp_path / "settings.json").stat().st_mode & 0o777 == KEY_FILE_MODE
    assert store.load().printer_bed_types == {"1": "Cool Plate", "2": "Supertack Plate"}
