"""A downloaded 3MF is the output's saved file, byte for byte (#2652).

It used to be rebuilt on every download (#769, #770, #1280): re-plated for the default
printer in Settings, named on presets read from Bambuddy within a 20 s budget, and given
the template's ``print_settings``. So a download depended on Bambuddy being up and on
what Settings said at that moment, and could differ from what was saved.

Now whatever a file needs to open correctly in Bambu Studio is written when the output
is **saved**: a file laid out for a printer names that printer and each slot's material
(#2639), and the template's ``print_settings`` (#770) go into its
``project_settings.config`` (``OutputStore.create``, ``render.provenance.stamp``). The
download streams the saved bytes, the blob first, else the file (#7256,
`api/output_streams.py`): it has a ``Content-Length`` and, for a blob, a strong ``ETag``,
and it serves no ranges.
"""

from __future__ import annotations

from fastapi.responses import StreamingResponse

from scadbuddy.api.output_streams import attachment, stream_response
from scadbuddy.library.output_bytes import OutputStream
from scadbuddy.library.outputs import OutputMeta, download_filename

THREE_MF_MEDIA_TYPE = "model/3mf"


def download_3mf(stream: OutputStream, meta: OutputMeta) -> StreamingResponse:
    """``stream``'s 3MF as an attachment, as it was saved."""
    return stream_response(
        stream,
        media_type=THREE_MF_MEDIA_TYPE,
        headers={"content-disposition": attachment(download_filename(meta))},
    )
