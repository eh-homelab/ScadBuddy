# Filament type proposals (#6701)

Spec: `docs/superpowers/specs/2026-11-14-filament-management-design.md` §8.1 ("else SB
proposes a new type for a person to confirm"), §5.2 and §5.3.

## Goal

The Bambuddy import leaves most spools with no type. ScadBuddy groups the open spools that
need a type, proposes one type per group, and a person (or the assistant, after the
person says yes) confirms each group.

## Decisions

- R-6701-1: the group key is (brand_id, material_id, subtype_id, rgba, label_weight_g,
  material_number). A spool missing material, RGBA or label weight is not grouped. It is
  listed as "not enough to propose", with what is missing. `material_number` is the one
  in the spool's newest Bambuddy reading (`synced_from_bb` `data.bb`); a spool with no
  link has none.
- R-6701-2: the proposed name follows how types are named today. The types store names a
  type by its `code`, and the price book's "Make a type" builds it as
  `BRAND-MATERIAL-SUBTYPE-COLOUR` in upper case (`makeType.ts` `typeCodeOf`), with the
  colour name, else the hex. A group with a `material_number` proposes that number,
  because §8.1 says `material_number` carries the type code and the pull matches by it
  first. A code another type or another proposal holds gets `-<label weight>G`, then
  `-2`, `-3`. The person can edit the name before confirming.
- R-6701-3: the confirm, for each group, first re-checks the match (`_match` in
  `type_proposals.py`, as R-6701-8 limits it). If a type now matches, it uses that type
  instead of making a duplicate. It sets `type_id` on the group's spools that are still untyped and
  unarchived, drops any per-spool override that equals the type's value, and writes one
  `edited` ledger event per spool. The actor is the caller's, as other writes do. One
  transaction. A spool that gained a type meanwhile (or was archived, or changed its
  identity) is skipped and reported.
- R-6701-4: the confirm route is a write. The agent tool for it is on the write tier,
  with a touched.ts entry. The read is on the read tier. The assistant confirms only
  after the person says yes.
- R-6701-5: no migration, unless the store needs one. If it does, make a new
  timestamped file.
- R-6701-6: the UI is a "Create types" panel on the Needs-type filter. It shows a row per
  group with the count, a colour swatch, the fields and an editable name. "Create" is per
  row, plus "Create all". Below it is a list of the spools that cannot be grouped. It
  must work at a 390 px viewport.
- R-6701-7: MSW mocks and fixtures for the new routes, per `frontend/CLAUDE.md`.
- R-6701-8: in `proposals` and `confirm`, a type found by the group's `material_number`
  (`types.match(code=...)`, which matches by code alone) is used only when its brand,
  material, subtype, RGBA and label weight equal the group's key. Otherwise the match
  falls back to the identity match, or the confirm makes the new type. Groups can share a
  `material_number` and differ in colour or weight (Bambuddy's product code can be shared
  across colours), and each gets its own type, confirmed together or one at a time.
- R-6729-1 (#6729, finding 1): in `proposals`, a group with no `material_number` folds
  into the one group that has the same key fields (brand, material, subtype, RGBA, label
  weight) and a `material_number`, when exactly one such group exists. Otherwise the
  groups stay separate. `confirm`'s answer says, for each group, which type it landed on
  (`code`, `type_id`), whether that group made it (`made`) and whether this request made
  it (`new`). When a group joined a type that another group in the same request had just
  made (`new` and not `made`), the UI (`TypeProposals.tsx`) says so in a line, with the
  name the row sent, so a typed name is never dropped silently.
- R-6729-2 (#6729, finding 2): only a violation of the `filament_spool_types_code`
  constraint (`exc.diag.constraint_name`) becomes the 409. Any other unique violation
  propagates.
- R-6729-3 (#6729, finding 3): `TypeProposal.material_number` is capped at the same 128
  that `TypeProposalPick` uses. A longer Bambuddy code is proposed with no code, not cut
  short: a cut code could collide.
- R-6729-4 (#6729, finding 4): the agent tool's zod schema bounds `label_weight_g` at
  100000 and `spool_ids` at 5000 items, matching the backend.
- R-6729-5 (#6729, the PR's concern 2): `confirm` lets a spool with no `material_number`
  match a numbered pick only under the fold's own rule: among the open untyped spools now
  in the database, exactly one numbered group has the same five key fields, and it is the
  pick's. Otherwise that spool comes back as `changed`, as before.
- R-6729-6 (#6729, the PR's concern 4): the agent tool's `material_number` takes
  `.max(128)`, matching the backend.

## Builder's calls

- The new type takes the key fields, the colour name the group's spools share (else the
  most common one), and each other overridable value every spool of the group shares
  (category, effect, extra colours, diameter, density, temperatures, insert type). It
  takes no cost, so no price book row is written.
- A code that clashes at the confirm is a 409 for the whole confirm; nothing is written.
- The `material_number` lookup is a correlated subquery per untyped spool. The existing
  `filament_spool_events_spool_at (spool_id, at)` index serves it: on a synthetic ledger
  of 2,000 spools and 200,000 events (local Postgres 18, `EXPLAIN ANALYZE`), each lookup
  was a backward index scan of about 2 rows plus an incremental sort on `seq`. No new
  index (R-6701-5).

## Steps

1. Backend store `filament/type_proposals.py` and its tests (grouping, names, the match
   re-check, the race skip, overrides dropped, events).
2. Routes `GET /filament/type-proposals` and `POST /filament/type-proposals/confirm`,
   with API tests.
3. Agent tools `filament_type_proposals` (read) and `filament_type_proposals_confirm`
   (write), a touched.ts entry, tests.
4. Frontend: API client, MSW mocks, the `TypeProposals` panel on the Needs-type filter,
   vitest, one Playwright check in `e2e/filament.spec.ts`.
