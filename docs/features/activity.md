# Activity

## What it does

The Activity page and the bell list what happened and what is running: renders, sends,
prints, library writes, flow runs and the assistant's actions, each attributed to a
person, the assistant or a flow.

## How it works

Features write `activity` rows through the API (workers through their CRUD routes); the
agent registers its own activity kinds. The inventory's writes are Temporal standalone
activities in scadbuddy-core (#6949 K1). Core writes an `inventory_operations` row for each
and sends `inventory_operation.changed`; the API turns it into an `activity` row with one
kind per activity, `inventory.<Name>` (`backend/scadbuddy/inventory/activity.py`; for example
`inventory.ReceiveLot`, `inventory.Take`), linked to the item, lot, unit or kind page. The browser follows new rows over the realtime
socket.

## Code

- Route: `backend/scadbuddy/api/activity.py:46`; core `backend/scadbuddy/activity/`.
- UI: `frontend/src/pages/ActivityPage.tsx`, `frontend/src/components/activity/ActivityBell.tsx:61`,
  `ActivityDrawer.tsx:125`.
- Attribution header: `backend/scadbuddy/core/authorship.py` (the `AgentAuthorship`
  middleware), `agent/src/tools/authorship.ts`.
- Agent tools: `agent/src/tools/activity.ts`.

## Data

- Tables: `activity`, `events`, `ai_activity_kinds`.

## Tests

- Backend: `backend/tests/test_activity_*.py`, `backend/tests/api/test_activity.py`.
- e2e: `frontend/e2e/activity.spec.ts`.

## Limits and known issues

- Toasts for others' background actions are not yet grouped or capped
  ([#3189](https://github.com/eh-homelab/ScadBuddy/issues/3189)).
