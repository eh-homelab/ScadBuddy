"""A minimal sliced 3MF for tests that read one (#3822): ``read_filaments(sliced=True)``
needs G-code, ``slice_info.config`` and a project settings file."""

import io
import zipfile
from collections.abc import Mapping


def sliced_3mf(
    *, plates: Mapping[int, tuple[int, list[float]]], gcode: Mapping[int, bytes]
) -> bytes:
    """``plates``: plate -> (prediction seconds, used grams per slot from 1);
    ``gcode``: plate -> its ``Metadata/plate_<n>.gcode``."""
    info = ["<config>"]
    for index, (seconds, grams) in plates.items():
        info.append("  <plate>")
        info.append(f'    <metadata key="index" value="{index}"/>')
        info.append(f'    <metadata key="prediction" value="{seconds}"/>')
        info.append(f'    <metadata key="weight" value="{sum(grams)}"/>')
        for slot, used in enumerate(grams, start=1):
            info.append(f'    <filament id="{slot}" type="PLA" color="#FFFFFF" used_g="{used}"/>')
        info.append("  </plate>")
    info.append("</config>")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("Metadata/project_settings.config", "{}")
        archive.writestr("Metadata/slice_info.config", "\n".join(info))
        for index, data in gcode.items():
            archive.writestr(f"Metadata/plate_{index}.gcode", data)
    return buffer.getvalue()


def with_corrupt_member(payload: bytes, member: str) -> bytes:
    """``payload`` re-deflated, with the start of ``member``'s compressed stream
    overwritten, so inflating it fails (``zlib.error`` or a bad CRC)."""
    out = io.BytesIO()
    with (
        zipfile.ZipFile(io.BytesIO(payload)) as original,
        zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive,
    ):
        for name in original.namelist():
            archive.writestr(name, original.read(name))
    corrupt = bytearray(out.getvalue())
    with zipfile.ZipFile(io.BytesIO(bytes(corrupt))) as archive:
        info = archive.getinfo(member)
    # The member's data starts after its 30-byte local header, its name and its extra.
    start = info.header_offset + 30 + len(info.filename) + len(info.extra)
    corrupt[start : start + 16] = b"\xff" * 16
    return bytes(corrupt)
