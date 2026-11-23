# Catalog cost calculator and material-number stats (#7152)

Issue #7152, a gap of the §11 parity sweep (#6270, `docs/features/filament-parity.md`, rows
"Filament catalog with cost calculation" and "Material number stats"). Spec:
`docs/superpowers/specs/2026-11-14-filament-management-design.md` §5.2 (a type's `code` goes to
Bambuddy as `material_number`), §5.7 (metrics), §5.8 (price points), §11.

## What Bambuddy 1.2.6b1 has

SB holds no recording of either page. `backend/tests/bambuddy/recordings/openapi/routes.txt`
lists `POST /api/v1/filament-catalog/calculate-cost` (no schema recorded), and BB's
`FilamentResponse` (`openapi/sync-routes.json`) carries `cost_per_kg`, `spool_weight_g` and
`currency`. No BB route is keyed by `material_number`: BB's stats are a grouping of its spools
(`SpoolResponse.material_number`, `inventory-spools.json`). Live Bambuddy was not called. So SB
matches the substance the issue names, not BB's look.

## Rulings

- **R-7152-1. The calculator is a read.** `GET /filament/cost-per-kg` writes nothing, so it
  needs no `Idempotency-Key` and nudges no sync (`nudge_after_write` skips a GET). The money
  arithmetic is the server's, on `Decimal` (`filament/invoice_money.py`), never a float in the
  browser.
- **R-7152-2. The inputs and the sum.** `price` (what was paid for `quantity` spools, as
  `Money`: at least 0, four places at most), `tax` and `shipping` (`Money`, default 0),
  `quantity` (1 to 1000, default 1) and `net_weight_g` (grams of filament in one spool, 1 to
  100000, the `LabelWeight` bounds). `cost_per_kg` = (price + tax + shipping) / quantity /
  (net_weight_g / 1000), and `price_per_kg` = (price + tax) / quantity / (net_weight_g / 1000):
  the basis of a price point (§5.8: tax in, shipping out). Both round half up to four places, as
  `spool_cost_per_kg` does for an invoice line, which is the same sum with shipping in. A result
  past what `numeric(12,4)` holds (`Cost`, under 10^8) is a 422.
- **R-7152-3. One currency: the shop's.** The answer names `currency`, the shop's
  `orders_currency`, because a type's `cost_per_kg` is in it (as every metric's money is). The
  request takes no currency, so the result can never be saved in another one.
- **R-7152-4. Saving is a person's Save.** On the type page, the calculator's "Use as default
  cost" puts `cost_per_kg` in the type's cost field as an unsaved edit, as the price panel's
  button does (#6266 Decision 1; spec §5.8: nothing rewrites a type's cost without a person's
  click). Save writes it with the type's PATCH. The net weight starts as the draft's label weight.
  The calculator works on a new type too.
- **R-7152-5. A material number is a type code, case folded.** The key is `upper(code)` of the
  spool's type. Codes are unique only among unarchived types (`filament_spool_types_code`), so a
  key can hold one live type and any archived types of that code: `types` lists them all, with
  `archived`. The label is the live type's code, else the newest archived one's. Untyped spools
  are one row with key `null`, "No material number", as `/metrics` names "No type". A Bambuddy
  reading's `material_number` on an untyped spool is not used: that is the type proposals' job.
- **R-7152-6. What "used" counts.** The grams of the plates that ended from `since` to `until`
  (UTC days, both included), every outcome, archived spools included, a plate SB and Bambuddy
  both booked counted once: the plates `/filament/metrics` counts as consumption (spec §5.5 rule
  3, `metrics.plates`). `wasted_g` is the failed and cancelled part, as there. Readings, manual
  adjustments and drying are not use.
- **R-7152-7. What "on hand" counts.** The unarchived spools: their number, grams left and value
  at each spool's effective cost per kg, else `filament_default_cost_per_kg`; `null` when no spool
  has a cost. These are `/filament/metrics` stock's rules (`_value`).
- **R-7152-8. The route.** `GET /filament/metrics/material-numbers` with `since` and `until`,
  default the 180 days before today, 422 for a reversed range or one past three years (the
  `/metrics` rules, `_range`). A key is listed when it has a spool in stock or a plate in the
  range. Rows sort by grams on hand, most first, then label; the `null` row last.
- **R-7152-9. Where they show.** The stats are a section of the Metrics tab, after Consumption, read for the tab's
  range. The agent's filament tools read the metrics today, so it gets
  `filament_material_number_stats` (read). The calculator gets `filament_cost_per_kg` (read)
  too, so the agent works money out on the server's decimals and never in floats; to keep a
  result it sets the type's cost as before, at that tool's tier.

## Steps

1. Backend: `filament/cost_calc.py` (pure) and the route; `MetricsStore.material_numbers` and
   its route; tests in `tests/filament/test_cost_calc.py`, `tests/filament/test_metrics_store.py`
   and `tests/api/test_filament_metrics.py`.
2. Frontend: `api/filament.ts` calls, `components/filament/CostCalculator.tsx` on `TypePage`, a
   section of `MetricsTab`, msw mocks, vitest tests.
3. Agent: `filament_material_number_stats` and `filament_cost_per_kg` in `src/tools/filament.ts`,
   with tests.
4. Docs: `docs/features/spools-and-filament.md`, and the two parity rows to MATCH.
