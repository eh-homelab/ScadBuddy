# Orders Phase 3: Fulfilment and Provenance, the Rest

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Finish #4251 (epic #4248): what an order's fulfilment records and what it cost, on top of what phase 3 has already merged.

**Spec:** `docs/superpowers/specs/2026-10-18-orders-design.md` §5.4, §6.1, §6.2, §6.4, §7.3, §11 phase 3.

## What is already on main (re-checked 2026-11-03 at `d94b92c5f`)

The issue's scope was written before most of it landed. These items are done, and nothing here redoes them:

- §6.1 attempts by hand: `order_item_fulfilments`, `POST …/fulfilments`, `PATCH …/{f}`, `POST …/{f}/accept`, and `…/unfulfil` (#4249, #4952).
- §5.4 the item print path (#4719, PR #5149): `order_item_print`, `POST /orders/{id}/items/{item}/print`, the claim, `queued`, `printing`, `printed`/`failed` followed from the run (`orders/printing.py`), `requote_needed`, and `copies` set to the quantity still to print. The deferral comments the issue's sweep found (`orders/models.py:30`, `orders/__init__.py:9`, `orders/lifecycle.py:245`) were rewritten by that PR. `derive` was widened to "any item `queued` or beyond, or with an attempt".
- §6.2 attachments on an order and on a line item: `order_attachments`, the upload through the claim store and `order_attachment_add`, `GET` with `Range`, delete, caption, role, the delivered picture (#4661, #4722).
- §6.3 shipments, partial ones included (phase 1).
- §8.4 the attempt's printer (#5229), §8.1 nested projects (#5231), §8.2 archive copy-in (#5233): `archive_ids`, `actual_seconds` and `actual_grams` on the attempt, and `order_attempt_copy` copying a `finish_` photo or timelapse.
- Agent tools `order_item_fulfil`, `order_ship`, `order_item_print`, `order_attach`. Activity kinds `order.quote`, `order.print`, `order.attach` and `order.copy` as operation kinds (`orders/activity.py`).

## What this plan builds

Four PRs, each from fresh `origin/main`:

1. **§6.4 the real cost** (`feat(orders): the real cost of an order`, `Refs #4251`). Also adds this plan.
2. **An attempt's own files** (`feat(orders): attach files to a print attempt`, `Refs #4251`): the `order_item_fulfilment:<id>` subject, archive copies onto the attempt, and "Attach to order item" on the print detail page.
3. **§5.4's spool per slot** (`feat(orders): fill an item print's spools from its quote's filaments`, `Closes #4251`): `needs_spool_choice`, `spool_not_filament`.
4. **#5263**, the review follow-ups of #4253 (`fix(orders): …`, `Closes #5263`), on its own.

## Rulings

1. **The real cost is a read over the rows, priced at the quote's own rate.** `GET /orders/{id}/costs` answers, per item, its accepted quote beside its attempts (an item with none accepted shows no quoted figures). A `slice` quote's plates give the quoted seconds and grams per unit, and its `filament` lines over its grams give the quote's own price per gram. An attempt's `actual_grams` (§8.2, the archives' sum) is priced at that rate, failures included, because a failed print used filament too. A `manual` quote prices no grams, so its item shows actual figures with no filament cost. Quoted figures are per unit times the item's quantity; actual figures are the sum over every attempt. Nothing is stored, and nothing re-reads the price book: the price book in force today is not what the shop quoted.
2. **Bambuddy's cost is shown beside the sums and never added to them.** The seam gains `archive_cost(archive_id)`, which reads the archive and its runs. A run whose grams are a spool's weight (#950, `SUSPECT_RUN_GRAMS_RATIO`) has its overstatement taken back out, as the print page does. That correction moves out of `api/print_history.py` into `bambuddy/costs.py`, so both callers share it. Without Bambuddy, for a deleted archive or when Bambuddy is down, the attempt's `bambuddy_cost` is `null`. When Bambuddy could not be read, the answer says so in `bambuddy_unavailable`, and the route still answers.
3. **An attempt's files are attachments with the subject `order_item_fulfilment:<id>`.** The table's subject check widens to that subject in a new migration. The upload route takes `fulfilment_id` beside `item_id`. `OrderDetail.attachments` carries `fulfilment_id`. An archive's copy lands on the attempt it came from, which closes #5263's "copies attach to the item". Removing an attempt is not a route, so no cascade is needed. The item's delivered picture may be an attempt's image as well as the item's own.
4. **The spool per slot is filled at the print's start, from the quote's filaments.** A slot's filament (`colours[].filament_id`, §3.3) is matched to spools by `_filament_key`'s identity, as the spool import matches them (§8.3, #4253). With Bambuddy, each slot takes the spool loaded on the run's printer of that filament. With none loaded, it takes the unarchived shelf spool of that filament with the most grams left. A spool loaded in another printer is never taken. With no spool at all, the print is refused with 409 `needs_spool_choice` naming the slots. A request's own `filament_plan` slot must be a spool of that slot's filament, else 409 `spool_not_filament`. A slot the request leaves out is filled. On the native path the plan is left as the request gave it. The check runs in the operation's check, so a refusal writes nothing.

## Not in this plan (kept deferred, with why)

- **Printing the quote's sliced file itself** (§5.4 "Not yet"). `PrintRunWorkflow`'s source is an output or a library file (`workflows/print_models.py` `SourceSpec`). Printing a quote's pre-sliced plates needs a third source kind on both the Bambuddy and the native LAN queue path, and that is a workflow change of its own. Meanwhile the print reuses the quote's own background slice when the choices are the quote's (`bambuddy/preview.py` `reusable_slice`). The attempt records and holds the quote's file. This stays tracked on #4251's epic (#4248).
- **An accepted `manual` quote's attempt recording its run's file.** It waits on the same source work, since the run's result names a Bambuddy library file and not a digest in ScadBuddy's store.
- **An `order.fulfil` activity kind.** Accepting an attempt is a plain route, not background work, and the `activity` table holds background work only (spec 2026-10-09 §2). The accept is on the order's audit line.

## Review focus

- The cost route never sums Bambuddy's cost (`test_bambuddy_cost_is_shown_never_summed`).
- A failed attempt's grams are in the actual cost (`test_failed_attempts_count_in_the_actual_cost`).
- An archive copy lands on its attempt (`test_copy_attaches_to_the_attempt`).
- A spool loaded in another printer is never taken (`test_a_spool_loaded_elsewhere_is_never_taken`).
