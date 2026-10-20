"""`scadbuddy.render.zipcopy`: the raw copy the stamp and the re-placement both use."""

from __future__ import annotations

import sys

from scadbuddy.render import zipcopy


def test_the_raw_copy_is_checked_on_this_python() -> None:
    """#2910: copy_raw writes through zipfile's private state. A Python minor bump fails
    here until the raw-copy tests of its callers are run on it and RAW_COPY_CHECKED is
    moved (#3439). They are, in `tests/test_provenance.py` (the stamp):

    - ``test_every_other_part_is_copied_as_its_compressed_bytes``

    in `tests/test_bambu3mf.py`, class ``TestReplate`` (the re-placement):

    - ``test_replating_copies_an_untouched_entry_as_it_stands``
    - ``test_replating_is_what_writing_for_that_plate_would_have_produced``

    and in `tests/test_recolour.py`:

    - ``test_recolouring_copies_what_it_does_not_change_and_writes_nothing_to_disk``

    Until then every caller reads and writes each entry again."""
    assert sys.version_info[:2] == zipcopy.RAW_COPY_CHECKED
