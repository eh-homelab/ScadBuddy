"""The "Edit in ScadBuddy" deep link.

One definition, because three places have to agree on it: the frontend route that
resolves it, the 3MF stamped with it, and the Bambuddy library file annotated with
it. ``public_url`` is the setting Bambuddy's sidebar link already uses — the server
cannot infer its own external URL from a request behind a proxy.
"""

from __future__ import annotations

import re

EDIT_ROUTE = "/edit"
#: How the link is introduced wherever it appears as prose — the 3MF's own
#: Description metadata and the note on the Bambuddy library file.
EDIT_NOTE = "Edit in ScadBuddy: "


def edit_path(output_id: str) -> str:
    return f"{EDIT_ROUTE}/{output_id}"


#: What XML 1.0 cannot represent at all, escaped or not. The link is the one piece
#: of settable text that reaches the 3MF as prose rather than through JSON, so a
#: setting holding one of these would corrupt every file written after it was saved.
_UNWRITABLE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def edit_url(public_url: str | None, output_id: str) -> str | None:
    """None when no public URL is configured, so callers can leave the link out.

    A URL that cannot be written is treated the same way: there is nothing to show,
    and dropping the link costs one feature rather than every output's 3MF.
    """
    if not public_url or _UNWRITABLE.search(public_url):
        return None
    return f"{public_url.rstrip('/')}{edit_path(output_id)}"


def merge_edit_note(notes: str | None, link: str) -> str:
    """The file's notes with our line on the end, replacing any earlier one of ours.

    ``notes`` is the one free-text field a library file has, so a person may have
    typed into it. Writing the link straight over it would throw that away silently,
    and re-sending the same output would otherwise stack duplicate lines.
    """
    kept = [line for line in (notes or "").splitlines() if not line.startswith(EDIT_NOTE)]
    while kept and not kept[-1].strip():
        kept.pop()
    kept.append(f"{EDIT_NOTE}{link}")
    return "\n".join(kept)
