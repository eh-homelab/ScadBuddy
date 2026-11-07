# Orders phase 4: Flows Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An order's process runs as a flow: triggers on `order.changed` / `order_item.changed`, a grant bound by order, `queue_print` of a line item, linked runs, and the built-in `order-fulfilment` flow (#4252).

**Architecture:** Four PRs off main, in sequence, each usable alone. PR 1 adds the trigger kinds, `match` and `subject`. PR 2 adds the `orders` grant bound and `queue_print`'s line-item source. PR 3 adds the run's links (parent run, order) and what a person sees. PR 4 adds the built-in flow and `order_notify_customer`.

**Tech Stack:** Python 3.12 (FastAPI, pydantic, psycopg, Temporal), TypeScript (agent tools, zod), React 19 (frontend), msw.

**Spec:** `docs/superpowers/specs/2026-10-18-orders-design.md` §7, §10 Rulings 9, 10, 13, §11 phase 4.

## Global Constraints

- A trigger kind is a bus event's name; narrowing is a `match` on the event's fields (Ruling 13).
- Event keys: `order:<id>:<status>:<change>` and `order_item:<id>:<status>:<change>`, one fire per row of `order_status_changes` since the trigger began to listen (`live_since`), never per event: a plain edit, a hold or a release fires nothing, and a return to a status fires again.
- The facts come from the order's row at fire time, never from a request body (§7.1, 10-11 §12.1).
- The grant vocabulary stays closed and never reads inputs (Ruling 10); narrow, never widen (10-11 §4.3).
- A linked run is a record, not a Temporal child workflow (Ruling 9).
- `ProjectWorkflow` is AUTO_UPGRADE: each host-surface change replays `tests/fixtures/project_workflow_histories/`.
- Migrations are new files only; only the API (CRUD service) runs SQL (#5295).

## Review Focus

- An order edited with no status change publishes `order.changed` again: one trigger fires once per status (the key), never once per edit.
- A stored trigger saved with `slug` keeps firing and reads back as a template subject.
- A `match` naming a field the kind does not have, or a status that does not exist, is refused at create, not silently never matched.
- A run grant seeded from an `order.changed` fire acts on that order only, never on another order the script names.
- A child run's seeded grant never has more than the parent's: a rule with a counter is not copied.

---

## PR 1: trigger kinds, `match`, `subject`, `trigger()` models

### Task 1: `EventTiming` subject and match

**Files:** Modify `backend/scadbuddy/flow_triggers/models.py`; Test `backend/tests/test_flow_trigger_models.py` (new).

**Produces:** `EventKind` adds `"order.changed"`, `"order_item.changed"`; `Subject{kind: Literal["template","order","order_item"], id: str | None}`; `EventTiming{kind, subject: Subject | None, match: dict[str, list[str]] | None}`; a `slug` input becomes `subject={kind:"template", id:slug}`; `matches(timing: EventTiming, facts: dict) -> bool`. The subjects an event is about come from `TriggerEvent.subjects` (Task 2).

- [ ] Tests: slug input reads as a template subject; a template subject on `order.changed` is refused; `order_item.changed` takes an `order` or `order_item` subject; `match` with an unknown field (`{"colour": [...]}`) or an unknown status (`{"status": ["acepted"]}`) is refused; `match` on `render.finished` is refused; `matches()` true for a listed status, false otherwise, true when `match` is None.
- [ ] Implement; run `uv run --frozen pytest tests/test_flow_trigger_models.py`.

### Task 2: the bus events as trigger events, matched in the store

**Files:** Modify `backend/scadbuddy/flow_triggers/events.py`, `flow_triggers/store.py`; Test `backend/tests/test_flow_trigger_events.py` (new), `test_flow_trigger_store.py`.

- [ ] Tests: `trigger_event(OrderChanged(order_id="o1", status="accepted"))` gives kind `order.changed`, key `order:o1:accepted`; an item event gives `order_item:i1:printed:2` (`-` for no attempt); `event_triggers` returns a trigger whose subject is the event's order (and, for an item event, the item's order), a legacy `{"kind", "slug"}` row still matches its slug, and a trigger whose `match` excludes the status is not returned; `order_facts` adds the order row's `customer_id`.
- [ ] Implement: `event_triggers(kind, subjects: dict[str,str], facts)`; `fire_event` reads the order row's `customer_id` through `TriggerStore.order_facts` and skips an order that is gone.

### Task 3: `trigger()` models

**Files:** Modify `backend/scadbuddy/workflows/flow_tools.py`; Test `backend/tests/flows/test_trigger_function.py`.

- [ ] Tests: a script reading `t['customer_id']` after `t['kind'] == 'order.changed'` type-checks; reading `t['item_id']` without telling the kinds apart does not.
- [ ] Implement `OrderChanged`, `OrderItemChanged`; the fall-through becomes `case "render.finished"` plus the two new cases.
- [ ] Run `tests/flows/test_project_replay.py` (needs Temporal).

### Task 4: agent and Triggers tab

**Files:** `agent/src/tools/flowTriggers.ts` (+ test), `frontend/src/components/flows/Triggers.tsx` (+ test), `frontend/src/mocks/features/flowTriggers.ts`.

- [ ] The agent's `event` takes the two kinds, `subject` and `match`; the Triggers tab offers them, with status checkboxes for the order kinds and an order or item id as the subject.

## PR 2: the `orders` bound and `queue_print` of a line item

- `GrantRule.orders: list[str] | Literal["any"] | None` bounds `queue_print` and `tool`; `CallFacts.order_id` is `args["order_id"]` for a tool and `source["order_id"]` for a print. `"any"` admits a call that names some order.
- A run grant seeded from an order or order-item fire has each `orders` bound narrowed to the fire's order (a list without it drops the rule).
- `queue_print` source `{"order_id", "order_item_id"}` posts to `POST /orders/{order_id}/items/{item}/print`, then follows the attempt's print run. A `requote_needed` refusal comes back as `PrintOutcome.error`, not as an exception. Behind `workflow.patched`.
- Agent `grantRule` and `GrantEditor` gain `orders`.

## PR 3: linked runs and what a person sees

- `workflow_run_links (run_id, parent_run_id, order_id)`, written by the API at a run's start (a run's own row stays the workflow's).
- The agent sends `X-ScadBuddy-Flow-Run` on a flow call's requests. The API believes it only with the agent's author headers, for an open run whose starter is the author. `start_flow_run` from a run records `parent_run_id` and seeds the child's grant from the parent's. Rules with counters are left out, and `orders` is narrowed to the body's `order_id`.
- Runs list `parent_run_id`/`order_id`; the Flows tab nests children; `GET /orders/{id}/runs`; the order page lists its runs and parked calls with the decide controls; an order's audit `by` carries `run_id`.

## PR 4: the built-in flow and `order_notify_customer`

- `orders/builtin_flow.py`: registered at start through `FlowHistory.register` when no `order-fulfilment` flow exists, `inputs.json` `{notify, auto_accept_quotes_under_minor}`, never rewritten.
- Agent `order_notify_customer` (outward): composes from a message template and returns the text for the person to send.
