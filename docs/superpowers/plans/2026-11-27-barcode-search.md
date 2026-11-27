# Barcode Search Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

> **UI work (Tasks 2, 3 and 4):** the implementer loads the `frontend-design:frontend-design`
> skill before writing any UI.

**Goal:** A scanned code that is not a ScadBuddy code (a maker's EAN on a spool) becomes a spool search that matches property values, and a single match from a scan opens at once.

**Architecture:** Browser and agent only, no server change. The filament matcher (both copies) keeps each property value with its key, learns a whole-value `key:value` token, and a spool's search reads its type's properties too. A new `lib/scanSearch.ts` turns a code into `/filament/spools?q=<query>&from=scan`; the Spools tab opens a single result only for `from=scan`. CodePage, the swap page's spool step and the scan page send unresolved codes there.

**Tech Stack:** React 19, TypeScript, vitest, msw; the agent's TypeScript tools and vitest.

**Spec:** `docs/superpowers/specs/2026-11-27-barcode-search-design.md` (binding). Issue #8372, epic #7171.

## Global Constraints

- No new route and no server change (R1). No change in `tools/go/core/internal/scan` or `tagreads` (R2).
- Do not change `frontend/src/pages/filament/ScanPage.tsx`, `WriteTag.tsx`, `lib/tagWrite.ts`, `NewTag.tsx`, `NfcBridge.cs` or the filament tag mocks (c4f175d1's).
- The two matcher copies (`frontend/src/lib/filamentSearch.ts`, `agent/src/tools/filamentSearch.ts`) stay identical in behaviour; every matcher case goes in `test-fixtures/filament/search-vectors.json`, which both tests run.
- Token syntax, exact: lower-cased token matching `^([a-z][a-z0-9_]{0,63}):(.+)$`.
- Fixed copy: the scan page link "Search spools for <value>".
- Scan path, exact: `` `/filament/spools?q=${encodeURIComponent(query)}&from=scan` ``.
- Tests never depend on runner speed or the date (R5). Use `findBy*`/`waitFor`, never a delay.
- Locally run only lint, types and the focused tests each task names (memory "Offload tests to CI"). Frontend vitest outside `pnpm test` needs `pnpm gen:api` first; the agent likewise.
- Simplified Technical English in docs, comments and copy. No counts in docs.

## Review Focus

1. A tag UID with colons (`a1:b2:c3:d4`) has the `key:value` shape: it must still find its tagged spool. Task 1's vectors pin it.
2. A scanned 13-digit EAN and a stored 14-digit GTIN with a leading zero must match; a part of a stored GTIN (`gtin:7041234`, `gtin:0697533704123`) must not match it. Task 1's vectors pin both.
3. `from=scan` with one result: Back from the opened spool must go to the page before the search, and a later edit of the search box to one result must not jump. Task 2 tests both.
4. `from=scan` while the property reads are still pending: the list must not decide on a partial answer (zero or one by accident). Task 2 tests a held property read.
5. A typed ScadBuddy code that is not found (`S-999` on `/x/S-999` or in the swap step) keeps its "not found" sentence, never a search. Task 3 tests both.

---

### Task 1: The matcher learns `key:value`, and a spool reads its type's properties

**Files:**
- Modify: `frontend/src/lib/filamentSearch.ts`, `agent/src/tools/filamentSearch.ts`
- Modify: `test-fixtures/filament/search-vectors.json` (its `comment` too)
- Modify (types follow): `frontend/src/components/entities/api.ts` (`readKindPropertyTexts`, `loadKindPropertyTexts`), `frontend/src/pages/filament/SpoolsTab.tsx`, `frontend/src/pages/filament/TypesTab.tsx`, `frontend/src/components/filament/FilamentQuickFind.tsx`, `agent/src/tools/filament.ts`
- Test: `frontend/src/lib/filamentSearch.test.ts`, `agent/test/filamentSearch.test.ts`

**Interfaces:**
- Produces (both copies): `interface PropertyText { key: string; text: string }`.
- Produces: `propertyTexts(values) => PropertyText[]` (was `string[]`): a string as it is, each list item, a number as text, booleans left out; each with its key.
- Produces: `SearchDoc.properties?: readonly PropertyText[]`; `spoolSearchDoc` and `typeSearchDoc` put `extras.properties` there and no longer in `texts`.
- Produces: `SpoolExtras.typeProperties?: readonly PropertyText[]`; `spoolSearchDoc` puts both `properties` and `typeProperties` in `doc.properties`.
- Produces: `SearchToken.property?: { key: string; value: string }`, set by `parseSearch` for a `key:value` token; `text` stays the whole token.
- Produces (frontend): `readKindPropertyTexts(kind)` and `loadKindPropertyTexts(kind)` give `Map<string, PropertyText[]>`; the agent's `kindPropertyTexts` likewise.

Matching rules for `matchesToken`:
- A plain token also matches when some `doc.properties[].text` contains it (case-insensitive), as the texts did.
- A token with `property` matches when some property with that key has `text.trim()` equal to the value in any case, or both are 8 to 14 digits and equal after `padStart(14, '0')`. It also matches by every plain rule on its whole `text` (spec D7).

- [ ] **Step 1: Add the failing vectors.** In `search-vectors.json`, a spool group may give `typeProperties` (raw values, as `properties`). Add to "a type found by its property values (#7026)":

```json
{ "query": "gtin:06975337041234", "match": true },
{ "query": "GTIN:06975337049999", "match": true },
{ "query": "gtin:6975337041234", "match": true },
{ "query": "gtin:0697533704123", "match": false },
{ "query": "gtin:7041234", "match": false },
{ "query": "mpn:a00-k0", "match": true },
{ "query": "mpn:a00", "match": false },
{ "query": "td:1.8", "match": true },
{ "query": "food_safe:true", "match": false },
{ "query": "asin:06975337041234", "match": false },
{ "query": "gtin:06975337041234 black", "match": true },
{ "query": "gtin:06975337041234 white", "match": false }
```

Add a group "a spool is found by its type's property values (#8372)": a spool with `"properties": { "lot_number": "L-2207" }` and `"typeProperties": { "gtin": ["06975337041234"] }`, cases `gtin:6975337041234` true, `06975337041234` true, `lot_number:l-2207` true, `gtin:l-2207` false, `lot_number:06975337041234` false. To the tagged spool group add `a1:b2:c3:d4` false and, with a tag `A1B2C3D4E5`, a new case `a1:b2:c3:d4` true (change that group's tags or add a group; keep every old case).

- [ ] **Step 2: Make both harnesses pass `typeProperties`** through `propertyTexts` into `spoolSearchDoc`'s extras. Run `cd frontend && pnpm exec vitest run src/lib/filamentSearch.test.ts` and `cd agent && pnpm exec vitest run test/filamentSearch.test.ts`. Expected: FAIL on the new cases.

- [ ] **Step 3: Implement the matcher** in both copies with the Interfaces above. Update both header comments for `key:value`, the whole-value rule, the GTIN-14 rule and type properties.

- [ ] **Step 4: Follow the type change** in every caller listed under Files. The agent's `filament_spool_list` reads `kindPropertyTexts(backend, 'spool_type')` too (with `orElse`) and passes `typeProperties: typeProps.get(spool.type_id)`; its description and `filament_type_list`'s name the `key:value` form (such as `gtin:06975337041234`). The frontend callers keep their own reads for now (Task 2 adds type properties to the spool list and the quick find).

- [ ] **Step 5: Add an agent test** in `agent/test/filamentSearch.test.ts`: `filament_spool_list` with `q: 'gtin:6975337041234'` lists the spool whose type `t2` has `gtin: ['06975337041234']` (the msw handler there already serves it) and no other.

- [ ] **Step 6: Verify.** Both vitest commands above PASS; `cd frontend && pnpm lint && pnpm exec tsc -p tsconfig.app.json --noEmit`; `cd agent && pnpm lint && pnpm typecheck`.

- [ ] **Step 7: Commit** `feat(filament): the search matches a property by key:value, and a spool by its type's properties (#8372)`.

### Task 2: The spool list reads type properties and opens a scan's single match

**Files:**
- Create: `frontend/src/lib/scanSearch.ts`, `frontend/src/lib/scanSearch.test.ts`
- Modify: `frontend/src/pages/filament/SpoolsTab.tsx`, `frontend/src/components/filament/FilamentQuickFind.tsx`, `frontend/src/components/filament/spoolsQuery.ts` (only if `from` must survive `readQuery`), `frontend/src/mocks/features/entities.ts` (seed values)
- Test: the Spools tab's test (find it with `git grep -l "SpoolsTab" frontend/src/**/*.test.tsx`), `FilamentQuickFind.test.tsx`

**Interfaces:**
- Consumes: Task 1's `PropertyText`, `SpoolExtras.typeProperties`, `loadKindPropertyTexts`.
- Produces: `scanSearchQuery(text: string): string` (8 to 14 digits after trim: `gtin:<digits>`; else the trimmed text), `scanSearchPath(text: string): string` (the Global Constraints path), `isScadBuddyCode(text: string): boolean` (`/^[a-z]-?\d+$/i` on the trimmed text).

- [ ] **Step 1: Write `scanSearch.test.ts`:** `scanSearchQuery('6975373321234')` is `gtin:6975373321234`; `scanSearchQuery(' 12345678 ')` is `gtin:12345678`; `scanSearchQuery('1234567')` and `scanSearchQuery('ABC 12')` stay as trimmed text; `scanSearchPath('a b&c')` is `/filament/spools?q=a%20b%26c&from=scan`; `isScadBuddyCode` is true for `S-142`, `l4`, `K-3` and false for `6975373321234` and `https://x`.

- [ ] **Step 2: Implement `scanSearch.ts`**; run the test; PASS.

- [ ] **Step 3: Write the failing Spools tab tests** (msw; seed `entityMock.properties` with a type's `gtin`):
  - `?q=gtin:6975373321234&from=scan` with one spool of that type: the spool page opens, and the history entry was replaced (a `MemoryRouter` with a start entry before the list: going back lands on it).
  - The same with two spools of that type: the list shows both, the address no longer has `from`, and typing in the search box until one spool shows does not open it.
  - The same with none: the list is empty and the search box holds `gtin:6975373321234`.
  - The type property read held (an msw handler that waits on a promise the test resolves): the page does not open the spool and shows no empty answer as final until the test releases the read; then it opens it.
  - Without `from=scan`, one match does not open.

- [ ] **Step 4: Implement in `SpoolsTab.tsx`:** read `loadKindPropertyTexts('spool_type')` beside the spool property read on every load and pass `typeProperties: typeProps.get(spool.type_id)`. Track when the list, the spool property read and the type property read have each settled (a failure settles). With `from=scan`, once all three have settled, decide once: exactly one shown spool → `navigate(filamentLinks.spool(id), { replace: true })`; else remove `from` with `setParams(..., { replace: true })`. `FilamentQuickFind.tsx` passes its `typeProperties` as `typeProperties` of each spool.

- [ ] **Step 5: Seed the mocks:** in `entities.ts` `fresh()`, give one seeded spool type that has spools a `gtin` list (read the seeded types in `mocks/fixtures.ts` or the filament mock to pick one), so the mocked app finds it by barcode.

- [ ] **Step 6: Verify:** the Spools tab test, `FilamentQuickFind.test.tsx`, `scanSearch.test.ts` PASS; `pnpm lint`; the quick typecheck.

- [ ] **Step 7: Commit** `feat(filament): a scan-made spool search opens its one match (#8372)`.

### Task 3: `/x/<value>` and the swap page's spool step search an unresolved code

**Files:**
- Modify: `frontend/src/pages/CodePage.tsx`, `frontend/src/components/filament/IdentifySpool.tsx`, `frontend/src/components/filament/identify/CodeWay.tsx`
- Test: `frontend/src/pages/CodePage.test.tsx`, `frontend/src/components/filament/IdentifySpool.test.tsx`

**Interfaces:**
- Consumes: Task 2's `scanSearchPath`, `scanSearchQuery`, `isScadBuddyCode`; Task 1's matcher and `loadKindPropertyTexts`.
- Produces: `CodeWay`'s `fromText` may return `{ search: string }`; `CodeWay` then calls a new optional prop `onSearch(text: string)` and shows nothing itself. `IdentifyPlace` passes no `onSearch` and never returns it.

- [ ] **Step 1: Write the failing tests.**
  - `CodePage.test.tsx`: `/x/6975373321234` answered 404 lands on `/filament/spools?q=gtin%3A6975373321234&from=scan` (or the decoded `q` equals `gtin:6975373321234` and `from` is `scan`), with `replace`; `/x/S-999` answered 404 still shows "No such code: S-999".
  - `IdentifySpool.test.tsx`: in the QR code way, typing `6975373321234` (resolve 404) switches to "Pick by hand" with the search box holding `gtin:6975373321234`; with one spool whose type has that GTIN, `onIdentified` gets it at once; with two, both show and none is named; with none, the list is empty with the value in the box. Typing `S-999` (404) still says "No spool has the code S-999." Editing the box afterwards to one spool does not name it.

- [ ] **Step 2: Implement CodePage:** on a 404 for a code that is not `isScadBuddyCode`, `<Navigate to={scanSearchPath(code)} replace />`.

- [ ] **Step 3: Implement IdentifySpool:** `spoolFromText` returns `{ search: scanSearchQuery(text) }` for a 404 on a text that is not a ScadBuddy code shape and not a `scadbuddy:spool:` label. `IdentifySpool` keeps the search in state and switches to `HandWay` with it (`HandWay` gets `initialSearch?: string`, `fromScan?: boolean`). `HandWay` reads `loadKindPropertyTexts('spool')` and `('spool_type')`; a spool shows when its old `searchText` match passes or `matchesSearch(spoolSearchDoc(spool, { properties, typeProperties }), parseSearch(search))` does. With `fromScan`, once the spools and both reads have settled, exactly one shown spool is picked and named once; after that first decision it never picks by itself again.

- [ ] **Step 4: Verify:** both test files PASS; `pnpm exec vitest run src/pages/filament/SwapPage.test.tsx` still PASSES; lint; quick typecheck.

- [ ] **Step 5: Commit** `feat(filament): an unresolved code on /x and the swap page searches the spools (#8372)`.

### Task 4: The scan page links an unresolved code to the search

R2: this task is small and separate so c4f175d1 can take it; c4f175d1 agreed we make it (fleet DM #588).

**Files:**
- Modify: `frontend/src/components/filament/scan/ScanCode.tsx` (`ScannedCodeView`, the else branch only)
- Test: `frontend/src/components/filament/scan/ScanCode.test.tsx`

**Interfaces:**
- Consumes: Task 2's `scanSearchPath`.

- [ ] **Step 1: Write the failing test:** a scan with an unresolved `EAN_13` code `6975373321234` shows a link named "Search spools for 6975373321234" whose `href` is `scanSearchPath('6975373321234')`; a resolved code shows no such link; an https text shows both "Open this address" and the search link; nothing navigates until a tap (the location stays the scan page after render and after "Read again").

- [ ] **Step 2: Implement:** a router `Link` to `scanSearchPath(code.text)` with a search icon, under the text, in the else branch.

- [ ] **Step 3: Verify:** `ScanCode.test.tsx` PASSES; lint; quick typecheck.

- [ ] **Step 4: Commit** `feat(filament): the scan page links an unknown code to the spool search (#8372)`.

---

## Self-review

- Spec coverage: the token (Task 1), type properties reach spools (Tasks 1 and 2), the scan query and path (Task 2), one/several/none and `from=scan` (Task 2), `/x` and the swap step (Task 3), the scan page (Task 4), the agent (Task 1). Non-goals have no task.
- Types: `PropertyText`, `typeProperties`, `scanSearchQuery`, `scanSearchPath`, `isScadBuddyCode`, `onSearch` and `{ search }` are named the same in every task.
- Review Focus: each line has its test in the owning task.
