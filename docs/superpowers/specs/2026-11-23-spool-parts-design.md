# Spool parts: shells and addons as tracked units (design)

Status: approved by the owner on 2026-11-23. Issue #7278. It builds on the inventory core, phase 2 of epic #6949 (#7067), and does not start before #7067 merges. Epic #6260 (filament) and #6949 (inventory).

## 1. Intent

A spool on a scale is more than filament. It is:

- the filament, wound on an **insert** (a cardboard or plastic core), sitting in
- a **shell** (a Bambu reusable spool, or a spool the shop printed), with
- zero or more **addons** (an AMS width adaptor, a back spacer for a 250 g mini).

Today ScadBuddy knows only the insert. Each spool, or its type, has one `filament_insert_types` row with one `empty_weight_g`. Grams left on a weighing is gross less that one weight (`filament/bambuddy_pull.py` `_core_g`). A refill in a reusable shell, or a mini with its adaptor, reads wrong by the weight of the parts it does not know.

Shells and addons are physical things. They move from spool to spool. The owner wants each one traceable in the inventory, with its location, cost and material, like any other part (owner, 2026-11-23).

## 2. Goals

- G1. Shells and addons are inventory **units** (one row per physical thing), of a built-in kind `spool_part`, each with an empty weight.
- G2. A part is **installed** on a spool and removed from it. Its history shows every spool it was on and every place it sat.
- G3. A spool's empty weight = its insert's `empty_weight_g` + the empty weight of every part installed on it. Every reading of grams left uses that sum.
- G4. Weighing a new, full spool checks the parts: `gross − label_weight_g` is compared with that sum, and a difference over a tolerance is flagged on the spool, never silently absorbed.
- G5. Inventory items carry `saleable`. A kind sets the default, and an item can override it. `spool_part` is not saleable by default.

## 3. Non-goals

- The cardboard insert stays a filament catalog (`filament_insert_types`). It is used up with its filament, so there is nothing to trace. Inventory spec §6.5 keeps the door open for it to become an item later.
- No scale hardware here. Weighing input is #7273, or the readings ScadBuddy already has.
- No Bambuddy-side model. Bambuddy has one `core_weight` per spool, and the SB→BB sync sends it the total empty weight (G3). Bambuddy is a sync target only (#7276).

## 4. Anti-goals

- A second inventory model in the filament tables (a `filament_spool_parts` table). Parts live in the inventory core and nowhere else.
- A spool's empty weight that silently disagrees with a full-spool weighing.

## 5. Design

- **Kind.** A built-in kind `spool_part`, `tracking = unit`, `saleable = false`. Its attribute schema documents `empty_weight_g` (integer, > 0) and `role` (`shell` / `addon`). Items under it are, for example, "Bambu reusable spool", "AMS mini adaptor" and "Mini back spacer". Each physical one is a `stock_units` row.
- **Install.** A part unit's `installed_in_unit_id` is the spool's unit (`filament_spools.unit_id`, inventory spec §6.4). Installing writes a `use` move with `entity_id` = the spool. Removing it clears the link, writes a `return` move and sets the unit's `location_id`. Only one spool holds a unit at a time. A unit already installed must be removed first, or the request moves it explicitly.
- **Empty weight.** `filament` exposes one function, the spool's empty weight with its parts. `grams_left`, the BB pull's core weight (`_core_g`), the scale paths and the SB→BB sync's `core_weight` all read it. A part's weight changes only on its item, or on a unit override (measured).
- **Full-spool check (G4).** On the first weighing of a spool marked added-full, compute `gross − label_weight_g − empty weight`. If its size is over the tolerance (a setting, default 10 g), the spool shows "parts don't add up: N g" with a fix link (add a part, or correct a weight). The gap is never booked as filament.
- **Saleable (G5).** `item_kinds.saleable` (bool) and a nullable `items.saleable` override, decided in #7067. Order fulfil, the storefront and the agent's sell tools refuse an item that is not saleable. Stock reports still count and value it.
- **UI.** The spool page lists its parts, with Add part (pick a unit in stock, or make one) and Remove. A part unit's page shows its spool history. The inventory list filters by kind.
- **Agent.** Tools to install and remove a part, at write tier. Reads go through the inventory tools.

## 6. Approaches

1. **Parts as inventory units, built on #7067 (chosen).** Traceability, location, cost and material come from the inventory core. The cost: it waits for #7067.
2. A filament-only parts table now, migrated into inventory later. It is faster, but it builds the duplicate model the inventory spec exists to prevent (anti-goal). Rejected.
3. Weights only: a list of part weights on a spool, with nothing tracked. The owner wants parts traceable. Rejected.

## 7. Left to the builder

- The tolerance's setting name and area, following the settings rules (every setting rememberable).
- Whether a unit's measured weight is a unit attribute or a column.
- Migrations follow the repo's rule (one new timestamped file each).
