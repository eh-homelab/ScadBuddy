---
name: filament-intake
description: Use when adding purchased filament to the spool inventory from receipts or invoices (Bambu Lab, Best Buy, Micro Center, Amazon), when spool cost should include sales tax, when ScadBuddy's price book or job cost needs the new spool prices, when downloading or importing 3dfilamentprofiles.com slicer profiles, or when a spool needs per-nozzle presets for the H2C. Also when 3dfilamentprofiles answers 429 "Vercel Security Checkpoint", Micro Center answers 403, or a Bambu RFID spool duplicates a manually added one.
---

# Filament intake

Receipts become spools with tax-inclusive cost. Non-Bambu filaments get
3dfilamentprofiles (3DFP) profiles for every nozzle size. Then ScadBuddy's
price book takes the new prices, so quotes and job cost use them. Two scripts do
the mechanical part. The judgment is in reading the receipts and matching them
against what is already in inventory.

- `scripts/intake.py`: `apply` turns receipt lines into spools; `link` imports profiles and wires the per-nozzle presets. Run `--help`; both take `--dry-run`.
- `scripts/3dfp.mjs`: `login`, `search`, `add`, `export` against 3DFP. Usage is in the header.

Where the spools live today: Bambuddy (BB) owns the spool inventory, and
ScadBuddy (SB) reads it. SB's overlay refreshes every
`spool_overlay_refresh_seconds` (300 s, `backend/scadbuddy/core/settings.py`),
and SB's price book (`filaments`) is filled from BB spools only on request
(step 8). So the scripts write to BB.

BB is `https://bambuddy.internal.nullreference.io` (`BAMBUDDY` overrides it).
Write endpoints need no API key on this instance. Printer model string is `H2C`.

## Workflow

1. **Read every receipt, then diff against inventory before creating anything.**
   Dump the inventory with `curl -s $B/api/v1/inventory/spools?include_archived=true`.
   Spools already exist from RFID auto-create (`data_origin: rfid_auto`) and
   from earlier manual entry.
   - A spool with `cost_per_kg: null` that matches a line is that purchase: use `"update": <id>`.
   - A spool that already has a cost belongs to an earlier purchase, and its note says which one. A matching line is a *new* spool.
   - A qty-N line that covers one uncosted spool plus new ones becomes two entries with the same price/tax/qty: one `"update"`, and one create with `"spools": N-1`.
   - Stop and ask the user when it's ambiguous, e.g. a costed spool with no note; don't guess between "same spool" and "second spool".
2. **Compute cost per spool** as (line subtotal + that line's tax) ÷ qty.
   - Bambu and Best Buy print tax per line; use their numbers.
   - When only total tax is printed (Micro Center), the rate is total tax ÷ subtotal (Miami-Dade is 7.0%). Apply it per line and set `"tax_basis": "receipt-wide 7.0%"` so the note says so.
   - No SKU on the receipt: put the product name in `sku`. Don't borrow a SKU from another receipt.
   - A bundle that splits into N spools is `qty: N, spools: 1` per color.
3. **Bambu lines:** give `code`, the 5-digit color code on the invoice (e.g. "Misty Blue (30108)").
   `intake.py` looks it up in Bambu Studio's `filaments_color_codes.json` in the `bambuddy-slicer` pod (`kubectl -n bambuddy exec deploy/bambuddy-slicer`).
4. **Other brands:** find the 3DFP id with `node 3dfp.mjs search "<brand> <material> <color>"`.
   Use the 3DFP hex as `rgba` and put the id in `fp`.
   If a receipt truncates the color, look the SKU up with WebSearch; Micro Center blocks both curl and headless Chromium.
5. `intake.py apply lines.json --dry-run`, check it, then run it for real.
6. **Profiles:**
   - `node 3dfp.mjs login` (headed, once per profile dir; needs `FP_EMAIL`)
   - `add <fp ids>`
   - `export <fp ids>` (PRINTER=H2C, all four nozzles by default)
   - `intake.py link profiles/`
   `link` covers spools whose note carries the 3DFP URL. Add `--map SPOOL=FP` for older spools that don't.
7. Verify in BB:
   - `GET /inventory/spools/{id}/filament-presets` shows 0.2/0.4/0.6/0.8.
   - No spool has a null `cost_per_kg` or a null `note`. A cost without a note is what makes the next intake ambiguous.
8. **Bring the prices into ScadBuddy.** Run the price book import: the agent tool
   `price_book_import_spools`, or `POST /api/v1/orders/price-book/import-spools`
   (`backend/scadbuddy/api/orders.py`). It groups BB spools by material,
   subtype, brand and colour and takes the newest priced spool's `cost_per_kg`
   (`backend/scadbuddy/orders/spool_import.py`). Read its report: a filament
   whose spools have different prices shows the min and max, and the newest
   one wins. A group with no `#RRGGBB` colour or no priced spool is reported
   and skipped: fix the spool in BB and run it again. The new spools themselves appear in SB's print dialog after the
   next overlay refresh (at most 300 s).

## Traps

| Symptom | Cause / fix |
|---|---|
| 3DFP answers 429 with "Vercel Security Checkpoint" | Bot challenge. A real Chromium passes it; curl and WebFetch never will. |
| 3DFP "Export" on a detail page says to add to My Spools | Export is login-only and per spool. `add`, then `export`. |
| Every 3DFP page answers `403: Forbidden` headless, even with a good cookie | Vercel's firewall rejects the `HeadlessChrome` user agent (since 2026-11-01). `3dfp.mjs` sends a normal Chrome UA; a one-off script must too. |
| `export` says "not in My Spools" or times out on the row's export button, while the spool is listed | The My Spools row groups several spools of one filament (a second `add` does this) and swaps the export button for "Expand the group". `export` now uses `/my/spools/details/<id>`, which has one button per spool. |
| `search` prints only the headings | The site's `?q=` index misses some brands (DEEPLEE). Browse `/filaments/<brand>/<material>/<type>` for the detail ids instead. |
| Export dies with "Target page, context or browser has been closed" | Chromium crashes on the blob download here. `3dfp.mjs` reads the blob in-page instead; keep that hook. |
| 3DFP `?search=` / `?brand=` return unfiltered results | Only `?q=` filters. |
| 1Password "3D Filament Profiles" has an empty username | The account email is the user's; pass it as `FP_EMAIL`. |
| Bambu spool duplicates when loaded in the AMS | Tag linking needs an exact material + RGBA match on an untagged spool. Take RGBA from the color-code table, never from a swatch or guess. Translucents carry alpha `80`. |
| `GFA17` missing from `/cloud/builtin-filaments` | That list is incomplete. GFA17 (PLA Translucent) is real and has H2C profiles for all four nozzles. |
| Multi-color silk subtype | Bambuddy's rule: SKU `A05-M*` = "Dual Color", `A05-T*` = "Tri Color", regardless of how many colors it has. |
| A quote or job cost still uses the old price after intake | SB's price book is not refreshed from BB on its own. Run step 8. |

## Housekeeping decisions

- **Used-up spool:** archive (`POST /inventory/spools/{id}/archive`), don't delete. Archiving keeps its usage history attached to past prints so filament cost per print still adds up; delete loses that.
- **A purchase with no receipt:** keep it, note "no receipt on file", and assume the nearest known price. Say which spool you assumed it for.
- **A duplicate you created:** delete it only if it has zero usage. Otherwise merge onto the spool that has the print history.

Moved from eh-homelab/clusters (`.claude/skills/bambuddy-filament-intake`,
added in #1311 there) on 2026-11-14.
