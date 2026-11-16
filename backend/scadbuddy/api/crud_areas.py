"""``GET /api/v1/internal/crud/<caller>/areas`` (#5298, plan "Rolling deploy"): the CRUD
areas this API pod serves to that caller. A new worker waits until it lists every area
the worker needs before it polls Temporal; the storefront edge (#5282) waits the same way
before it serves. Each area's task appends its name to `CRUD_AREAS` once the area is
complete."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from scadbuddy.crud.guard import CrudCaller, crud_router

#: Each caller's complete areas. The render ``assets`` area also covers
#: ``…/render/blob-index/{key}/forget-copy``, added with it (#5298 Task 18). The print
#: ``runs`` area also covers ``…/print/outcomes``, added with it (#5298 Task 24). The print
#: ``printer-ensure`` area is Task 29's ``…/print/printers/ensure-bambuddy`` alone, taken
#: early by #4625 so a merge never runs on the worker's role. The print ``reprints`` area
#: is ``…/print/reprints``, the queue item a reprint queued (#3741). The storefront ``pay`` area
#: is ``…/storefront/pay/checkout`` (#4960), which the edge's ``/pay#<token>`` page reads.
#: The storefront ``turnstile`` area is ``…/storefront/turnstile`` and
#: ``…/turnstile/verify``, the bot check (#5283). The storefront ``accounts`` area is
#: ``…/storefront/account`` and its ``verify``, ``sign-out`` and ``orders`` (#5283). The
#: storefront ``order`` area is ``…/storefront/order/*``, the customer's order page (#5283).
#: The agent's ``workspace-blob-refs`` area is ``…/agent/workspace-blob-refs`` and its
#: ``/release`` (#5299), the holds of its workspace files' blobs. Its ``events`` area is
#: ``…/agent/events`` and ``…/events/head``, the event log's gap and head; its ``flows``
#: area is ``…/agent/flow-runs/*``, ``…/flow-decisions`` and ``…/flow-grants/in-force``,
#: its reads of flow runs, their decisions and grants (#5299 D7). Its ``settings`` area is
#: ``…/agent/settings``, ``ai_settings`` (#5299 step 4); its ``credentials`` area is
#: ``…/agent/credentials`` and every route under it, the Claude credentials sealed and
#: opened on the API, a reveal audited; its ``payload-keys`` area is
#: ``…/agent/payload-keys/{subject}``, a subject's data key released (made first under the
#: subject's lock, never for a forgotten one) and audited, never the KEK (#5299 D5,
#: #5342; `api/crud_agent_secrets.py`); its ``forget`` area is
#: ``…/agent/forgotten-subjects/{subject}``, ``…/payload-keys/{subject}/drop``,
#: ``…/payload-key-sweeps`` and ``…/flow-blobs/{subject}`` with its ``/{name}``: a subject's
#: tombstone with its key and flow images, a browser call's key dropped, the keys no forget
#: deletes swept, and a flow run's images kept and read (#5299 step 6;
#: `api/crud_agent_forget.py`). Its ``workspace`` area is every ``…/agent/workspace/*`` route:
#: the workspace files, sealed and opened on the API, their blobs' holds and their events
#: written in each write's transaction, a fork's copy, the move to blobs, the sweep and the
#: reconcile (#5299 step 7; `api/crud_agent_workspace.py`). Its ``session-blobs`` area is
#: ``…/agent/session-blobs`` and its ``…/deletes`` (a queued message's images, #6413), a
#: session's images and a durable tool call's, read through a
#: fork's chain; its ``attachments`` area is ``…/agent/attachments`` and every route under
#: it, the images the panel stages before a turn sends them; its ``http-bodies`` area is
#: ``…/agent/http-bodies`` and every route under it, a session's saved HTTP bodies read by
#: range (#5299 step 7; `api/crud_agent_blobs.py`). Its ``background-tasks`` area is
#: ``…/agent/background-tasks`` and every route under it, a session's background tasks, a
#: Monitor's events and a Workflow's journal and child chats; its ``operations`` area is
#: ``…/agent/operations`` and every route under it, ``ai_operations`` with its activity row
#: (#5299 step 8, `api/crud_agent_tasks.py`). Its ``sessions`` area is ``…/agent/sessions``,
#: ``…/agent/session-starts`` and every route under ``…/agent/sessions/{id}`` but
#: ``…/resources``: a session's row as the session manager reads and writes it (its insert
#: under the owner's start lock, a fork's under its parent's, the handoff, the budget, a
#: failed fork discarded); its ``session-resources`` area is ``…/agent/sessions/{id}/resources``,
#: what a session's tool calls touched (#5299 step 9, `api/crud_agent_sessions.py`). Its
#: ``transcripts`` area is ``…/agent/transcripts`` and ``…/agent/sessions/{id}/entries``,
#: ``…/transcript``, ``…/cwd`` and ``…/subkeys`` with every route under them: a session's
#: transcript, sealed and opened on the API, a fork's read through its chain (#5299 step 9,
#: `api/crud_agent_transcripts.py`). Its ``session-events`` area is
#: ``…/agent/sessions/{id}/events`` and ``…/fork-log`` with every route under them: a session's
#: event log, a fork's read through its chain (#5299 step 9, `api/crud_agent_events.py`). The
#: print ``uploads``
#: area also covers ``…/print/slices`` and ``…/print/slice-starts`` (#5298 Task 25). Its
#: ``previews`` area also covers ``…/print/project-targets``, ``…/print/folderless`` and
#: ``…/print/destination-touches`` (#5298 Task 26). Its ``copy-leases`` area is
#: ``…/print/copy-leases/{key}``, the copy lease (#5298 Task 27). Its ``links`` area is
#: ``…/print/links``, ``…/print/sends`` and ``…/print/last-prints`` (#5298 Task 28). Its
#: ``lan-imports`` area is ``…/print/lan-imports``, a LAN-only printer's print record
#: (#4699 phase 4). Its ``printers`` area is every other ``…/print/printers`` route (the
#: reads, a printer's access code, a virtual printer's sync) and ``…/print/reports``
#: (#5298 Task 29). Its ``rack`` area is ``…/print/rack``, the rack hotends' usage
#: (#5298 Task 30). Its ``spools`` area is ``…/print/spools``, ``…/print/native-spools``,
#: ``…/print/held-presets`` and ``…/print/native-slices`` (#5298 Task 31). Its
#: ``transfer-urls`` area is ``…/print/transfer-urls``, a LAN print's read URL of a
#: native slice's file (#5298 Task 32). Its ``access-codes`` area is
#: ``…/print/access-codes`` and ``…/access-codes/virtual-printer``: whether the API opens
#: access codes, and a virtual printer's sealed code opened (#5298 Task 33). The intake
#: ``sources`` area is ``…/intake/sources/{id}/read``, ``…/preview`` and ``…/check``: a
#: customer's staged file, checked by the intake worker (#6133). The agent's small stores
#: (#5299 step 5, `api/crud_agent_stores.py`): its ``activity-kinds`` area is
#: ``…/agent/activity-kinds``; its ``browser-origins`` area is ``…/agent/browser-origins``
#: and every route under it, a session's approved origins; its ``bridge-tabs`` area is
#: ``…/agent/session-tabs``, ``…/agent/tab-owners/*`` and ``…/agent/tab-images/{name}``
#: (the bytes in the body), which tab a session pairs with, which replica holds a tab and
#: a tab's images; its ``pairings`` area is ``…/agent/pairings`` and every route under it;
#: its ``mcp-tokens`` area is ``…/agent/mcp-tokens`` and every route under it, the MCP
#: bearer tokens by their hashes; its ``notify`` area is ``…/agent/push/*`` and
#: ``…/agent/notify-channels`` and every route under it, the Web Push records and the
#: notification channels, sealed and opened with the API's KEK
#: (`api/crud_agent_notify.py`). Its plugin stores (#5299 step 5): its ``access`` area is
#: ``…/agent/access/*`` and ``…/agent/tool-listings/*``, the tool access and the listings
#: a connection test stores, and ``…/agent/leases/{key}``, the leases the agent holds
#: across a connection test and a sign-in's token refresh (#5299 D3,
#: `api/crud_agent_access.py`); its ``plugin-sessions`` area is ``…/agent/plugin-sessions``
#: and every route under it, the upstream MCP sessions its forwarders hold; its ``plugins``
#: area is ``…/agent/plugins`` and ``…/agent/plugin-oauth`` and every route under them, the
#: remote plugins and their sign-ins, sealed and opened with the API's KEK
#: (`api/crud_agent_plugins.py`); its ``plugin-packages`` area is
#: ``…/agent/plugin-packages`` and every route under it, the installed packages' pins
#: (`api/crud_agent_packages.py`). Its ``gate`` area is ``…/agent/gate/*``: the tool-call
#: gate's writes, an entry opened, a run grant's approval checked against ``flow_grants``, a
#: flow's refusal, an entry resolved, a response decided against the row's record by the
#: gate's validator, and the record read, each with its session events, status, audit row and
#: ``scadbuddy_input`` NOTIFY in its transaction (#5299 step 10, `api/crud_agent_gate.py`). Its
#: ``pending-input`` area is ``…/agent/pending-input/*``: the open entries and their responses
#: read, a parked call's and a decision's span written, the first only while the durable turn's
#: placement holds the session, the orphan sweep's cancels (each with its events, status and
#: audit row in its transaction), and a call's recorded approval (`api/crud_agent_pending.py`).
#: Its ``audit`` area is ``…/agent/audit/*``: the audit log's rows written, listed and pruned
#: (`api/crud_agent_audit.py`). Its ``approvals`` area is ``…/agent/approvals/*``: the classic
#: approvals read, made, decided once (with their events and audit row in the decision's
#: transaction) and used once (`api/crud_agent_approvals.py`). Its ``questions`` area is
#: ``…/agent/questions/*``: the `done` summaries read, dismissed and posted (the post only while
#: the turn's placement holds the session), and the attention limit's count under its lease
#: (`api/crud_agent_questions.py`).
CRUD_AREAS: dict[CrudCaller, list[str]] = {
    "projects": ["flows", "operations", "payload-keys"],
    "render": ["settings", "jobs", "refs", "blob-index", "store-folders", "assets", "libraries"],
    "print": [
        "settings",
        "runs",
        "operations",
        "events",
        "printer-ensure",
        "reprints",
        "uploads",
        "previews",
        "copy-leases",
        "links",
        "lan-imports",
        "printers",
        "rack",
        "spools",
        "transfer-urls",
        "access-codes",
    ],
    "storefront": [
        "listings",
        "renders",
        "drafts",
        "requests",
        "pay",
        "turnstile",
        "accounts",
        "order",
        "models",
    ],
    "agent": [
        "order-costs",
        "workspace-blob-refs",
        "events",
        "flows",
        "settings",
        "credentials",
        "payload-keys",
        "forget",
        "activity-kinds",
        "browser-origins",
        "bridge-tabs",
        "pairings",
        "mcp-tokens",
        "notify",
        "workspace",
        "session-blobs",
        "attachments",
        "http-bodies",
        "access",
        "plugin-sessions",
        "plugins",
        "plugin-packages",
        "background-tasks",
        "operations",
        "sessions",
        "session-resources",
        "transcripts",
        "session-events",
        "gate",
        "pending-input",
        "audit",
        "approvals",
        "questions",
    ],
    "intake": ["sources"],
}


class Areas(BaseModel):
    areas: list[str]


def _areas_router(caller: CrudCaller) -> APIRouter:
    caller_router = crud_router(caller)

    @caller_router.get("/areas", name=f"crud_{caller}_areas")
    def areas() -> Areas:
        return Areas(areas=CRUD_AREAS[caller])

    return caller_router


#: The callers' routes, each built by its own `crud_router` (prefix, guard, headers),
#: in one flat router as `main.py` `_api_router` mounts every module's.
router = APIRouter()
for _caller in CRUD_AREAS:
    router.routes.extend(_areas_router(_caller).routes)
