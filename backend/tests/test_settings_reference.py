from __future__ import annotations

from scadbuddy.core.settings import Settings
from scadbuddy.tools.settings_reference import reference


def test_every_setting_is_listed_once() -> None:
    rows = reference().splitlines()[2:]
    listed = [row.split("`")[1] for row in rows]
    assert listed == [f"SCADBUDDY_{name.upper()}" for name in Settings.model_fields]


def test_the_database_url_reads_as_required() -> None:
    rows = {row.split("`")[1]: row for row in reference().splitlines()[2:]}
    assert rows["SCADBUDDY_DATABASE_URL"].endswith("| (required) | no |")
    assert rows["SCADBUDDY_RENDER_CONCURRENCY"].endswith("| `int` | `2` | restart |")


def test_an_optional_type_does_not_split_its_row() -> None:
    rows = {row.split("`")[1]: row for row in reference().splitlines()[2:]}
    assert rows["SCADBUDDY_PUBLIC_URL"].endswith("| `str \\| None` | (unset) | live |")


def test_the_temporal_address_reads_as_required() -> None:
    rows = {row.split("`")[1]: row for row in reference().splitlines()[2:]}
    assert rows["SCADBUDDY_TEMPORAL_ADDRESS"].endswith("| (required) | no |")
