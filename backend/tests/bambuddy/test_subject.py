"""`PrintSubject` (#1750): what a print is of, one key for an output and a library file."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from scadbuddy.bambuddy.subject import PrintSubject

OUTPUT = "a" * 32


def test_an_output_and_a_library_file_have_one_key_shape() -> None:
    output = PrintSubject.output(OUTPUT)
    library = PrintSubject.library(41)

    assert (output.kind, output.id, output.key) == ("output", OUTPUT, f"output:{OUTPUT}")
    assert (library.kind, library.id, library.key) == ("library", "41", "library:41")
    assert (output.output_id, output.file_id) == (OUTPUT, None)
    assert (library.output_id, library.file_id) == (None, 41)


def test_a_key_parses_back_to_its_subject() -> None:
    for subject in (PrintSubject.output(OUTPUT), PrintSubject.library(41)):
        assert PrintSubject.parse(subject.key) == subject


def test_a_run_subject_is_what_runs_were_keyed_by_before_1750() -> None:
    # A run's idempotency key, workflow id and event topic are built from this, so a
    # retry across the upgrade still finds its run (`runs.run_key`).
    assert PrintSubject.output(OUTPUT).run_subject == OUTPUT
    assert PrintSubject.library(41).run_subject == "library:41"
    assert PrintSubject.from_run_subject(OUTPUT) == PrintSubject.output(OUTPUT)
    assert PrintSubject.from_run_subject("library:41") == PrintSubject.library(41)


@pytest.mark.parametrize("key", ["", "output:", "library:", "library:x", "library:-1", "scad:1"])
def test_a_malformed_key_is_refused(key: str) -> None:
    with pytest.raises(ValueError, match="print subject"):
        PrintSubject.parse(key)


def test_a_library_subject_needs_a_file_id() -> None:
    with pytest.raises(ValidationError):
        PrintSubject(kind="library", id="abc")
