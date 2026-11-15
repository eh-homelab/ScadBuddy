"""#4699 — each automatic import as an activity row (`print_watch/activity.py`)."""

from __future__ import annotations

from scadbuddy.bambuddy.print_links import PrintImport
from scadbuddy.print_watch.activity import imported_row, row_of
from scadbuddy.print_watch.store import AutoImport


def test_an_inbox_print_is_imported_into_unsorted_imports() -> None:
    row = row_of(AutoImport(archive_id=7, print_name="demo_elan", state="inbox", reason="r"))
    assert (row.id, row.title, row.facets["state"]) == (
        "print_auto_import:7",
        "Imported print demo_elan into Unsorted imports",
        "inbox",
    )


def test_a_dismissed_print_says_so() -> None:
    """Review of #6028: a dismiss rewrites the row, so its title and its `state` facet
    say where the print is now."""
    row = row_of(AutoImport(archive_id=7, print_name="demo_elan", state="dismissed"))
    assert (row.id, row.title, row.facets["state"], row.status) == (
        "print_auto_import:7",
        "Dismissed print demo_elan from Unsorted imports",
        "dismissed",
        "succeeded",
    )


def test_an_import_rewrites_the_row_onto_its_model() -> None:
    """#6029: Import print of a dismissed (or inbox) archive rewrites its row, so the
    `state` facet and the title say the print is on its model now."""
    row = imported_row(
        AutoImport(archive_id=7, print_name="demo_elan", state="dismissed", reason="r"),
        PrintImport(archive_id=7, slug="keychain", source="archive", filename="k.3mf"),
    )
    assert (row.id, row.title, row.subject, row.status) == (
        "print_auto_import:7",
        "Imported print demo_elan onto keychain",
        "model:keychain",
        "succeeded",
    )
    assert row.facets == {"state": "linked", "source": "archive", "matched_by": "import"}
    assert (row.detail["filename"], row.detail["reason"]) == ("k.3mf", None)
