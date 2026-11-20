"""The filament gauges (#6269 Task 6): the snapshot folded to at most 20 values a label
and set on the API's registry."""

from __future__ import annotations

from decimal import Decimal

import pytest

from scadbuddy.core.metrics import Metrics
from scadbuddy.filament import metrics_export
from scadbuddy.filament.metrics_models import Snapshot


def _snapshot(**over: object) -> Snapshot:
    body: dict[str, object] = {
        "stock": [],
        "used": {},
        "burn": {},
        "days_of_stock": {},
        "low_stock": {},
        "reorder": {},
        "currency": "USD",
    }
    body.update(over)
    return Snapshot.model_validate(body)


def _samples(metrics: Metrics, name: str) -> dict[tuple[tuple[str, str], ...], float]:
    """Each sample of gauge ``name`` by its sorted labels."""
    found: dict[tuple[tuple[str, str], ...], float] = {}
    for family in metrics.registry.collect():
        for sample in family.samples:
            if sample.name == name:
                found[tuple(sorted(sample.labels.items()))] = sample.value
    return found


def _brand_values(metrics: Metrics, name: str) -> set[str]:
    return {dict(labels)["brand"] for labels in _samples(metrics, name)}


def test_brands_past_twenty_fold_into_other() -> None:
    # Brand b01 has 1 kg ... b25 25 kg: the five smallest are b01..b05.
    stock = [("PLA", f"b{i:02d}", "Shelf", 1, float(i * 1000), None) for i in range(1, 26)]
    metrics = Metrics()
    metrics_export.export(metrics, _snapshot(stock=stock))
    grams = _samples(metrics, "scadbuddy_filament_stock_grams")
    by_brand = {dict(labels)["brand"]: value for labels, value in grams.items()}
    assert len(by_brand) == 21
    assert by_brand["other"] == sum(i * 1000 for i in range(1, 6))
    assert {f"b{i:02d}" for i in range(6, 26)} <= set(by_brand)
    assert sum(by_brand.values()) == sum(i * 1000 for i in range(1, 26))


def test_fold_keeps_the_largest_and_never_ranks_null() -> None:
    kept = metrics_export.fold({None: 9e9, "a": 3.0, "b": 2.0, "c": 1.0}, top=2)
    assert kept == frozenset({"a", "b"})
    assert metrics_export.label("c", kept) == "other"
    assert metrics_export.label("a", kept) == "a"


def test_null_label_is_none() -> None:
    assert metrics_export.label(None, frozenset()) == "none"
    metrics = Metrics()
    metrics_export.export(
        metrics,
        _snapshot(
            stock=[(None, None, None, 2, 500.0, Decimal("12.50"))],
            low_stock={None: 1},
        ),
    )
    grams = _samples(metrics, "scadbuddy_filament_stock_grams")
    assert grams == {(("brand", "none"), ("location_kind", "none"), ("material", "none")): 500.0}
    value = _samples(metrics, "scadbuddy_filament_stock_value")
    assert value == {(("currency", "USD"), ("material", "none")): 12.5}
    low = _samples(metrics, "scadbuddy_filament_low_stock_spools")
    assert low == {(("material", "none"),): 1.0}


def test_stock_spools_folds_with_the_stock_grams_brands() -> None:
    # b01 has the most spools (50) but the fewest grams, so it folds into `other` in
    # both gauges: the brands are ranked once, by grams.
    stock = [("PLA", "b01", "Shelf", 50, 10.0, None)]
    stock += [("PLA", f"b{i:02d}", "Shelf", 1, float(i * 1000), None) for i in range(2, 26)]
    metrics = Metrics()
    metrics_export.export(metrics, _snapshot(stock=stock))
    grams_brands = _brand_values(metrics, "scadbuddy_filament_stock_grams")
    spools_brands = _brand_values(metrics, "scadbuddy_filament_stock_spools")
    assert grams_brands == spools_brands
    assert len(spools_brands) == 21
    assert "b01" not in spools_brands
    spools = _samples(metrics, "scadbuddy_filament_stock_spools")
    other = next(v for labels, v in spools.items() if dict(labels)["brand"] == "other")
    assert other == 50 + 4  # b01..b05


def test_export_clears_a_gone_material() -> None:
    metrics = Metrics()
    both = _snapshot(
        stock=[
            ("PLA", "Bambu", "Shelf", 1, 900.0, Decimal("20")),
            ("PETG", "Bambu", "Shelf", 1, 800.0, Decimal("18")),
        ],
        used={("PLA", "completed"): 100.0, ("PETG", "failed"): 5.0},
        burn={"PLA": 3.0, "PETG": 1.0},
        days_of_stock={"PLA": 300.0, "PETG": 800.0},
        low_stock={"PLA": 0, "PETG": 0},
        reorder={"PLA": 0, "PETG": 1},
    )
    metrics_export.export(metrics, both)
    text = metrics.exposition().decode()
    assert 'material="PETG"' in text
    pla = _snapshot(
        stock=[("PLA", "Bambu", "Shelf", 1, 900.0, Decimal("20"))],
        used={("PLA", "completed"): 100.0},
        burn={"PLA": 3.0},
        days_of_stock={"PLA": 300.0},
        low_stock={"PLA": 0},
        reorder={"PLA": 0},
    )
    metrics_export.export(metrics, pla)
    text = metrics.exposition().decode()
    assert 'material="PETG"' not in text
    assert 'scadbuddy_filament_used_grams{material="PLA",outcome="completed"} 100.0' in text


def test_a_failed_export_keeps_the_last_good_gauges(monkeypatch: pytest.MonkeyPatch) -> None:
    metrics = Metrics()
    metrics_export.export(
        metrics,
        _snapshot(
            stock=[("PETG", "Bambu", "Shelf", 1, 800.0, Decimal("18"))],
            reorder={"PETG": 1},
        ),
    )

    def boom(*_: object) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(metrics.filament_reorder_types, "labels", boom)
    with pytest.raises(RuntimeError):
        metrics_export.export(
            metrics,
            _snapshot(stock=[("PLA", "Bambu", "Shelf", 1, 900.0, None)], reorder={"PLA": 1}),
        )
    stock = _samples(metrics, "scadbuddy_filament_stock_grams")
    assert {dict(labels)["material"] for labels in stock} == {"PLA", "PETG"}
    assert 'material="PETG"' in metrics.exposition().decode()
    assert next(v for k, v in stock.items() if dict(k)["material"] == "PETG") == 800.0


def test_days_of_stock_of_folded_materials_is_the_soonest() -> None:
    # 22 materials: m01 and m02 have the least stock and fold into `other`.
    stock = [(f"m{i:02d}", "Bambu", "Shelf", 1, float(i * 100), None) for i in range(1, 23)]
    days = {f"m{i:02d}": float(i * 10) for i in range(1, 23)}
    burn = {f"m{i:02d}": 1.0 for i in range(1, 23)}
    metrics = Metrics()
    metrics_export.export(metrics, _snapshot(stock=stock, days_of_stock=days, burn=burn))
    found = _samples(metrics, "scadbuddy_filament_days_of_stock")
    assert found[(("material", "other"),)] == 10.0
    rates = _samples(metrics, "scadbuddy_filament_burn_grams_per_day")
    assert rates[(("material", "other"),)] == 2.0


def test_worker_roles_do_not_export_filament() -> None:
    assert "scadbuddy_filament_" not in Metrics(role="print").exposition().decode()


def test_a_real_value_named_like_a_fold_label_never_shares_its_series() -> None:
    kept = frozenset({"other", "None", "a"})
    assert metrics_export.label("other", kept) == "other*"
    assert metrics_export.label("None", kept) == "None*"
    assert metrics_export.label("a", kept) == "a"
    assert metrics_export.label(None, kept) == "none"
    metrics = Metrics()
    stock = [("PLA", "other", "Shelf", 1, 500.0, None), ("PLA", None, "Shelf", 1, 100.0, None)]
    metrics_export.export(metrics, _snapshot(stock=stock))
    assert _brand_values(metrics, "scadbuddy_filament_stock_grams") == {"other*", "none"}
