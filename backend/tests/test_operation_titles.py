"""#3856: an operation's activity title names its subject, not its filter reference."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest

from scadbuddy.operations.activity import row_of


@pytest.mark.parametrize(
    ("kind", "subject", "title"),
    [
        ("create_project", "project-name:Shelf", "Create project “Shelf”"),
        ("create_project", "project-name:" + "x" * 50, f"Create project “{'x' * 40}…”"),
        ("library_file_delete", "library:12", "Library file delete #12"),
        ("library_file_move", "library-files", "Library file move (several files)"),
        ("library_folder_update", "library-folder:3", "Library folder update #3"),
        ("library_remove", "scad-library:BOSL2", "Library remove BOSL2"),
        # #4001: an all-digit OpenSCAD library is no Bambuddy file.
        ("library_remove", "scad-library:123", "Library remove 123"),
        # A removal recorded before #4001 shared `library:`; all digits read as a file.
        ("library_remove", "library:BOSL2", "Library remove BOSL2"),
        ("library_remove", "library:123", "Library remove #123"),
        ("print", "output:abc", "Print output:abc"),
    ],
)
def test_a_title_reads_its_subject(kind: str, subject: str, title: str) -> None:
    op = SimpleNamespace(id="o1", kind=kind, subject=subject, status="running")
    assert row_of(cast(Any, op)).title == title
