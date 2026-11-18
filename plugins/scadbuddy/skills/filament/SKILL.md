---
name: filament
description: Manage ScadBuddy's own filament inventory - spool types, spools, tags, the ledger, suppliers and invoices - and import a purchase invoice (a PDF or an image) into it. Use when asked to import, read or confirm a filament invoice, to add purchased filament, to cost a spool, to look up a Bambu colour code, to match an invoice line to a spool that already exists, or to explain why a spool has no cost. ScadBuddy owns spools; Bambuddy only holds a copy.
---

# Filament inventory and invoice import

ScadBuddy (SB) owns the spool inventory. Bambuddy (BB) keeps a copy that SB pulls
and does not write back to.

Sources, relative to the root of
[eh-homelab/ScadBuddy](https://github.com/eh-homelab/ScadBuddy):

- `docs/superpowers/specs/2026-11-14-filament-management-design.md` ("filament
  spec"): §5.2 types, §5.3 spools, §5.4 tags, §5.5 ledger, §5.8 purchasing, §9
  the invoice import.
- `docs/superpowers/plans/2026-11-18-filament-invoice-import.md` (#6267): the
  decisions of the import, and "Decisions made while building".
- `.claude/skills/filament-intake/SKILL.md`: the owner's rules for receipts. It
  is the source of the diff, cost and colour-code rules below. It still drives
  the BB-era scripts, so use it for 3dfilamentprofiles work (see "Profiles").
- Bambu Studio's colour table, `filaments_color_codes.json`, from the
  [BambuStudio](https://github.com/bambulab/BambuStudio) profiles. SB reads its
  copy at `BBL/filament/filaments_color_codes.json` under `slicer_profiles_dir`
  (`backend/scadbuddy/filament/colour_codes.py`).
- `backend/openapi.json` for every route named below (all under `/api/v1`).

## The data

- A **type** is a product: brand, material, subtype, colour (`#RRGGBBAA`),
  presets per printer model and nozzle, default cost per kg (filament spec §5.2).
- A **spool** is a physical spool. Every value is set on its type, and the spool
  can override it. A spool with no type is "needs type" (filament spec §5.3).
- A **tag** is an RFID or NFC identity bound to one spool at a time (filament
  spec §5.4).
- The **ledger** (`spool_events`) is the only source of grams left (filament
  spec §5.5). Never write grams left on a spool by hand.
- An **invoice** has lines. A line names a supplier product and a type. It
  creates spools, or costs spools that already exist (`update_spool_ids`).
  Confirming an invoice does that in one transaction (filament spec §5.8).

## Import an invoice

The person presses "Import with the assistant" on Filament, Invoices, on an invoice draft that has a
file. The agent route `POST /api/v1/ai/filament/invoice-import` starts this
session. The session's first message holds the invoice file, as an image or as a
PDF document block, and the draft's id. The route refuses a confirmed invoice
(409), an invoice with no file (422), and an invoice that a live session already
imports (409 with that session's id). The draft's `import_run_id` is the session
id. A session that is idle is waiting for the person; it is not finished.

Read the file in the first message. Do not look for it with a tool.

Tools, in the order you use them. A candidate spool has an empty own cost and
no invoice line:

1. `filament_invoice_get`: the draft as it stands.
2. `filament_bambu_colour` with the five-digit `code` of a Bambu SKU: `name`,
   `rgba`, `fila_type`, `fila_id`. A 404 means the table does not have the code.
   Say so and ask the person for the colour. A 503 means this install has no
   table. Never invent an RGBA.
3. `filament_invoice_candidates`: for each line, the open spools whose own cost
   is empty and which match the line's type (or proposed type) by material and
   RGBA exactly, and the spools that have a cost already.
4. `filament_invoice_upsert`: write the draft (the header, the lines, each
   line's type and `update_spool_ids`). Edit the draft you were given. Never make
   a second invoice.
5. `filament_invoice_confirm`: only after the person says yes to a summary of what
   will be created and updated. It needs approval.

### Rules

1. **Diff before you create.** Call `filament_invoice_candidates` for every
   line first.
   - A spool with no cost that matches a line is that purchase. The line
     updates it. An RFID tag often created that spool (`data_origin:
     rfid_auto` in BB).
   - A spool that has a cost belongs to an earlier purchase. A matching line is
     a new spool.
   - A line of quantity N that covers one uncosted spool and new ones is one
     update plus N-1 new spools.
   - When it is ambiguous, ask the person. Do not guess between "the same
     spool" and "a second spool".
   - "Uncosted" means the spool's own cost per kg is empty and it has no invoice
     line. A cost that only the type gives is a default, not a purchase.
2. **Cost per spool** is (line subtotal + that line's tax + that line's share of
   shipping) ÷ quantity. Shipping is shared over the lines by value
   (`allocate` and `unit_cost` in `backend/scadbuddy/filament/invoice_money.py`).
   The confirm computes the cost. Show the person the same number. Use the tax
   the invoice prints per line. When only a total tax is printed, the rate is
   total tax ÷ subtotal, applied per line, and the line records its tax basis
   (for example "receipt-wide 7.0%"). A bundle that splits into N spools is
   quantity N.
3. **No SKU on the invoice:** use the product name as the SKU. Never borrow a SKU
   from another invoice.
4. **Bambu lines:** the colour code on the invoice (for example "Misty Blue
   (30108)") gives the exact RGBA through `filament_bambu_colour`. Tag linking
   needs an exact material and RGBA match, and a translucent colour keeps alpha
   `80`. Take the RGBA from the table, never from a swatch.
   - `GFA17` (PLA Translucent) is missing from BB's `/cloud/builtin-filaments`
     list. It is a real filament. A missing entry there does not make the line
     wrong.
   - A multi-colour silk's subtype follows the SKU: `A05-M*` is Dual Color and
     `A05-T*` is Tri Color.
5. **Other brands:** propose a new type with its 3dfilamentprofiles.com (3DFP)
   id and its hex as the RGBA. Say that the nozzle presets are still to fetch
   (see "Profiles").
6. **Never confirm for the person.** Show the totals, the spools to create, and
   the spools to update. Wait for a yes.
7. If the person corrects a line in the chat or on the page, edit the same draft.
   Read it again with `filament_invoice_get` first. The person may have changed
   it on the page.

### Check at the end

- The invoice's lines add up to its total, within the confirm tolerance. Confirm
  refuses a larger difference (409).
- No spool from the invoice is missing a cost.
- Every new type has a colour.

## Profiles

The fetch of slicer profiles from 3DFP needs a real browser, behind a Vercel bot
check. SB has no worker for it yet: it is #6625. Until it lands, this session
does not fetch profiles. For a non-Bambu line, the session proposes the type with
its 3DFP id and hex, and tells the person that the presets are still to fetch.
For the BB-era scripts (`intake.py`, `3dfp.mjs`), use
`.claude/skills/filament-intake/SKILL.md`.

## Where a fact comes from

- The 3DFP bot check, its 429 and 403 answers, and the `GFA17` gap are stated
  in `.claude/skills/filament-intake/SKILL.md`, which gives no date or method for
  them. #6267 did not measure them again. Check one before you rely on it.
- The candidate rule ("uncosted") and the 404 and 503 answers of the colour
  route are SB's code and its tests: `backend/tests/filament/test_invoice_candidates.py`
  and `backend/tests/filament/test_colour_codes.py`.
