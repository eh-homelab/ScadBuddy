"""The filament gauges (#6269 plan Decisions 3-5): `MetricsStore.snapshot()` set on the
API's registry, each label folded to its 20 largest values plus ``other`` and ``none``.

A label is ranked once and that ranking serves every gauge that has it: brands and
location kinds by stock grams, materials by stock grams plus every plate's grams, so a
material out of stock that was used keeps its name. Rows that map to one label set are
summed, except days of stock, where a folded set takes its soonest material's days."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping

from scadbuddy.core.metrics import Metrics
from scadbuddy.filament.metrics_models import Snapshot

#: Values of one label kept by name; the rest are summed as ``other``.
TOP = 20

#: The fold's own labels. A real value equal to one (any case) gets a ``*`` so it never
#: shares the fold's or the null series (#6269 R-T6-4).
_RESERVED = frozenset({"other", "none"})


def fold(totals: Mapping[str | None, float], top: int = TOP) -> frozenset[str | None]:
    """The ``top`` values with the largest totals (ties by name). Null is never ranked:
    `label` maps it to ``none`` on its own."""
    named = [value for value in totals if value is not None]
    named.sort(key=lambda value: (-totals[value], value))
    return frozenset(named[:top])


def label(value: str | None, kept: frozenset[str | None]) -> str:
    if value is None:
        return "none"
    if value not in kept:
        return "other"
    return value + "*" if value.lower() in _RESERVED else value


def _totals(pairs: Iterable[tuple[str | None, float]]) -> dict[str | None, float]:
    totals: dict[str | None, float] = defaultdict(float)
    for value, amount in pairs:
        totals[value] += amount
    return totals


def export(metrics: Metrics, snapshot: Snapshot) -> None:
    """Clear every filament gauge and set it from ``snapshot``: a value that left the
    snapshot leaves its series. `filament_metrics_up` is the caller's."""
    stock = snapshot.stock
    materials = fold(
        _totals(
            [(row[0], row[4]) for row in stock]
            + [(material, grams) for (material, _), grams in snapshot.used.items()]
            + [(material, 0.0) for material in snapshot.burn]
        )
    )
    brands = fold(_totals((row[1], row[4]) for row in stock))
    kinds = fold(_totals((row[2], row[4]) for row in stock))

    def mat(value: str | None) -> str:
        return label(value, materials)

    grams: dict[tuple[str, str, str], float] = defaultdict(float)
    spools: dict[tuple[str, str, str], float] = defaultdict(float)
    value: dict[str, float] = defaultdict(float)
    for material, brand, kind, count, row_grams, row_value in stock:
        key = (mat(material), label(brand, brands), label(kind, kinds))
        grams[key] += row_grams
        spools[key] += count
        if row_value is not None:
            value[mat(material)] += float(row_value)
    used: dict[tuple[str, str], float] = defaultdict(float)
    for (material, outcome), row_grams in snapshot.used.items():
        used[(mat(material), outcome)] += row_grams
    burn = _by_material(snapshot.burn.items(), mat)
    low = _by_material(snapshot.low_stock.items(), mat)
    reorder = _by_material(snapshot.reorder.items(), mat)
    days: dict[str, float] = {}
    for material, found in snapshot.days_of_stock.items():
        days[mat(material)] = min(found, days.get(mat(material), found))

    gauges = (
        metrics.filament_stock_grams,
        metrics.filament_stock_spools,
        metrics.filament_stock_value,
        metrics.filament_used_grams,
        metrics.filament_burn,
        metrics.filament_days_of_stock,
        metrics.filament_low_stock_spools,
        metrics.filament_reorder_types,
    )
    for gauge in gauges:
        gauge.clear()
    for key, amount in grams.items():
        metrics.filament_stock_grams.labels(*key).set(amount)
    for key, amount in spools.items():
        metrics.filament_stock_spools.labels(*key).set(amount)
    for material_label, amount in value.items():
        metrics.filament_stock_value.labels(material_label, snapshot.currency).set(amount)
    for used_labels, amount in used.items():
        metrics.filament_used_grams.labels(*used_labels).set(amount)
    for gauge, values in (
        (metrics.filament_burn, burn),
        (metrics.filament_days_of_stock, days),
        (metrics.filament_low_stock_spools, low),
        (metrics.filament_reorder_types, reorder),
    ):
        for material_label, amount in values.items():
            gauge.labels(material_label).set(amount)


def _by_material(
    rows: Iterable[tuple[str | None, float]], mat: Callable[[str | None], str]
) -> dict[str, float]:
    found: dict[str, float] = defaultdict(float)
    for material, amount in rows:
        found[mat(material)] += amount
    return found
