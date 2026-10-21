# Orders: customers, line items priced by the slice, fulfilment with provenance, and the flow that works them

Status: accepted, merged in #4176 (2026-10-19). Epic #4248. Design only; no code. Builds on the flows specs
(`2026-10-01-durable-printing-agents-flows-design.md`, the "10-01 spec", and
`2026-10-11-flow-grants-and-git-design.md`, the "10-11 spec", with its §12 triggers
amendment), the template-pipelines spec (`2026-09-27-template-pipelines-design.md`,
template-owned UIs in §4) and the own-store spec
(`2026-10-16-own-store-slicer-rust-workers-design.md`, epic #3818). Related epics:
#3818 (Bambuddy out of the hot path), #2486 (flows people can see, steer and trust),
#2182 (print as easily as a page).

Every fact about the code below was read from the tree at `d4df45d` on 2026-10-18; file
and line references are to that commit, except F4, re-read at `ee43c92` (#4162) the same
day. Nothing here was measured on a live system.

## 1. Goal

ScadBuddy knows who it prints for. A **customer** has contact points (phone numbers,
emails, social profiles), addresses and notes. An **order** for a customer holds **line
items**, each of which pins one template at one version with one set of inputs and
colours, is **quoted** from what its slice says it will use, is printed, and is
**fulfilled** with the evidence of what was made: the exact file printed, the print's
outcome, the finished photo, the timelapse. An order closes only when every line item
is fulfilled or cancelled. Once an order is accepted, a **flow** works it: the order is
an event a trigger fires on, and the run renders, quotes, prints and asks a person only
where the grant does not cover the call.

Bambuddy stays optional. Everything an order needs to exist, be priced, be printed and
be fulfilled lives in ScadBuddy. When `bambuddy_url` is set, the Bambuddy integration
lights up around it: nested Bambuddy projects mirror the order, archives link the
outcome and the timelapse, and spool prices feed the price book. Without it, nothing is
missing but those mirrors.

### 1.1 Goals

- Customers with any number of contact points and addresses, and free notes.
- Orders of line items, a money total that adds up from the line items and the order's
  charges, and a lifecycle whose close is gated on its line items'.
- A line item references exactly one render: template, commit, inputs, libraries, and
  the chosen filament per colour. The reference is durable: it outlives the output and
  the render cache.
- A quote computed from the sliced file's own `slice_info.config` (grams per filament,
  per plate), the same file whether Bambuddy's slicer or the native one sliced it, at
  prices ScadBuddy holds. The accepted quote is a snapshot that later price changes
  never move.
- Fulfilment records provenance ScadBuddy owns: the sliced file's digest, the print
  run, the outcome, the photos and the timelapse as blobs in ScadBuddy's own store.
- The whole process is a flow, started by a trigger on the `order.changed` event, written and
  changed in the Flows tab like any other flow. ScadBuddy ships one as a built-in.
- The orders API is tools for the agent, routes for the browser, and host calls for
  flows, in that order of design.

### 1.2 Non-goals

- Payment processing, invoicing to a tax authority, or accounting. An order records
  amounts and what was paid; it does not take money.
- A storefront customers log into. §9 says how a page for that could be declared later.
- Inventory: how many spools are in stock stays Bambuddy's (own-store spec §1.2) until a
  ScadBuddy filament inventory exists. The price book (§5.1) is prices, not stock.
- Carrier integration: a shipment is a carrier name, a tracking number and a date.
- Replacing `print_attachments` (#309). §6.2's attachments are a new, subject-keyed
  table; moving print attachments onto it is a later change.

### 1.3 Terms

"Project" is already two things in this codebase: the flow runs' Temporal queue and
workflow name (`workflows/project.py`), and Bambuddy's project folders
(`bambuddy/projects.py`, which nest through `parent_id`, #930). This document does not
add a third. The user's framing, "each line item is a sub-project of an order-level
project", is what §3's tree is: the **order** is the root, each **line item** is one
child with its own render, prints and fulfilment, and the root's close is gated on the
children's. Where Bambuddy is configured, that tree is mirrored as nested Bambuddy
projects (§8.1), which is the only place the word "project" appears in an order.

## 2. Facts this rests on

| # | Fact | Where |
|---|---|---|
| F1 | A render is keyed by `render_key(slug, params, model_version)`, sha256 of the triple; pipeline templates use `inputs_key(slug, inputs, model_version)` over whole inputs. | `render/job_models.py:261-265`, `render/inputs.py:111-117`, chosen at `render/submit.py:291-296` |
| F2 | A model version is a git commit of `data/models` (`COMMIT_ID_PATTERN` `^[0-9a-f]{7,40}$`); `restore` makes a new commit and never rewrites history; `export(slug, commit, dest)` reproduces the tree. | `library/history.py:1-13,105,208-224,469`, `core/gitrepo.py:100` |
| F3 | An output's `meta.json` carries `slug`, `model_version`, `job_id`, `colors`, `parts`, `layout_plates` and `libraries` (the exact library commits), beside `params.json` and `inputs.json`; outputs can be deleted. | `library/outputs.py:50-60,131-186,388-499` |
| F4 | ScadBuddy's native slice result carries no grams: `SlicePlateOutput` is digests, sizes, warnings, exit code and elapsed time. The grams are read from the sliced 3MF afterwards: `slicing/slice_info.py` `read_sliced(payload, plate_id)` (#4162) gives a plate's `print_time_seconds`, `grams_by_slot` and `filament_changes`, over `filaments3mf.read_filaments(sliced=True)`, the same parser of `Metadata/slice_info.config` the Bambuddy path uses (F5). It reads no metres. | `slicing/models.py:56-64`, `workers/rust/crates/sb-slicer/src/models.rs:57-71`, `slicing/slice_info.py:58-101` at `ee43c92`, own-store spec §5.4 |
| F5 | Today every grams figure comes through Bambuddy: `_sliced()` reads per-plate, per-slot `used_grams` from the same `slice_info.config`; `SlicePreview` sums the slots because Bambuddy's `filament_used_g` covers only the first filament (#2450). | `bambuddy/filaments3mf.py:108-117,268-291`, `bambuddy/preview.py:68-86,330-339` |
| F6 | The only price anywhere is Bambuddy's per-spool `cost_per_kg`, exposed as `price_per_g` and a `grams_cost` on `GET /farm/spools/{id}/summary?grams=`; ScadBuddy stores no price and no density. | `bambuddy/farm.py:75,172-181,207-270`, `api/farm.py:105-126` |
| F7 | A print run's row holds `status` (running, succeeded, failed), `result` and `error`; "succeeded" means queued. The exact sliced file is `PrintRunResult.sliced_library_file_id`; the colour choice is `PrintRunRequest.filament_plan.slots[{slot_id, spool_id}]`. | `bambuddy/runs.py:91-114`, `bambuddy/print_run.py:169-206,311-339` |
| F8 | The print's outcome (status, `failure_reason`, times, grams, `cost`, runs) and its media (`finish_` photo, photos, timelapse) are Bambuddy's archive, linked by `print_links` on the queue item or the sliced file's content hash; ScadBuddy proxies them and stores none. | `bambuddy/print_links.py:1-60,226,351`, `bambuddy/linking.py:182-220`, `api/print_history.py:160-172,280-339,1051-1079` |
| F9 | `print_attachments` (photos and videos of the finished object) is keyed by Bambuddy `archive_id`, and the bytes live in Bambuddy; there is no "attach media to X" for any other subject. | `migrations/20261013T1600Z_print_attachments.sql`, `bambuddy/attachments.py:1-50` |
| F10 | Bambuddy configured means `bambuddy_url` set; `BambuddyConfig.from_settings` raises `not_configured` (409) otherwise, and every call goes through `client_for`. The native print library (`print_library/`) replaces browsing and saving only; printers by serial and the LAN path (#3823) are planned, not built. | `core/settings.py:136`, `bambuddy/client.py:236-238,1511`, `bambuddy/errors.py:31,60`, plans `2026-10-17-native-print-library.md`, `2026-10-16-printer-identity-native-path.md` |
| F11 | A flow run is `ProjectWorkflow`, one `execute` per run, unversioned (replay fixtures), started only by `flows/launch.py:start_execution` from the route or a trigger fire. Scripts get `render`, `save_output`, `queue_print` (gated), `arrange` (gated), `tool`/`tool_result` (gated when outward), `agent`, `ask_session`, `wait_for_human`, `sleep`, `inputs`, `trigger`. There is no parent run, no child run and no subflow; 10-11 Ruling 21: "a run is not a child workflow". | `workflows/project.py:86,139`, `workflows/flow_tools.py:80-1190` (`SCRIPT_TOOLS` 1140), `flows/launch.py:52` |
| F12 | Trigger event kinds are exactly `render.finished` and `model.changed`, matched per template `slug`; `trigger()` falls through to `RenderFinished` for any kind but `model.changed`. A fire runs as the trigger's creator with a per-run grant seeded from the standing grant. | `flow_triggers/models.py:21,114`, `flow_triggers/events.py:30`, `flow_triggers/store.py:343-352,499`, `workflows/flow_tools.py:1159-1190`, `flow_triggers/fire.py:49,100-189` |
| F13 | Grant rules bound `queue_print`, `arrange` and `tool` by printers, model slugs, library files, `max_prints`, `max_filament_g`, `max_calls` and pointer-bounded `args`; the vocabulary is closed and never reads inputs. | `flows/grants.py:27,70`, 10-11 spec §3 |
| F14 | Template-owned UIs: the loader (dynamic `import()`, a shadow root, a 15 s mount deadline, fallback to the form) is module-agnostic; Host API v1 is one template's inputs (`inputs.get/set/subscribe`, `schema`, `files.url`, `generate`, `openPrint`, `presets`, `describe`), the serving route is `models/{slug}/ui/**` with suffix and path checks, and the page's CSP is `script-src 'self'; connect-src 'self'`, so a module may call any `/api/v1` route as the user. | `frontend/src/template-ui/host.ts:19-151`, `TemplateUi.tsx:168-299`, `api/template_ui.py:33-146`, `api/static.py:197-211` |
| F15 | The one JSON-Schema form in the frontend is the flow inputs form: a closed subset (scalars, arrays of scalars, enum, bounds), validated by `flows/inputs.py`. | `frontend/src/lib/flowInputs.ts:35-60`, `components/flows/inputsForm.tsx:10-28`, `flows/inputs.py:1-116` |
| F16 | Operations: a feature registers kinds by exporting `OPERATION_KINDS` from `scadbuddy/<feature>/operations.py`; a route runs one through `run_operation` with an `Idempotency-Key`; activity kinds the same way through `ACTIVITY_KINDS`; a service is a discovered `Component`. | CLAUDE.md "Layout" (operations, activity, components) |

## 3. The record

Everything is Postgres, in new migration files (`backend/scadbuddy/migrations/`), owned
by a new feature package `backend/scadbuddy/orders/` with its `component.py` (`ORDERS`),
`operations.py` (`OPERATION_KINDS`) and `activity.py` (`ACTIVITY_KINDS`), discovered as
every feature is (F16). Routes are `api/orders.py` and `api/customers.py`.

### 3.1 Customers

- `customers`: `id` (uuid), `display_name`, `company`, `notes` (text, free), `tags`
  (text[]), `created_at`, `updated_at`, `archived_at`.
- `customer_contacts`: `customer_id`, `id`, `kind` (`phone`, `email`, `instagram`,
  `facebook`, `tiktok`, `etsy`, `website`, `other`), `value`, `label` (free: "work",
  "WhatsApp"), `primary` (boolean, at most one per kind), `position`. A profile link is
  stored as the handle or URL as typed; the UI links known kinds.
- `customer_addresses`: `customer_id`, `id`, `label`, `recipient`, `lines` (text[]),
  `locality`, `region`, `postal_code`, `country` (ISO 3166-1 alpha-2), `phone`,
  `default_shipping`, `default_billing`, `position`.

A customer is never deleted while an order names them; `archived_at` hides them from
pickers. A customer's own record is editable by anyone who can edit the library
(there is one user, as everywhere in ScadBuddy; the agent is a principal, §7.3).

### 3.2 Orders

- `orders`: `id` (uuid), `number` (a short human id, `ORD-<yyyy>-<n>` from a per-year
  counter row, never a sequence, which skips on a rolled-back insert: Ruling 5 of the
  phase 1 plan), `customer_id`, `status` (§3.5), `currency` (ISO 4217, from the setting
  `orders_currency`, fixed when the order is made), `shipping_address` (jsonb snapshot
  of the address chosen, so a later edit to the customer does not move a shipped
  order), `billing_address` (jsonb snapshot, nullable), `notes`, `due_at`,
  `accepted_at`, `closed_at`, `cancelled_at`, `created_at`, `updated_at`, `created_by`
  (`_started_by()` from the author header, never from the body, as flows do).
- `order_charges`: `order_id`, `id`, `kind` (`shipping`, `discount`, `fee`, `tax`),
  `label`, `amount_minor` (integer minor units, signed; a discount is negative),
  `position`. Tax is a charge too, entered or computed by the flow; the record does not
  know tax law.
- `order_payments`: `order_id`, `id`, `amount_minor`, `method` (free text), `reference`,
  `received_at`, `notes`. Paid is the sum; the order shows balance due.
- `order_events`: `order_id`, `id`, `at`, `by` (the principal), `kind`, `detail`
  (jsonb): the audit line, every status change and quote acceptance, written in the
  same transaction as the change.

### 3.3 Line items

`order_items`: `order_id`, `id`, `position`, `status` (§3.5), `quantity` (≥ 1),
`title` (free, defaults to the template's name), `notes`, and the **reference**:

- `slug`, `commit` (full 40-hex, F2), `inputs` (jsonb, the normalised `{params, v, …}`
  of F1), `libraries` (jsonb, the library commits from F3), `template_name` (a copy, for
  display after a delete), `render_key` (F1, computed; `inputs_key` for a pipeline
  template);
- `colours` (jsonb): one entry per colour slot of the render, `{slot, colour, filament_id}`:
  the slot's `#RRGGBB` as rendered and the price-book filament chosen for it (§5.1);
  `filament_id` may be null until quoted;
- `output_id` (nullable): the output the reference was taken from or last rendered to.
  The reference is complete without it; a deleted output leaves the item renderable
  (F2, F3).

The reference is the one way a line item names what to make. **Nothing on an order is a
file path or a Bambuddy id.** The `// file` assets an item's inputs name are kept alive
by adding `order_items.inputs` to `referenced_asset_ids`
(`library/assets.py:766-780`), so the asset sweep never takes an ordered upload.

The UI makes a line item from the Customize page ("Add to order…" beside Generate: the
current slug, HEAD commit, inputs and the preview's colours), from an existing output
("Order this" in the output's menu: its `meta.json` and `inputs.json`), or from a
template's gallery with defaults. The agent makes one with `order_item_add` (§7.3).

### 3.4 Quotes

`order_item_quotes`: `order_item_id`, `id`, `created_at`, `source` (`slice` or
`manual`), `accepted` (boolean; at most one accepted per item, enforced by a partial
unique index), the measured facts and the arithmetic:

- `sliced_sha256` and `sliced_blob_key`: the sliced file the quote was read from,
  kept as a `library` blob with a `blob_refs` row named `order_item_quote:<id>` from
  the moment the quote is recorded, so the bytes an accepted quote names are there
  when the print starts, days later. An item keeps at most two such refs: its
  accepted quote's and its newest unaccepted one's (a quote a person is still
  looking at). Recording a quote releases the older unaccepted one's ref; accepting
  a quote releases every other quote's; cancelling the item, or closing the order,
  releases them all. The quote rows stay (Ruling 6 is about the record, not the
  bytes), with `sliced_sha256` saying what was priced. §6.1's attempt takes a ref of
  its own on the same key, so a print's file outlives the quote's ref. A `manual`
  quote has no sliced file: both fields are null and no ref is taken;
- `plates` (jsonb: per plate `{index, print_seconds, filaments: [{slot, used_g}]}`
  as `read_sliced` gives them, §5.2);
- `lines` (jsonb): the priced lines, each `{kind, label, quantity, unit_minor,
  amount_minor}` with `kind` one of `filament` (one per slot: grams × price per g),
  `machine_time`, `setup`, `labour`, `markup`, `manual`;
- `unit_cost_minor`, `unit_price_minor`, `quantity`, `total_minor`.

The item's price is its accepted quote's `total_minor`. The order's subtotal is the sum
of accepted quotes; the total adds the charges. An item with no accepted quote has no
price, and an order with such an item cannot be accepted (§3.5). A `manual` quote is a
person typing a price; it is recorded as such.

### 3.5 Lifecycle

Line item: `draft` → `quoted` → `approved` → `queued` → `printing` → `printed` →
`fulfilled`; from any open state `cancelled`; from `printing` or `printed` also
`failed`, which goes back to `approved` on a reprint (each attempt is a fulfilment
attempt, §6.1). `approved` means its quote is accepted.

Order: `draft` → `quoted` (every item has an accepted quote) → `accepted` (the person
or the customer said yes; sets `accepted_at` and publishes `order.changed`, §7.1) →
`in_production` (any item past `approved`) → `ready` (every item `fulfilled` or
`cancelled`, at least one fulfilled) → `shipped` (a shipment exists covering every
fulfilled item, §6.3) → `closed`; `cancelled` from any state before `shipped`;
`on_hold` as a flag, not a state, which pauses the flow's work (§7.2).

Phase 1 (#4249) reaches `draft`, `quoted`, `approved`, `fulfilled`, `cancelled` and `failed`
(from a hand-recorded attempt); `queued`, `printing` and `printed` wait for the print path
(phase 3). Phase 1 derives `in_production` as "any item has a fulfilment attempt and not
every item is settled", the "any item past `approved`" above restricted to the states it
has (an attempt is the only way past `approved` there); phase 3's `queued` writer must widen
`orders/lifecycle.py` `derive` to "any item `queued` or beyond, or with an attempt", so the
derivation keeps meaning "any item past `approved`" (Ruling 4 of the phase 1 plan).

**The close rule.** `orders.status` is never written directly past `accepted`. The
store derives `in_production`, `ready` and whether `shipped` and `closed` are allowed
from the items and shipments, in the same transaction as the item change, under the
order's row lock. `POST /orders/{id}/close` is refused with a problem listing every
item not `fulfilled` or `cancelled` and every fulfilled item not shipped. Cancelling an
order cancels its open items. A cancelled item that was already printed keeps its
fulfilment attempts and their provenance; the record is never thinned.

### 3.6 Money

Amounts are integer minor units (`*_minor`, bigint) with the order's `currency`.
Rates in the price book are decimal strings parsed with `Decimal`, never floats; each
quote line rounds half-up to a minor unit once, at the line, and the totals add
rounded lines. Float `cost` figures Bambuddy reports (F8) are shown beside a quote as
"Bambuddy's cost" and never enter one.

## 4. API

Under `/api/v1`. Plain reads and writes are routes; a write that does work (a quote,
a render, a print) is an operation kind (F16) so the browser's `command()` and the
agent's follow a 202.

| Route | Does |
|---|---|
| `GET/POST /customers`, `GET/PATCH /customers/{id}`, `POST /customers/{id}/archive` | customers; contacts and addresses as nested lists in the body, replaced whole |
| `GET /orders` (`status`, `customer_id`, `q`, `from`, `to`, cursor), `POST /orders`, `GET/PATCH /orders/{id}` | orders; `PATCH` edits notes, due date, addresses, charges while `draft` or `quoted` |
| `POST /orders/{id}/items`, `PATCH/DELETE /orders/{id}/items/{item}` | line items; the reference is set on create and replaced whole on `PATCH` while `draft` or `quoted` (a new reference drops the accepted quote); `DELETE` only while the item is `draft` (never quoted), else it is a cancel (§3.5) |
| `POST /orders/{id}/items/{item}/quote` (operation `order_item_quote`) | render if needed, slice, price (§5); 202 then the quote |
| `POST /orders/{id}/items/{item}/quotes/{quote}/accept` | pins the quote; the item becomes `approved` |
| `POST /orders/{id}/items/{item}/quotes` with `{unit_price_minor, label?}` | a typed price: one `manual` line, `unit_cost_minor = unit_price_minor`, the item's quantity as it is now (Ruling 1 of the phase 1 plan) |
| `POST /orders/{id}/accept`, `/cancel`, `/hold`, `/release`, `/close` | lifecycle (§3.5); `accept` publishes the event |
| `POST /orders/{id}/items/{item}/print` (operation `order_item_print`) | §5.4: a print run for the item at its accepted quote's sliced file; the item becomes `queued` |
| `POST /orders/{id}/items/{item}/fulfilments`, `PATCH …/{fulfilment}` | §6.1: an attempt and its outcome |
| `POST /orders/{id}/items/{item}/fulfilments/{f}/attachments` (multipart), `DELETE …/{attachment}` | §6.2; the delete removes the row and its blob ref (a mis-upload), recorded on the audit line |
| `POST /orders/{id}/items/{item}/fulfilments/{f}/accept` | §6.1: the attempt's quantity counts as made; the item becomes `fulfilled` when its attempts cover its quantity |
| `POST /orders/{id}/shipments`, `PATCH …/{shipment}` | §6.3 |
| `GET /orders/{id}/costs` | the quote against the actuals (§6.4) |
| `GET/PUT /orders/price-book` | §5.1 |
| `GET /orders/{id}/events` | the audit line |

Every body has `extra="forbid"`; no body carries an author, a principal or a status
the store derives. Writes publish `order.changed` / `order_item.changed` on the event
bus (ids only, as every event), which the realtime `orders` topic relays and the
frontend's list re-reads on, as the Activity page does.

## 5. Pricing

### 5.1 The price book

Settings, in the settings tables (`library/settings_store.py`), edited on a Pricing
tab of the Orders page:

- `orders_currency` (ISO 4217, default `USD`); changing it affects new orders only.
- `filaments`: ScadBuddy's own filament records, a new table `filaments`: `id`,
  `material`, `subtype`, `brand`, `colour_name`, `colour` (`#RRGGBB`), `cost_per_kg`
  (decimal), `density_g_cm3` (decimal, nullable; for the rough estimate in §5.3),
  `archived_at`. This is the price of a filament, not a spool: `_filament_key`'s
  grouping (material, subtype, brand, colour; `bambuddy/farm.py:187-206`) is the
  identity, so with Bambuddy configured a record is matched to its spools by that key
  (§8.3).
- `pricing_policy`: `machine_rate_per_hour` (decimal, may be 0), `setup_fee` (per
  plate, decimal), `labour_per_item` (decimal), `markup_percent` (decimal), `rounding`
  (`to_minor_unit`, the default: each line rounds as §3.6 says and the unit price is
  their sum; or `to_major`: the unit price is then rounded up to the next whole
  currency unit, the shop owner's choice).

The policy and the filament prices in force are copied into each quote's lines, so the
book can change freely afterwards.

### 5.2 A slice quote

The `order_item_quote` operation, on the `library` queue, as activities of one
`OrderQuote` workflow (the same shape as `Arrange`, `workflows/pipelines.py`):

1. **Render.** If `output_id` is null, or the output is gone, or its `meta.json` does
   not match the reference (slug, `model_version`, inputs), submit the render through
   `RenderService` with the reference's slug, commit and inputs (`render_model`'s
   path, pinned to the commit as a versioned render is) and save an output; set
   `output_id`. A matching output is reused: `render_key` joins the piece.
2. **Slice.** Slice the output's 3MF with the item's colours mapped to the chosen
   filaments' slicer profiles, through the slicer the stack has: the print dialog's
   background slice (`housekeeping_sweep_preview_slices`' path) today, the native
   `slice_plate` activity once #3822 lands. The quote keeps the sliced file
   (`sliced_blob_key`, §3.4) and records its `sliced_sha256`.
3. **Read usage.** For each plate, `slice_info.read_sliced(payload, plate_id)` (F4):
   `print_time_seconds` and `grams_by_slot`, from the sliced 3MF's
   `Metadata/slice_info.config`. It sits over the parser the Bambuddy path reads
   with (F5), so both slicers price from one reader and no second parser is written.
   A plate that reads as unknown (`read_sliced`'s every-field-None answer), or a slot
   at `0` grams (not sliced, as F5's code treats it), fails the quote rather than
   pricing a slot at nothing.
4. **Price.** For each slot: `used_g × cost_per_kg / 1000` at the slot's filament; a
   slot with no `filament_id` fails the quote naming the slot. Then machine time
   (`print_time_seconds` summed over plates × rate), setup (per plate), labour, markup on the
   sum, rounding per §3.6; times `quantity`. The result is the quote's `lines`.
5. **Record** the quote (`accepted=false`) and publish `order_item.changed`. The
   browser shows it with "Accept"; the flow accepts it or asks (§7.2).

A quote is a fact about one sliced file. Changing the reference, the colours or the
quantity makes the next quote a new row; the old one stays, unaccepted, as history.

### 5.3 The rough estimate

Before a slice, the Customize page and the agent can show an estimate: the output's
parts' enclosed volumes (`volume_mm3`, which `render/geometry.py:206-207` measures
for a checked, closed part) × the filament's
`density_g_cm3` × a `rough_estimate_factor` setting (default 1.15, for walls, infill
and purge; the shop sets it from its own history). It is labelled an estimate, never
stored as a quote, and never accepted. A filament with no density gives none.

### 5.4 Printing an item

`order_item_print` starts a `PrintRunWorkflow` for the item: the source is the
accepted quote's sliced file (its digest must match, else the print is refused with
`requote_needed` and the item wants a re-quote). An accepted `manual` quote names no
file, so the print slices at its start, through §5.2's step 2 with the item's
colours, and the attempt records that file; the digest check applies only to a
`slice` quote, since a manual price was never a fact about one file. The request's
`filament_plan` maps
each slot to a spool of the slot's filament, and `copies` is the quantity still to
print. A flow reaches it as `queue_print({"order_item_id": id}, request)` (§7.2), so
the grant's `printers`, `max_prints` and `max_filament_g` bounds apply as they do to
any print.

**Spools.** A quote names a filament per slot (§3.3), never a spool, so the print fills
`filament_plan.slots` (F7) when it starts, on the printer it runs on:

- With Bambuddy configured, a slot's spool is the one loaded on that printer whose
  `_filament_key` is the slot's filament's (§5.1); with none loaded, the unarchived
  spool of that filament on the shelf with the most grams left, as the print dialog's
  spool-first choice makes it (`bambuddy/filaments.py` `_display_order`). A spool
  loaded in another printer is never taken: using it means unloading it there, which
  is a person's call. No such spool at all refuses the print with
  `needs_spool_choice` naming the slot, which the flow's `wait_for_human` or the
  dialog answers (naming a spool loaded elsewhere, if they choose).
- The request may supply `filament_plan` itself, as a narrowing: each spool named must
  be of its slot's filament, else the print is refused with `spool_not_filament`. A
  different filament is a change to the quote (§3.3 `colours`), never to the print.
- On the native path (#3823) there is no spool inventory yet (§1.2): the plan carries
  the filament's slicer profile per slot and no spool, and the person loads the
  printer. A ScadBuddy inventory is a later design.

The run's `subject` is `order_item:<id>`, which is how the outcome finds its way back
(§6.1).
The item's status follows the run: `queued` when the run succeeds (F7: queued, not
printed), `printing` on the first `print.progress` for its send, `printed` or `failed`
when the print settles. On the Bambuddy path the settle is the archive's (F8); on the
native path it is the native follow #3823 plans, and until that lands a native print
settles only by hand (`PATCH` the fulfilment attempt, §6.1).

## 6. Fulfilment and provenance

### 6.1 Attempts

`order_item_fulfilments`, one row per print attempt of an item, written by the print
path and editable by hand:

- `order_item_id`, `id`, `attempt` (1, 2, …), `quantity` (made in this attempt),
- `print_run_id`, `sliced_sha256`, `sliced_blob_key` (the sliced file kept as a
  `library` blob with a `blob_refs` row named `order_item_fulfilment:<id>`, so the
  blob sweep never takes it: `store/refs.py` `sweep_blobs` removes every blob of a
  swept kind that no row names),
- `printer` (`{serial, name}` once #3823's registry exists; `{bambuddy_printer_id,
  name}` meanwhile), `started_at`, `finished_at`,
- `outcome` (`succeeded`, `failed`, `partial`, `unknown`), `failure_reason`,
  `actual_seconds`, `actual_grams` (jsonb per slot), as the settle reported them,
- `archive_id` (Bambuddy's, nullable; §8.2), `timelapse_blob_key` (nullable, §6.2),
- `notes`.

An item is `fulfilled` when its attempts' succeeded quantities reach its quantity and a
person (or the flow, within its grant) accepts the attempt with
`POST …/fulfilments/{f}/accept` (§4); it is `failed` when the latest attempt failed,
and a new attempt returns it to `approved`.

### 6.2 Attachments

A new table `attachments`, keyed by subject, is the "attach media to X" facility F9
says does not exist:

- `subject` (`order:<id>`, `order_item:<id>`, `order_item_fulfilment:<id>`), `id`,
  `kind` (`photo`, `video`, `document`), `role` (`finished`, `packed`, `timelapse`,
  `other`), `filename`, `media_type`, `size_bytes`, `sha256`, `blob_key` (a `library`
  blob in ScadBuddy's store with a `blob_refs` row named `attachment:<id>`),
  `caption`, `created_at`, `created_by`.
- Upload is multipart, streamed into the claim store as media uploads are
  (`ClaimStore.hold_file`), then an operation (`attachment_add`) moves the bytes into
  the blob store and records the row; `GET /attachments/{id}` serves the blob with a
  `Range`-capable response for video.
- The **finished photo** is the attachment with `role: finished` on the fulfilment
  attempt. With Bambuddy linked, the archive's `finish_` photo is offered as a
  one-click copy into it (§8.2); without, the person or the flow (through the
  agent's `get_printer_camera` tool, `agent/src/tools/print.ts:386`, which exists
  for Bambuddy printers today) adds one.
- The **timelapse** is the attachment with `role: timelapse`: with Bambuddy, pulled
  through `POST /prints/{archive_id}/timelapse/pull` and then copied as a blob; on
  the native path, whatever #3823's camera work fetches.

### 6.3 Shipments

`order_shipments`: `order_id`, `id`, `carrier`, `service`, `tracking`, `shipped_at`,
`address` (jsonb snapshot), `cost_minor`, `notes`; `order_shipment_items`:
`shipment_id`, `order_item_id`, `quantity`. An order may ship in parts; it is `shipped`
when the shipment quantities cover every fulfilled item. A packed photo is an
attachment on the order with `role: packed`.

### 6.4 The real cost

`GET /orders/{id}/costs` lays the accepted quotes beside the attempts: per item, quoted
grams and seconds against actual, the quoted filament cost against the actual grams at
the quote's prices, the attempts that failed and what they consumed. That is "the real
cost of a specific line item": the sum over its attempts, failures included. Bambuddy's
own `cost` (F8) is shown alongside when an archive is linked, corrected as
`api/print_history.py:1119-1151` corrects it, and not used in the sums.

## 7. The flow

### 7.1 Events and the trigger

Two bus events (`core/events.py`, ids only): `order.changed` (`order_id`, `status`) and
`order_item.changed` (`order_id`, `item_id`, `status`, `attempt`). **A trigger's event
kind is the bus event's own name.** The existing two kinds are renamings
(`render.finished` for `job.done`, `model.changed` for `version.committed`, F12); the
orders kinds are not, and the trigger amendment (10-11 §12.3) gains:

| Trigger event | Bus event | Event key | Subject |
|---|---|---|---|
| `order.changed` | `order.changed` | `order:<id>:<status>` | an order |
| `order_item.changed` | `order_item.changed` | `order_item:<id>:<status>:<attempt>` | an order item |

What narrows a trigger is a **filter**, not a kind: `EventTiming` gains `match`, an
object of the event's own fields and the values each may take (`{"status":
["accepted"]}`), checked against the event at fire time; an event matching no entry
starts nothing. So "when an order is accepted" is `{kind: "order.changed", match:
{status: ["accepted"]}}`, and "when an item's print settles" is `{kind:
"order_item.changed", match: {status: ["printed", "failed"]}}`. The key carries the
status, so one trigger fires once per transition, not once per order.

`EventTiming.slug` becomes `subject: {kind, id?}` with `kind` one of `template`,
`order`, `order_item` (the slug form stays accepted and is read as
`{kind: "template", id: slug}`), and `_event_triggers`' SQL matches on it. `trigger()`
returns the event as it is, `OrderChanged{order_id, status, customer_id}` or
`OrderItemChanged{order_id, item_id, status, attempt}`, and the fall-through at
`flow_tools.py:1188` becomes a dispatch on `kind`. The facts come from the order's row
at fire time, never from the event, as §12.1 requires. The agent's
`create_flow_trigger` and the Triggers tab list the kinds by their bus names and offer
`match` for the ones with a `status`. Each is a change to the unversioned
`ProjectWorkflow`'s host surface and replays the fixtures (F11).

The two existing kinds are kept as they are; the same change may accept `job.done`
and `version.committed` as their bus names and read the old names as aliases, so a new
trigger is written in bus names only. That is a ruling for the plan, not a rename of
stored triggers.

Rate and overlap apply as they do: `overlap: skip` on an order trigger means a second
acceptance of the same order while its run is open starts nothing, and the 60-per-hour
cap holds.

### 7.2 The built-in `order-fulfilment` flow

Registered at start by the orders component, through `FlowHistory.register` under the
flows lock, as a flow named `order-fulfilment` when no flow of that name exists yet,
with `inputs.json` `{notify: boolean, auto_accept_quotes_under_minor: integer}`. It
is version 1 of an ordinary flow: the shop edits it into version 2, and the registered
text is never rewritten by a later build (a new built-in is a new name). Its script,
in Code Mode over the host functions, does what a person would:

```python
ev = trigger()                       # OrderChanged, status "accepted" by the trigger's match
order = tool("order_get", {"id": ev.order_id})
for item in order["items"]:
    if item["status"] != "approved":
        continue
    out = queue_print({"order_item_id": item["id"]}, {})   # §5.4: the accepted quote's file
    if out["error"] == "requote_needed":   # PrintOutcome always carries error (None when it queued)
        quote = tool("order_item_quote", {"order_id": order["id"], "item_id": item["id"]})
        # in a flow the tool follows its operation to the answer (§7.3); this is the quote
        rise = quote["total_minor"] - item["accepted_quote"]["total_minor"]
        if rise > inputs().auto_accept_quotes_under_minor:     # past the shop's threshold: ask
            a = wait_for_human(f"{item['title']} re-quoted at {quote['total_minor']} minor units "
                               f"(accepted {item['accepted_quote']['total_minor']}). Accept and print?")
            if a.answer.strip().lower() != "yes":
                continue
        tool("order_item_accept_quote", {"order_id": order["id"], "item_id": item["id"],
                                         "quote_id": quote["id"]})   # covered by the grant: no second park
        out = queue_print({"order_item_id": item["id"]}, {})
    if out["status"] != "succeeded":
        wait_for_human(f"Print of {item['title']} did not queue: {out['error']}")
done = agent("Check every printed item of this order, add the finished photo and the "
             "timelapse to its fulfilment, and tell me what failed.",
             skills=["print"], result_schema=FULFILMENT_REPORT,
             grant={"rules": [{"fn": "tool", "tool": "order_item_fulfil",
                               "args": [{"path": "/order_id", "in": [order["id"]]}]}]})
if inputs().notify:
    tool("order_notify_customer", {"order_id": order["id"], "template": "ready"})  # outward
```

What this relies on, and what the design adds for it:

- `queue_print`'s source gains a third kind, `{"order_item_id"}`: the call is sent to
  `order_item_print` (§5.4), which fills the request from the accepted quote, checks
  the file's digest and sets the run's subject; the request argument may only narrow
  it (a printer, fewer copies, a spool of the slot's own filament, §5.4). The item's status then follows the print with no
  further script work, and a stale quote comes back as `requote_needed`, never as a
  print of the wrong file (Ruling 2).
- A person is asked only where the grant does not cover the call: `queue_print` is
  covered by the trigger's standing grant (`printers`, `max_prints`, `max_filament_g`,
  F13), and the order tools that are `outward` (§7.3: `order_accept`,
  `order_item_accept_quote`, `order_item_fulfil`, `order_ship`,
  `order_notify_customer`) only by a rule naming each. The built-in's trigger is
  expected to carry a standing grant naming `order_item_accept_quote` and
  `order_item_fulfil` (any order), so the script's accept after a person's "yes" does
  not park a second time; a rise under `auto_accept_quotes_under_minor` is accepted
  with no question at all, which is the one place the script spends that grant
  unattended. A flow's `tool()` gates only an
  outward tool (F11), so those are the tools the new bound reaches: `GrantRule` gains
  `orders: [ids]`, so a standing grant can say "any order" or a run grant "this order
  only", and the flow narrows the grant it hands the child agent to this order (10-11
  §4.3: narrow, never widen). The `write` order tools (a customer, an item, a quote, an
  attachment) are ungated in a flow as every library write is, scoped by the
  principal and nothing else; the bound does not pretend to cover them. In the
  script above the child's grant names `order_item_fulfil` on this order, and it adds
  the photo and the timelapse with `order_attach`, a `write` tool that needs no rule.
- The run is one per order and loops over the items, as the 10-01 spec says a
  campaign does. **No subflow.** Where a shop wants one run per line item (parallel
  printers, independent resets), the script starts one with
  `tool("start_flow_run", …)`, which flows can already call (F11), and this design
  adds the link that call lacks: `workflow_runs.parent_run_id`, set when the starter is
  a run, shown in the Flows tab as a tree, with the child's grant seeded as a copy of
  the parent's narrowed to the item's order, and `get_flow_run` for the parent to wait
  on. That is the "subflow" the user asked about, kept as a run that is not a child
  workflow (Ruling 21): a parent's Reset does not reset its children, and a child
  outlives a terminated parent and says so.
- `on_hold` on the order makes every tool that advances it (accepting, a quote's
  acceptance, an attempt, a shipment, closing) answer 409 `order_on_hold`, from the
  lifecycle (`orders/lifecycle.py`), never from each tool; `release`, the cancels, a
  payment and the plain edits go through, so a person can fix what the hold was for
  (Ruling 14 of the phase 1 plan). A script sees the error and `wait_for_human`s, or
  the shop's own script sleeps and retries.

### 7.3 Agent tools

`agent/src/tools/orders.ts`, one `defineTool` per route of §4, in three tiers:

- `read`: `customer_list`, `customer_get`, `order_list`, `order_get`, `order_costs`,
  `price_book_get`.
- `write`, a record the shop keeps: `customer_upsert`, `order_create`, `order_update`
  (notes, due date, addresses, charges, hold and release, and payments: a payment row
  records money the shop already received and sends nothing; a release is `write` as
  the hold is, since it commits nothing to the customer and every step it lets through
  is `outward` below, #4460), `order_item_add`,
  `order_item_update` (a `PATCH`, or the delete of a draft item), `order_item_quote`,
  `order_attach` (by `workspace_path`, as uploads are, #2329), `price_book_set`.
- `outward`, a step that commits the shop to the customer or counts work as done:
  `order_accept`, `order_item_accept_quote` (the price), `order_item_fulfil` (the
  made quantity), `order_ship`, `order_close`, `order_cancel` (both `POST
  /orders/{id}/cancel` and `POST …/items/{item}/cancel`: a cancel is terminal and commits
  the shop to the customer as an accept does, so the agent never cancels without a
  person or a grant rule naming the tool; Ruling 14 of the phase 1 plan), and
  `order_notify_customer`, which composes from a
  message template and sends through a plugin the shop configured (an email or
  messaging MCP plugin, #297), or, with none, returns the text for the person to send.
  In the panel an outward call parks for the person as any does; in a flow it is
  covered by a grant rule naming it (§7.2) or parks the same way.

`order_item_quote` runs an operation (§4). The agent's tool follows it as every command
tool does (`tools/command.ts`: the answer, or `{status: "running", operation_id}` past
the follow window); in a flow the activity follows it to the answer, as `queue_print`
follows a print run, so a script gets the quote. Every route of §4 is covered, or named
in `coverage.ts` with a reason.
Activity kinds `order.quote`, `order.print`, `order.fulfil` record the work by
principal, and a route attributes it with `attribute_request` as `render_model` does.

### 7.4 What a person sees

The Flows tab shows the run with its steps and the order it is for (the fire's facts
carry `order_id`); the order's page shows the run and its parked calls, with the same
decide controls the run's own page has. An order's audit line (§3.2 `order_events`)
records what the flow did under which run id.

## 8. The Bambuddy integration

All of it in `orders/bambuddy.py`, reached only through an `OrderIntegrations` seam
whose Bambuddy-less implementation does nothing and answers "not linked". It is chosen
at start from `bambuddy_url` (F10) and re-chosen on `settings.changed`, so setting the
URL later lights it up without a restart, and nothing in `orders/` imports
`bambuddy.client`.

### 8.1 Nested projects

On `accept`, a Bambuddy project named by the order number is ensured (`ensure_project`,
`bambuddy/projects.py:265`), and one per line item nested under it (`parent_id`, #930);
`order_projects` (`subject`, `project_id`) keeps the mapping. A print of an item lands
its 3MF in the item's project folder and sets `project_id` on the queue item, as prints
do today, so Bambuddy's own project page shows the order's tree. A project is never
deleted by ScadBuddy on cancel; the mapping is dropped and the project left to its
owner.

### 8.2 Archives

When an item's print settles, `print_links` (F8) names its archive; the fulfilment
attempt copies `archive_id`, outcome, times and grams, and offers the archive's
`finish_` photo and timelapse as one-click copies into §6.2's attachments (bytes pulled
once through the existing proxies, then ScadBuddy's own blob). Nothing is promoted to
Bambuddy from an order: the project `Media/` folder copy that `print_attachments`
makes is that feature's, not this one's.

### 8.3 Spool prices

The price book's filament records can be **imported from spools**: `POST
/orders/price-book/import-spools` reads the inventory (`inventory_view`), groups by
`_filament_key`, and makes or updates one filament record per group with the spools'
`cost_per_kg` (the most recent spool's, with the spread shown when they differ) and
the colour. It is an import, run on click: the book never reads Bambuddy at quote
time, so a quote is the same with Bambuddy down.

### 8.4 Printers

Until #3823's registry, an attempt's `printer` is the Bambuddy printer id and name the
run used. When the registry lands, the serial is the key and the Bambuddy id is one of
its paths, as the own-store spec §6 says.

## 9. Pages: generalising template-owned UIs (and why orders is not one)

The user asked whether the template-UI work can give "generalised UI pages with
backend services and workflows". Read against F14 and F15:

- **The loader generalises as it is.** `TemplateUi.tsx`'s mount (dynamic `import()`,
  a shadow root with the app's styles adopted, the 15 s deadline, a failure the shell
  reports) has nothing template-specific in it. The serving route's content checks
  (`_ui_file`: suffix allowlist, no dotfiles, no path escape, 404 for every failure)
  generalise too.
- **The Host API and the route do not.** Host API v1 is one template's inputs, and
  `models/{slug}/ui/**` is keyed by a model. A page over orders needs records, an API
  and runs, not `inputs.set`.
- **A module already has the user's API.** `connect-src 'self'` and no sandbox mean a
  page module can `fetch('/api/v1/orders')` as the user today. A page mechanism adds
  no capability; it adds a place to put the module, a version, and a host that spares
  the module from re-implementing lists, forms and realtime.

So a **declared page** is a later amendment to the template-pipelines spec, shaped as
flows are: `data/pages/<name>/page.js` plus `page.json` `{"api": 2, "route":
"/p/<name>", "title", "nav": true|false}`, one commit per version in a `PageHistory`
over `GitRepo` (as `FlowHistory` is), served at `/api/v1/pages/{name}/ui/{path}` and
`/pages/{name}/versions/{commit}/ui/{path}` with `template_ui.py`'s checks, mounted by
the shell at `/p/:name` through the same loader, with **Host API v2**, subject-neutral:
`host.api.request(path, init)` (same-origin, typed by the generated schema, the one way
to the API so a page's calls are traced and attributed), `host.records.<type>` (list,
get, put, subscribe over a **declared record type**: a JSON Schema in the flow inputs
subset, F15, stored in a `records` table as `(type, id, body jsonb)`, with the flow
inputs form rendering it, the generic "backend service" a page gets without code),
`host.flows.start(name, inputs)` / `host.flows.run(id)` / `subscribe`, `host.realtime
.subscribe(topic)`, `host.describe`. A page's `{api: 1}` module keeps working under the
template host, and a template's `ui/` may declare `api: 2` to get the neutral host
beside `inputs`. That amendment is its own spec; this one only fixes the shape so
orders does not foreclose it.

**Orders is built as first-party React pages**, not as a declared page: it needs money
columns, state machines and foreign keys (§3), which a `records` jsonb store should not
carry, and the shell's own table, filter, drawer and sheet components (the Activity
page's pattern, `pages/ActivityPage.tsx`, `components/activity/`, which this work
extracts into generic `DataTable`, `FilterBar` and `RecordDrawer` components so the
customers, orders and item pages are three thin pages over them). A declared page is
the right vehicle for what comes *after* orders exist: a branded order form or a
customer-facing status page a shop writes for itself over the orders API, which is
exactly the kind of UI a template-owned editor is for a template.

Pages: `/orders` (list, filters, totals), `/orders/:id` (the order: items with quotes,
charges, payments, shipments, the run, the audit line), `/customers`,
`/customers/:id`, Pricing (a tab of Settings). Mocks under
`src/mocks/features/orders/`. The Customize page gains "Add to order…"; an output's
menu "Order this"; the print detail page "Attach to order item".

## 10. Rulings

1. **An order names a commit, never HEAD.** A line item is reproducible years later
   (F2). Re-pointing an item at a newer commit is an edit that drops its quote.
2. **The sliced file is the quote's and the attempt's.** Both keep the digest; the
   attempt keeps the bytes as a blob with a ref. A print whose file digest differs from
   the accepted quote's is refused.
3. **Prices live in ScadBuddy and are copied into the quote.** No quote reads
   Bambuddy; §8.3 is an import.
4. **Grams come from `slice_info.config`, through one reader,** whichever slicer wrote
   the file. The Bambuddy slicer path and the native one price identically.
5. **The order's status is derived past `accepted`.** No route writes `ready`,
   `in_production` or `closed`; `close` is a check that can refuse.
6. **Nothing the process produced is deleted.** Items past `draft` cancel, quotes
   stay unaccepted, attempts keep their provenance, customers archive. A `draft` item
   and a mis-uploaded attachment are the two things a person may delete, and the
   order's audit line records each.
7. **Bambuddy is reached through one seam, and only when configured.** `orders/`
   never imports the client; the seam's null implementation is the one without
   `bambuddy_url`.
8. **Media an order keeps is ScadBuddy's blob,** with a `blob_refs` row, never a
   Bambuddy id alone (F9 is the counterexample this design does not repeat).
9. **One run per order by default; a run per item is a linked run, not a child
   workflow** (Ruling 21 of the 10-11 spec stands; `parent_run_id` is a record, not a
   Temporal parent).
10. **A flow asks only outside its grant.** The grant vocabulary gains `orders`; it
    stays closed and never reads inputs (F13).
11. **Money is integer minor units; rates are decimals.** Floats never enter a quote.
12. **No body names a principal.** `created_by`, `by` and the run's starter come from
    the author header, as flows' do.
13. **A trigger kind is a bus event's name.** Narrowing is a `match` on the event's
    fields; no kind is invented for one value of a field.

## 11. Phases

Each phase is a PR series with its plan under `docs/superpowers/plans/`; each lands
usable on its own.

1. **Customers and orders** (§3.1-3.3, §3.5 without print states, §4's reads and
   writes, the three pages, agent tools, mocks) (#4249). An item can be added from Customize
   and from an output. Close works with manual fulfilment (§6.1 by hand), so §6.3's
   shipment tables and routes land here too (Ruling 2 of the phase 1 plan): `shipped`
   and `closed` need them.
2. **Pricing** (§5) (#4250): the price book, the `order_item_quote` operation over
   `slice_info.read_sliced` (F4, on main), accept, the rough estimate. Slices through
   the Bambuddy slicer until #3822's remaining parts put the native one in
   `PrintRunWorkflow`; the reader is the same either way.
3. **Fulfilment and provenance** (§6) (#4251): attempts from the print path (`subject`
   `order_item:<id>`), the `attachments` table and routes (the packed photo among them),
   costs; shipments came with phase 1.
4. **Flows** (§7) (#4252): the two bus-named trigger kinds, `match`, the subject generalisation, `trigger()`
   models, the `orders` grant bound, `parent_run_id`, the built-in `order-fulfilment`
   flow, `order_notify_customer`. Needs #2484's trigger work, which is in.
5. **Bambuddy mirrors** (§8) (#4253): the seam, nested projects, archive copy-in, spool import.
6. **Declared pages** (§9) (#4254): its own spec, after phase 1 has extracted the generic
   table, filter and drawer components the page host would offer.

## 12. Open decisions

- **Tax.** A charge a person or the flow enters (§3.2), or a `tax_percent` on the
  policy applied to the subtotal. The first is proposed: it is right everywhere and
  the flow can compute the second.
- **Quantity and copies.** One print run with `copies = quantity`, or one attempt per
  copy. One run is proposed, with `quantity` on the attempt; a failed copy out of five
  is a `partial` outcome and a new attempt for the rest.
- **Customer-facing notification.** Which plugin carries it is the shop's; the design
  only makes the tool outward. Whether a "customer approved the quote" step belongs in
  the lifecycle (a `quoted` → `accepted` transition the customer makes through a
  declared page) waits on §9.
- **Where the built-in flow's script lives.** Registered at start as version 1 of a
  named flow (proposed, §7.2), or as a hidden default the Flows tab offers to copy
  so a shop that never wants it never sees it.
