"""#8366: `tags.ndef_message_bytes`, the server's copy of the page's `ndefMessageBytes`
(`frontend/src/lib/webNfc.ts`), counted as NFC Forum NDEF 1.0 section 3.2 lays a record out."""

from __future__ import annotations

from scadbuddy.filament import models as m
from scadbuddy.filament.tags import ndef_message_bytes


def _rec(record_type: str, data_hex: str = "", **extra: str) -> m.NdefRecord:
    return m.NdefRecord.model_validate({"record_type": record_type, "data_hex": data_hex} | extra)


def test_no_records_is_one_empty_record() -> None:
    assert ndef_message_bytes([]) == 3


def test_a_mime_record_counts_header_lengths_type_and_payload() -> None:
    # Header 1, type length 1, SR payload length 1, "application/json" 16, payload 2.
    assert ndef_message_bytes([_rec("mime", "7b7d", media_type="application/json")]) == 21


def test_a_long_payload_takes_a_four_byte_length() -> None:
    record = _rec("mime", "00" * 300, media_type="application/json")
    assert ndef_message_bytes([record]) == 2 + 4 + 16 + 300


def test_text_and_url_records() -> None:
    # Text: type "T", then the status byte, "en" and "hi".
    assert ndef_message_bytes([_rec("text", "6869")]) == 3 + 1 + 1 + 2 + 2
    # URL: type "U", then the prefix byte and the URL.
    assert ndef_message_bytes([_rec("url", "6162")]) == 3 + 1 + 1 + 2
    # An id adds its length byte and itself.
    assert ndef_message_bytes([_rec("url", "6162", id="x")]) == 3 + 1 + 1 + 1 + 1 + 2
