"""The "Edit in ScadBuddy" deep link.

One definition, because three places have to agree on it: the frontend route that
resolves it, the 3MF stamped with it, and the Bambuddy library file annotated with
it. ``public_url`` is the setting Bambuddy's sidebar link already uses — the server
cannot infer its own external URL from a request behind a proxy.
"""

from __future__ import annotations

EDIT_ROUTE = "/edit"
#: How the link is introduced wherever it appears as prose — the 3MF's own
#: Description metadata and the note on the Bambuddy library file.
EDIT_NOTE = "Edit in ScadBuddy: "


def edit_path(output_id: str) -> str:
    return f"{EDIT_ROUTE}/{output_id}"


def edit_url(public_url: str | None, output_id: str) -> str | None:
    """None when no public URL is configured, so callers can leave the link out."""
    if not public_url:
        return None
    return f"{public_url.rstrip('/')}{edit_path(output_id)}"
