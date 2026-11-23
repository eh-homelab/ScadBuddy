# Make a type links an existing type (#7159)

The price book's "Make a type" (Filament → Types, `components/orders/FilamentsEditor.tsx`,
`components/filament/makeType.ts` `makeTypeFromBook`) made 10 duplicate spool types on prod on
2026-11-23 (issue #7159). Exact matching would not have caught them: the SUNLU rows said
material `PLA` while the types said `PLA+` (`SUNLU-PLA-SILK-*` against `SUNLU-PLA+-SILK-*`), and
the DEEPLEE rows said subtype `Silk` while the types said `Silk Dual`
(`DEEPLEE-PLA-SILK-BLACK-RED` against `DEEPLEE-PLA-SILK-DUAL-BLACK-RED`).

## Rulings (decided 2026-11-23; do not re-ask)

- **R-7159-1**: Before it makes a type, "Make a type" looks for unarchived types with the same
  brand (the catalog's key and alias match, as the filament catalogs match names) and the same
  colour (the RGB of `#RRGGBBAA`; the alpha is ignored). Those are the candidates, whatever their
  material, subtype or effect.
- **R-7159-2**: The candidates are ranked: an exact match on material, subtype, colour and effect
  first, then the rest. Each shows its code, material, subtype, label weight and spool count
  when that is available cheaply.
- **R-7159-3**: With at least one candidate, "Make a type" opens a choice: link one of the
  candidates (the exact match chosen first, else the first one) or make a new type anyway. With
  no candidate, it works as before.
- **R-7159-4**: Linking sets only that type's `cost_per_kg` from the row, the existing mechanism
  that links its price book row (`filament_id`). It changes no other field of the type.
- **R-7159-5**: No new backend route unless the existing types list cannot give the candidates.
  The existing API is preferred; a backend change, if one is needed, goes through the CRUD API.
- **R-7159-6**: If the price book can "Make a type" for several rows at once, each row gets the
  same check.

## What the code already gives

- The Types tab (`pages/filament/TypesTab.tsx`) already reads every type, the catalog names
  (materials, subtypes, brands) and each type's unarchived spool count. It passes the types to
  `FilamentsEditor`; it now passes the names and counts too. So R-7159-5 needs no backend change.
- `GET /filament/catalog/{kind}/resolve` (`resolveCatalog`) matches a name by key and alias. The
  brand, material, subtype and effect of the row resolve through it; a name that resolves to
  nothing adds nothing (the candidate search never writes the catalog).
- The price book has no bulk "Make a type": each row has its own button. R-7159-6 is met by the
  check running in the one path every click takes.
- `PATCH /filament/types/{id}` with `cost_per_kg` runs `types.py` `_link_price_book`: it links
  the book row of the **type's own identity** (material, subtype and brand names, RGB, alpha), and
  adds one when the book has none. For an exact candidate that is the clicked row. For a
  candidate whose material or subtype differs (the prod case), it is another row. The dialog says
  so before the link, and the answer names the row the type linked when it is not the clicked
  one.

## Tasks

1. `makeType.ts`: `rankCandidates(row, ids, types)` (pure: brand id and RGB filter, unarchived,
   exact match first) and `typeCandidates(row, types)` (resolves the row's brand, material,
   subtype and effect through `resolveCatalog`, adds nothing), and `linkTypeToBook(type, row)`
   (`PATCH` with `cost_per_kg` only). Unit tests first (`makeType.test.ts`): the SUNLU and
   DEEPLEE cases, alias brands, alpha ignored, archived and other-brand types left out, ranking,
   no catalog writes, and the patch body.
2. `components/filament/LinkTypeDialog.tsx`: the choice (radio list of candidates with swatch,
   code, material and subtype, label weight, spool count; "Link this type", "Make a new type",
   Cancel). `FilamentsEditor` looks for candidates on "Make a type" and opens it when there is
   one. Component tests first (`FilamentsEditor.test.tsx`): the dialog shows with the exact match
   chosen, linking patches only the cost, "Make a new type" makes one, no candidate makes one at
   once.
3. `TypesTab.tsx` passes `names` and `spoolsOf` to `FilamentsEditor`.
4. `pnpm gen:api`, the touched vitest files, `pnpm typecheck`, `pnpm lint`.
