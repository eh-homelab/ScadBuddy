"""Location labels (#6955)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def label_base_url(read: Mapping[str, Any]) -> str | None:
    """The https origin a location label's QR opens: ``filament_label_base_url``, else
    ``public_url``, else ``None``. ``read`` is what ``settings_store.read`` gives for both
    names."""
    return read.get("filament_label_base_url") or read.get("public_url") or None
