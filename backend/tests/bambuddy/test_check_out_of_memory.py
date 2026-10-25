"""A print check's out-of-memory log (#4598, #4632): its traceback at most once per
interval per subject, the record of when kept bounded. A unit test of
`print_run._log_out_of_memory`, outside `tests/api` so it needs no Temporal (#4695
review); the checks that reach it are `tests/api/test_print_library.py`'s."""

from __future__ import annotations

import logging
from functools import partial

import pytest

from scadbuddy.bambuddy import print_run


def test_an_out_of_memory_traceback_is_logged_again_past_the_interval_and_kept_bounded(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """#4632, #4659 review: past the interval a subject logs its traceback again, and an
    entry past it is dropped then, so the record stays bounded in a long-lived process."""
    interval = print_run.OUT_OF_MEMORY_TRACEBACK_INTERVAL
    steps = [(1000.0, "library:89"), (1001.0, "library:89"), (1002.0, "library:90")]
    steps.append((1000.0 + interval + 5, "library:89"))

    with caplog.at_level(logging.WARNING, logger=print_run.logger.name):
        for now, subject in steps:
            try:
                raise MemoryError
            except MemoryError:
                print_run._log_out_of_memory(
                    subject, "laying out the file", clock=partial(float, now)
                )

    assert [(r.__dict__["subject"], bool(r.exc_info)) for r in caplog.records] == [
        ("library:89", True),
        ("library:89", False),
        ("library:90", True),
        ("library:89", True),
    ]
    # 90's entry was past the interval when 89's traceback was logged again.
    assert set(print_run._out_of_memory_logged) == {"library:89"}
