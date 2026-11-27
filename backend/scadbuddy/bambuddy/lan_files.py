"""The LAN start an archive became, named by the archive's file (#5661, #8281).

Bambuddy archives a print it did not queue under the name of the file the printer
printed (``services/archive.py`` ``archive_print``,
``filename=original_filename or source_file.name``, maziggy/bambuddy@2ce0d93). A LAN
start uploads its plate under a name that names the start (`LAN_FILE`), so the archive's
file name names its start. Two reads match it here, by one rule: the print's read
(`api/print_history.py` `_run_by_file`) and an order attempt's archive read
(`orders/archives.py` `link_lan_archives`). The match holds only when exactly one start
of one run of the archive's own subject has that name, whatever the run's status: never
a guess, and the match uses no time window. A start gets one archive only: the record
keeps a start's first archive (`print_links.record_lan_start_in`), and two archives of
one read that name one start match neither (`starts_by_files`, #8306).
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence

from scadbuddy.bambuddy.print_links import LanArchiveLink
from scadbuddy.bambuddy.runs import PrintRun

#: The file name a LAN start uploads (`workflows/printing.py` `_start_lan`):
#: ``scadbuddy-<run id[:8]>-p<plate>``, then ``-c<copy>`` past the first copy (together
#: the start's ``subtask_name``), then ``-<attempt>`` past the first attempt, then
#: ``.3mf``. A stem never ends in ``-<digits>``, so the attempt is read without doubt.
LAN_FILE = re.compile(r"(?P<subtask>scadbuddy-(?P<run>[0-9a-f]{8})-p\d+(?:-c\d+)?)(?:-\d+)?\.3mf")


def lan_subtask(filename: str | None) -> str | None:
    """The ``subtask_name`` of the start that uploaded ``filename``, or None for a file
    no LAN start uploaded."""
    named = LAN_FILE.fullmatch(filename) if filename else None
    return None if named is None else named["subtask"]


def start_by_file(
    archive_id: int, filename: str | None, runs: Iterable[PrintRun]
) -> tuple[PrintRun, LanArchiveLink] | None:
    """The run of ``runs`` whose LAN start archive ``archive_id`` became, and the link
    to record for it, from the archive's ``filename``. ``runs`` are the subject's runs
    with a start of that name (`PrintRunStore.lan_started`). None unless exactly one
    start of one run has the name."""
    named = LAN_FILE.fullmatch(filename) if filename else None
    if named is None:
        return None
    subtask = named["subtask"]
    found = [
        (run, start)
        for run in runs
        if run.id.startswith(named["run"]) and run.result is not None
        for start in run.result.lan
        if start.subtask_name == subtask
    ]
    if len(found) != 1:
        return None
    run, start = found[0]
    return run, LanArchiveLink(
        archive_id=archive_id, run_id=run.id, subtask_name=subtask, plate_id=start.plate_id
    )


def starts_by_files(
    filenames: Mapping[int, str | None], runs: Sequence[PrintRun]
) -> list[LanArchiveLink]:
    """`start_by_file` for many archives at once, keyed by archive id (#8306): the link
    of each archive whose start no other archive of ``filenames`` names. When two
    archives name one start (a failed LAN print printed again from the printer's own
    storage makes a second archive of the same file), neither matches: the file name
    cannot tell which one is the start's print."""
    named = [
        found[1]
        for archive_id, filename in filenames.items()
        if (found := start_by_file(archive_id, filename, runs)) is not None
    ]
    count = Counter((link.run_id, link.subtask_name) for link in named)
    return [link for link in named if count[(link.run_id, link.subtask_name)] == 1]
