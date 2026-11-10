"""The dashboards' operation panels pick the workers' kinds by name (#5298): the API ends
every operation's record and counts it, so a job no longer tells a print or flow kind
from a library one. The printing dashboard shows the print worker's kinds, and the
renders dashboard leaves them and the flow kinds out. A kind added to either worker
fails here until both lists name it."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, cast

from scadbuddy.bambuddy.operations import bambuddy_kinds_over
from scadbuddy.flows.operations import flow_kinds

GRAFANA = Path(__file__).resolve().parents[2] / "deploy" / "grafana"
OPERATION_SERIES = (
    "scadbuddy_operation_duration_seconds_bucket",
    "scadbuddy_operations_finished_total",
    "scadbuddy_operations_deferred_total",
)


def _kinds(queue: str) -> set[str]:
    none = cast(Any, None)
    kinds = bambuddy_kinds_over(
        settings_store=none,
        outputs=none,
        uploads=none,
        links=none,
        reprints=none,
        archive_cache=none,
        printers=none,
        reports=none,
    ) + flow_kinds(none, None, None)
    return {kind.name for kind in kinds if kind.queue == queue}


def _matchers(dashboard: str) -> list[tuple[str, set[str]]]:
    """Each operation series' kind matcher in ``dashboard``: its operator and names."""
    found: list[tuple[str, set[str]]] = []
    for panel in json.loads((GRAFANA / dashboard).read_text())["panels"]:
        for target in panel.get("targets", []):
            expr = target.get("expr", "")
            for series in OPERATION_SERIES:
                for selector in re.findall(re.escape(series) + r"\{([^}]*)\}", expr):
                    [(op, names)] = re.findall(r'kind(=~|!~)"([^"]*)"', selector)
                    found.append((op, set(names.split("|"))))
    return found


def test_the_printing_panels_show_the_print_workers_kinds() -> None:
    matchers = _matchers("scadbuddy-printing.json")
    assert len(matchers) == 5
    assert all(matcher == ("=~", _kinds("bambuddy")) for matcher in matchers)


def test_the_renders_panels_leave_out_every_workers_kinds() -> None:
    matchers = _matchers("scadbuddy-renders.json")
    assert len(matchers) == 5
    workers = _kinds("bambuddy") | _kinds("projects")
    assert all(matcher == ("!~", workers) for matcher in matchers)
