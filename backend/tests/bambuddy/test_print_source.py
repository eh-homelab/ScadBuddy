"""The print source seam (#313): what the run reads from an output or a library file."""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

from scadbuddy.bambuddy.print_source import OutputSource, PrintSource
from scadbuddy.library.settings_store import StoredSettings


class _Meta:
    id = "a" * 32
    slug = "name-keychain"
    colors: ClassVar[list[str]] = ["#FF0000", "#0000FF"]


def test_an_output_source_is_the_models_colors_and_slug(tmp_path: Path) -> None:
    unused: Any = object()
    meta: Any = _Meta()
    source: PrintSource = OutputSource(
        store=unused, uploads=unused, meta=meta, settings=StoredSettings()
    )

    assert source.colours == ["#FF0000", "#0000FF"]
    assert source.filament_count == 2
    assert source.options_slug == "name-keychain"
