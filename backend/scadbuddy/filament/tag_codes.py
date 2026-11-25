"""What a tag's NDEF records name, for the hand-held Scan mode (#7273, #6955): a typed code
(`L-12`, `S-142`, a `/x/<code>` URL) or a `scadbuddy:spool:<id>` text, resolved to its row."""

from __future__ import annotations

import re
from collections.abc import Sequence
from urllib.parse import urlsplit
from uuid import UUID

from scadbuddy.codes.parse import Code, format_code, parse_code
from scadbuddy.codes.resolve import Conn, Resolved, resolve
from scadbuddy.filament import models as m

_SPOOL_TEXT = re.compile(r"^scadbuddy:spool:([0-9a-f-]{36})$", re.IGNORECASE)


#: The encodings Web NFC gives a text record (`NDEFRecord.encoding`: "utf-8", "utf-16",
#: "utf-16le" or "utf-16be"), by their lower-case spellings with or without the dash, to the
#: codec. Any other label is skipped: the stdlib's other codecs (idna, punycode, utf-7) are
#: never a tag's, and some raise a bare `UnicodeError` on bad bytes.
_CODECS: dict[str, str] = {
    "utf-8": "utf-8",
    "utf8": "utf-8",
    "utf-16": "utf-16",
    "utf16": "utf-16",
    "utf-16le": "utf-16-le",
    "utf-16-le": "utf-16-le",
    "utf-16be": "utf-16-be",
    "utf-16-be": "utf-16-be",
}


def decode_text(data: bytes, encoding: str | None) -> str | None:
    """The text of a text record's bytes in ``encoding``, or ``None`` when the encoding is not
    one Web NFC gives or the bytes are not valid text in it. UTF-16 with no byte order named is
    read as the frontend's `decodeText` (`frontend/src/lib/webNfc.ts`) reads it: little-endian
    by a FF FE BOM, else big-endian. A leading BOM is dropped (the frontend keeps a UTF-8 one
    for its preview; a code never starts with one)."""
    codec = _CODECS.get((encoding or "utf-8").lower())
    if codec is None:
        return None
    if codec == "utf-16":
        codec = "utf-16-le" if data[:2] == b"\xff\xfe" else "utf-16-be"
    try:
        return data.decode(codec).removeprefix("\N{ZERO WIDTH NO-BREAK SPACE}")
    except ValueError:
        # `UnicodeDecodeError`, and any other `UnicodeError` a codec raises.
        return None


def _code_in_url(record: m.NdefRecord) -> Code | None:
    href = decode_text(bytes.fromhex(record.data_hex), "utf-8")
    if href is None:
        return None
    try:
        path = urlsplit(href).path
    except ValueError:
        # An unclosed IPv6 host, or a host that is not NFKC: not a label's URL.
        return None
    segments = [part for part in path.split("/") if part]
    if len(segments) < 2 or segments[-2] != "x":
        return None
    return parse_code(segments[-1])


def _code_in_text(record: m.NdefRecord) -> Code | UUID | None:
    text = decode_text(bytes.fromhex(record.data_hex), record.encoding)
    if text is None:
        return None
    text = text.strip()
    code = parse_code(text)
    if code is not None:
        return code
    found = _SPOOL_TEXT.match(text)
    if found is None:
        return None
    try:
        return UUID(found.group(1))
    except ValueError:
        return None


def code_in(records: Sequence[m.NdefRecord]) -> Code | UUID | None:
    """The typed code, or the spool id of a `scadbuddy:spool:<uuid>` text, that the first
    record naming one carries. A `url` record names a code when its path's last two segments
    are `x` and a code, under any origin and with any query. A `text` record names a bare
    code in any case, or a spool id. Other record types, and bytes that are not text in
    their encoding, are skipped; nothing here raises on a tag's bytes."""
    for record in records:
        found: Code | UUID | None = None
        if record.record_type == "url":
            found = _code_in_url(record)
        elif record.record_type == "text":
            found = _code_in_text(record)
        if found is not None:
            return found
    return None


def resolve_records(conn: Conn, records: Sequence[m.NdefRecord]) -> Resolved | None:
    """The row the records name, in the `/codes/{code}` shape, or ``None``. An archived row
    still resolves; a code or id that names no row is nothing."""
    found = code_in(records)
    if found is None:
        return None
    if isinstance(found, Code):
        return resolve(conn, found)
    row = conn.execute(
        "SELECT number, archived_at FROM filament_spools WHERE id = %s", (found,)
    ).fetchone()
    if row is None:
        return None
    return Resolved(
        kind="spool",
        id=found,
        code=format_code("S", row["number"]),
        name=None,
        archived=row["archived_at"] is not None,
    )
