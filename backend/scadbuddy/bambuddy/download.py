"""A downloaded 3MF is the output's saved file, byte for byte (#2652).

It used to be rebuilt on every download (#769, #770, #1280): re-plated for the default
printer in Settings, named on presets read from Bambuddy within a 20 s budget, and given
the template's ``print_settings``. So a download depended on Bambuddy being up and on
what Settings said at that moment, and could differ from what was saved.

Now whatever a file needs to open correctly in Bambu Studio is written when the output
is **saved**: a file laid out for a printer names that printer and each slot's material
(#2639), and the template's ``print_settings`` (#770) go into its
``project_settings.config`` (``OutputStore.create``, ``render.provenance.stamp``). The
download is a plain :class:`FileResponse`, so Range and conditional GET work.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.responses import FileResponse

from scadbuddy.library.outputs import OutputMeta, download_filename

THREE_MF_MEDIA_TYPE = "model/3mf"


def download_3mf(path: Path, meta: OutputMeta) -> FileResponse:
    """``path``'s 3MF as an attachment, as it was saved."""
    return FileResponse(path, media_type=THREE_MF_MEDIA_TYPE, filename=download_filename(meta))
