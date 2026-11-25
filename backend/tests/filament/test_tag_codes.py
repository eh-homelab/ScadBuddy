"""A typed code or a `scadbuddy:spool:<id>` text read out of a tag's NDEF records (#7273,
#6955: the hand-held Scan mode)."""

from __future__ import annotations

from uuid import UUID

import pytest

from scadbuddy.codes.parse import Code
from scadbuddy.filament.models import NdefRecord
from scadbuddy.filament.tag_codes import code_in

SPOOL_ID = UUID("0b6f1b2c-3d4e-4f50-8a9b-0c1d2e3f4a5b")


def url(href: str) -> NdefRecord:
    """A `url` record as Web NFC hands it to the page: the URL's UTF-8 text as `data`."""
    return NdefRecord(record_type="url", data_hex=href.encode("utf-8").hex())


def text(value: str, encoding: str = "utf-8") -> NdefRecord:
    """A `text` record: the text's bytes in `encoding`, with no status byte."""
    codec = "utf-16-be" if encoding == "utf-16" else encoding
    return NdefRecord(record_type="text", encoding=encoding, data_hex=value.encode(codec).hex())


def test_a_url_record_ending_in_x_code_names_the_code() -> None:
    assert code_in([url("https://scadbuddy.example/x/L-4?from=label")]) == Code("L", 4)


def test_a_url_whose_path_is_not_a_code_names_nothing() -> None:
    assert code_in([url("https://scadbuddy.example/filament/spools/abc")]) is None


def test_a_url_with_a_trailing_slash_still_names_the_code() -> None:
    assert code_in([url("https://scadbuddy.example/x/L-4/")]) == Code("L", 4)


def test_a_text_record_with_a_bare_code_names_it_in_any_case() -> None:
    assert code_in([text("s142")]) == Code("S", 142)


def test_a_utf16_text_record_names_its_code() -> None:
    assert code_in([text("S-142", encoding="utf-16")]) == Code("S", 142)


def test_a_utf16_text_record_with_a_little_endian_bom_names_its_code() -> None:
    data = "﻿S-142".encode("utf-16-le").hex()
    assert code_in([NdefRecord(record_type="text", encoding="utf-16", data_hex=data)]) == Code(
        "S", 142
    )


def test_bytes_that_are_not_text_in_their_encoding_are_skipped() -> None:
    bad = NdefRecord(record_type="text", encoding="utf-8", data_hex="ff")
    assert code_in([bad, text("L-4")]) == Code("L", 4)


def test_an_unknown_encoding_is_skipped() -> None:
    odd = NdefRecord(record_type="text", encoding="no-such-codec", data_hex="4c2d34")
    assert code_in([odd, text("L-4")]) == Code("L", 4)


def test_a_url_record_that_is_not_utf8_is_skipped() -> None:
    bad = NdefRecord(record_type="url", data_hex="ff")
    assert code_in([bad, text("L-4")]) == Code("L", 4)


def test_a_scadbuddy_spool_text_names_the_spool_id() -> None:
    assert code_in([text(f"scadbuddy:spool:{SPOOL_ID}")]) == SPOOL_ID


def test_a_scadbuddy_spool_text_is_read_in_any_case() -> None:
    assert code_in([text(f"SCADBUDDY:SPOOL:{str(SPOOL_ID).upper()}")]) == SPOOL_ID


def test_a_scadbuddy_text_that_is_not_a_spool_names_nothing() -> None:
    assert code_in([text("scadbuddy:spool:not-a-uuid"), text("scadbuddy:place:1")]) is None


def test_the_first_record_that_names_something_wins() -> None:
    assert code_in([text("hello"), url("https://h/x/L-4"), text("S-1")]) == Code("L", 4)


def test_other_record_types_are_skipped() -> None:
    mime = NdefRecord(record_type="mime", media_type="application/json", data_hex="4c2d34")
    assert code_in([mime]) is None


def test_no_records_name_nothing() -> None:
    assert code_in([]) is None


def test_a_url_record_that_is_not_a_url_is_skipped() -> None:
    # `urlsplit` raises on an unclosed IPv6 host; a tag's bytes never raise.
    bad = NdefRecord(record_type="url", data_hex=b"http://[abc".hex())
    assert code_in([bad, text("L-4")]) == Code("L", 4)


def test_a_url_record_with_a_host_that_is_not_nfkc_is_skipped() -> None:
    bad = NdefRecord(record_type="url", data_hex="http://ex℀ample.com/x/L-4".encode().hex())
    assert code_in([bad, text("L-4")]) == Code("L", 4)


@pytest.mark.parametrize(
    ("encoding", "data"),
    [("idna", b"xn--a-"), ("punycode", b"a-" + b"9" * 40), ("utf-7", b"+")],
)
def test_an_encoding_web_nfc_never_gives_is_skipped(encoding: str, data: bytes) -> None:
    # Only `utf-8` and `utf-16` come from Web NFC; a codec that raises a bare `UnicodeError`
    # (idna, punycode) must not reach the bytes at all.
    odd = NdefRecord(record_type="text", encoding=encoding, data_hex=data.hex())
    assert code_in([odd, text("L-4")]) == Code("L", 4)


@pytest.mark.parametrize("encoding", ["UTF-8", "utf8", "utf-16le", "utf-16be", "UTF-16"])
def test_the_web_nfc_encodings_are_read_under_any_spelling(encoding: str) -> None:
    codec = "utf-16-be" if encoding.lower() in {"utf-16", "utf-16be"} else encoding
    record = NdefRecord(record_type="text", encoding=encoding, data_hex="L-4".encode(codec).hex())
    assert code_in([record]) == Code("L", 4)
