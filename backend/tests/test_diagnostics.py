"""The shared OpenSCAD diagnostic parser, as a render uses it (#252).

The editor check's own cases are in ``test_scad_check.py``; these cover what a
render adds: paths made relative to the model and its libraries, and a cap.
"""

from __future__ import annotations

from pathlib import Path

from scadbuddy.render.diagnostics import (
    MAX_DIAGNOSTICS,
    Diagnostic,
    DiagnosticCollector,
    parse_diagnostics,
)

MODEL = Path("/data/models/widget")
LIBRARY = Path("/data/libraries/BOSL2/0123abcd")


def test_a_located_message_names_its_file_and_line() -> None:
    assert parse_diagnostics(
        ["WARNING: Ignoring unknown variable 'wdith' in file model.scad, line 12"]
    ) == [
        Diagnostic(
            severity="warning",
            message="Ignoring unknown variable 'wdith'",
            file="model.scad",
            line=12,
        )
    ]


def test_an_absolute_path_is_named_from_the_model_or_library_root() -> None:
    diagnostics = parse_diagnostics(
        [
            f"ERROR: Assertion 'false' failed in file {MODEL}/parts/base.scad, line 3",
            f"WARNING: old_fn() is deprecated in file {LIBRARY}/BOSL2/std.scad, line 40",
            "WARNING: elsewhere in file /usr/share/openscad/libraries/MCAD/gears.scad, line 1",
        ],
        roots=[MODEL, LIBRARY],
    )

    assert [d.file for d in diagnostics] == [
        "parts/base.scad",
        "BOSL2/std.scad",
        "/usr/share/openscad/libraries/MCAD/gears.scad",
    ]


def test_trace_lines_and_unlocated_messages_are_kept() -> None:
    diagnostics = parse_diagnostics(
        [
            "ERROR: Assertion 'w > 0' failed in file model.scad, line 2",
            "TRACE: called by 'body' in file model.scad, line 5",
            "WARNING: Can't open library 'BOSL2/std.scad'.",
        ]
    )

    assert [(d.severity, d.line) for d in diagnostics] == [
        ("error", 2),
        ("trace", 5),
        ("warning", None),
    ]


def test_output_that_is_not_a_diagnostic_is_left_out() -> None:
    assert (
        parse_diagnostics(
            [
                'ECHO: "WARNING: not really"',
                "Total rendering time: 0:00:00.065",
                "Can't parse file 'model.scad'!",
                "",
            ]
        )
        == []
    )


def test_a_flood_is_capped_and_counted() -> None:
    collector = DiagnosticCollector()
    for index in range(MAX_DIAGNOSTICS + 5):
        collector.feed(f"WARNING: number {index}")
    collector.feed("ECHO: not counted")

    assert len(collector.diagnostics) == MAX_DIAGNOSTICS
    assert collector.dropped == 5
