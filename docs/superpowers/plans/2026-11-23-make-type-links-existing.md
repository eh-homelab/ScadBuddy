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

### Follow-ups (#7344, decided 2026-11-23)

- **R-7344-1**: The open choice follows `types`. `FilamentsEditor` keeps the row and its
  resolved catalog ids (`bookRowIds`, read once at the click), and ranks the candidates from the
  current `types` at each render (`rankCandidates`). A reload while it is open adds, drops or
  updates candidates, and the chosen one falls back to the first when it is gone. With no
  candidate left (all archived or deleted), the choice closes and does not open again by itself.
  The catalog ids are not read again: a catalog change while the dialog is open is rare, and
  re-reading them on each reload would send three resolves per refresh. The click ranks on the
  newest `types` after its catalog reads, so a reload during them that leaves no candidate makes
  the type instead of opening an empty choice. A chosen type that goes away leaves the shown
  pick chosen, so its return moves nothing. `typeCandidates` (Task 1) is gone: the editor calls
  `bookRowIds` and `rankCandidates` itself.
- **R-7344-2**: `linksBookRow` stays, as the dialog's forecast only (the warning and the button's
  "Link" or "Set …'s cost" before the click). It copies `orders/store.py` `existing_filament`, and
  a comment at its one use says to change both together. After the click, the PATCH answer's
  `filament_id` decides the notice; a test pins that the notice follows the server when the
  forecast disagrees.
- **R-7344-3**: The Types tab's tests cover a candidate that differs only in alpha: the
  "opacity differs" wording, the warning, and the "Set …'s cost" button and notice.

## What the code already gives

- The Types tab (`pages/filament/TypesTab.tsx`) already reads every type, the catalog names
  (materials, subtypes, brands) and each type's unarchived spool count. It passes the types to
  `FilamentsEditor`; it now passes the names and counts too. So R-7159-5 needs no backend change.
- `GET /filament/catalog/{kind}/resolve` (`resolveCatalog`) matches a name by key and alias. The
  brand, material, subtype and effect of the row resolve through it; a name that resolves to
  nothing adds nothing (the candidate search never writes the catalog).
- The price book has no bulk "Make a type": each row has its own button. R-7159-6 is met by the
  check running in the one path every click takes.
- `PATCH /filament/types/{id}` with `cost_per_kg` runs `types.py` `_link_price_book`: it finds
  the book row of the **type's own identity** by `orders/store.py` `existing_filament` (material,
  subtype and brand names compared upper-cased, with no aliases; RGB; alpha; not the effect),
  gives it the cost, and adds one when the book has none. When those names and colour are the
  clicked row's (`makeType.ts` `linksBookRow`), that is the clicked row. Otherwise (the prod
  case: `PLA+` against `PLA`) it is another row, and the clicked row keeps no type. The dialog
  says so before the click, its button then reads "Set <code>'s cost" instead of "Link <code>",
  and the notice after it says the type is linked to its own price book row.

## Tasks

1. `makeType.ts`: `rankCandidates(row, ids, types)` (pure: brand id and RGB filter, unarchived,
   exact match first, `differs` names the rest), `typeCandidates(row, types)` (resolves the row's
   brand, material, subtype and effect through `resolveCatalog`, adds nothing), `linksBookRow`
   (the server's book-row match) and `linkTypeToBook(type, row)` (`PATCH` with `cost_per_kg`
   only). Unit tests first (`makeType.test.ts`): the SUNLU and DEEPLEE cases, a brand alias,
   alpha ignored in the filter, archived and other-brand types left out, ranking, no catalog
   writes, the book-row match, and the patch body.
2. `components/filament/LinkTypeDialog.tsx`: the choice (radio list of candidates with swatch,
   code, material and subtype, label weight, spool count, and what differs; "Link <code>" or
   "Set <code>'s cost", "Make a new type", Cancel). `FilamentsEditor` looks for candidates on
   "Make a type" and opens it when there is one. Component tests first, through the Types tab
   (`pages/filament/TypesTab.test.tsx`): the exact match chosen and linked with only its cost
   sent, an effect-only difference that still links, the prod case warned before and only its
   cost set, "Make a new type" makes one, Cancel makes none. A row with no candidate makes one at
   once (the tab's existing tests).
3. `TypesTab.tsx` passes `names` (archived catalog rows included) and `spoolsOf` to
   `FilamentsEditor`.
4. `pnpm gen:api`, the touched vitest files, `pnpm typecheck`, `pnpm lint`.
