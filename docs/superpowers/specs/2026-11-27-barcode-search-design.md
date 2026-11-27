# A scanned barcode searches property values (#8372)

Issue #8372, epic #7171. The owner approved this design on 2026-11-27.

## Intent and why

A spool carries the maker's barcode (an EAN or UPC). ScadBuddy can keep that barcode on the
spool type as the `gtin` property (#7026). Today a scan of that barcode resolves to nothing:
the scan page shows the text, `/x/<value>` says "No such code", and the swap page says that no
spool has the code. The person must know which property holds the barcode and search by hand.

With this change, a code that is not a ScadBuddy code becomes a filament search. The owner,
2026-11-27: "We can cheat and encode a query with the property and value. Multiple results
return the list just like it does today."

## Goals

- Each place that reads a code sends an unresolved code to the spool search: the scan page,
  the swap page's identify steps and `/x/<value>`.
- One match opens it. Several matches show the filtered list. No match shows the empty list
  with the value in the search box.
- The filament search learns a `key:value` token that matches only that property. A plain
  value still matches any property value (#7026).
- A spool type whose property matches brings its spools into the spool list.

## Non-goals

- Saving an unknown barcode onto a spool from the scan (follow-up issue).
- Inventory items (follow-up issue).
- Any change in `tools/go/core/internal/scan` or `tagreads` (c4f175d1's).
- Any server change or new route.

## Approaches

1. **Encode a query (picked; the owner's choice).** The reader turns the code into a search
   text and opens the existing spool search. The matcher learns `key:value`. No server change.
2. **A barcode lookup route.** The server resolves a barcode to spools or types. This needs a
   new Go route and duplicates the search the list already does in the browser. Rejected.
3. **Resolve in the scan service.** `internal/scan` looks the barcode up in the properties.
   That is c4f175d1's area, and it would give only the scan page the feature. Rejected.

## The design

### The `key:value` token

- A token is a `key:value` token when, in lower case, it matches
  `^([a-z][a-z0-9_]{0,63}):(.+)$`. The key pattern is the entity property key pattern.
- It matches a property with that key when the value, or one item of a list value, equals the
  token's value, in any case, after the stored value is trimmed. It is a whole-value match, not
  a substring: a barcode must not match a longer number that contains it.
- GTINs: when both sides are 8 to 14 digits, they compare padded to 14 digits with leading
  zeros, as GS1 defines the GTIN-14 form
  ([GS1 General Specifications, GTIN](https://www.gs1.org/standards/id-keys/gtin)). So the
  scanned EAN-13 `6975373321234` equals the stored `06975373321234`.
- A `key:value` token also matches as a plain token. A tag UID such as `a1:b2:c3:d4` has the
  shape of a `key:value` token, and the tag search (#7151) must still find it.
- A boolean property is never searched (as today).

### The scan query

`scanSearchQuery(text)` makes the search text of a code:

- 8 to 14 digits (an EAN, a UPC or a GTIN): `gtin:<digits>`.
- Any other text: the trimmed text, as a plain search.

The path is `/filament/spools?q=<query>&from=scan`, with the query encoded by
`encodeURIComponent`.

### What each reader does

- **`/x/<value>` (CodePage):** a 404 for a text that is not a ScadBuddy code shape
  (`^[A-Za-z]-?\d+$`, such as `S-142`, `L-4` or `K-3`) goes to the scan path with `replace`.
  A ScadBuddy code that is not found keeps "No such code".
- **The scan page (`ScanCode.tsx` `ScannedCodeView`):** an unresolved code shows a link
  "Search spools for <value>". It is a link, not a jump: an https address keeps its tap rule,
  and "Read again" does not jump (c4f175d1, fleet DM #588).
- **The swap page's identify steps (`IdentifySpool`):** the spool step stays on the page.
  An unresolved text switches to "Pick by hand" with the scan query in its search box. One
  match is named at once; several show the filtered list; none shows the empty list. The place
  step (`IdentifyPlace`) does not change: a barcode names no place.
- **The swap step's camera:** the spool step's camera reads EAN-13, EAN-8, UPC-A and UPC-E
  as well as QR codes, kept to the formats `BarcodeDetector.getSupportedFormats()` gives where
  the browser has it (`identify/camera.ts` `SPOOL_FORMATS`). The place step's camera reads QR
  codes only.
- **The dry-box home-return dialog (`HomeReturnDialog`):** it uses the same spool step
  (`IdentifySpool`), so a scanned barcode becomes the same "Pick by hand" search there.

### The spool list

- Each spool's search reads its own property values and its type's property values.
- With `from=scan`, the list waits for the spools and both property reads to settle (a failed
  read counts as settled). Then, if exactly one spool shows, it opens that spool's page with
  `replace`. Then it takes `from` out of the address, whatever the outcome, so a later edit of
  the search box never jumps.

### The agent

The agent's copy `agent/src/tools/filamentSearch.ts` gets the same matcher, and
`filament_spool_list` reads its types' property values too. Both copies run the shared
vectors in `test-fixtures/filament/search-vectors.json`.

## Decisions

- D1 (the owner): encode a query; reuse the filament search.
- D2 (R1): no new route and no server change. Reading showed that none is needed: the kind
  property read `GET /entity-kinds/{kind}/properties` already gives every spool's and type's
  values.
- D3 (R2): the scan page edit is one small task of its own, in `ScanCode.tsx` only.
- D4 (R3, my call on the marker): `from=scan` marks a scan-made search. Only it opens a single
  result. The list removes it after the first decision.
- D5 (R4, my call): the token syntax, the whole-value match, the case rule and the list rule
  are as above. Whole-value, because a substring match finds a barcode inside a longer number.
- D6 (my call): 8 to 14 digits compare as GTIN-14. Without it, a 13-digit scan never equals a
  14-digit stored GTIN.
- D7 (my call): a `key:value` token also matches as a plain token, so tag UIDs with colons and
  addresses with a scheme still match as before.
- D8 (my call): the scan query uses `gtin:` for 8 to 14 digits. The `gtin` key is the seeded
  spool type key for barcodes (`backend/scadbuddy/migrations/20261123T0017Z_entity_properties.sql`).
  Other text is a plain search.
- D9 (my call): a spool's search did not read its type's properties before this change; it does
  now, in the Spools tab, the quick find, "Pick by hand" and the agent.
- D10 (my call): the swap page searches in its own "Pick by hand" list and does not leave the
  page, so the wizard keeps its steps. "Pick by hand" keeps its own text match and adds the
  filament search (a spool shows when either matches).
- D11 (my call): `/x/<value>` and the swap step keep their "not found" sentences for a text with
  the ScadBuddy code shape. Only other text becomes a search.
