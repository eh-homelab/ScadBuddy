# Native task screens in the phone app (design)

Status: draft for the owner's approval, 2026-11-28. Builds on the shop-floor flows design
(`2026-11-24-shop-floor-flows-design.md` §6.3, §6.4; epic #7361, phase A2 #8062, PR #8181) and
the devices design (`2026-11-27-devices-design.md`, #8244).

## 1. Intent

The phone app (`app/ScadBuddy`, .NET MAUI) is the Android form of ScadBuddy's UI. Today it is a
WebView of the web pages plus native NFC and camera. A flow's question (a `prompt()` task) reaches
the phone only if the person opens the right web page.

The owner wants (2026-11-28):

- Identify and bind work (a tag read, a barcode scan, "is this Spool #57?") done by flows, not by
  page-specific code: "So this should all be done via a flow".
- The whole flow task experience, not only a form: "We need the whole experience for the UI for
  flows".
- On the phone, a **native** experience over the app's **existing socket**: "I was thinking a
  native UI experience over the websicket" (sic). No new push service for now: the owner chose to
  keep the existing socket (voice-typed as "keep existing society for now").
- Notifications: the app is the Android version of the UI and its notifications (voice-typed as
  "an android version of our girl and notifications").

So: when a flow opens a task, the paired phone shows a native notification and a native screen
for it, built from the task's schema and context, answered with the phone's own NFC and camera.

## 2. Goals

- G1. The app hears task events on its device socket and keeps a live list of the open tasks its
  owner may answer.
- G2. A new open task posts an Android notification with the app in front, behind, or the screen
  off (a foreground service keeps the socket; §6.3). A tap opens that task's native screen.
- G3. A native screen draws every task #8181 can draw, except custom `ui` modules: each schema
  type and `x-sb-widget`, and each context block.
- G4. `scan` uses the phone's NFC (the reader the app has) and camera; `photo` uses the camera.
- G5. The answer goes through the same server check as a web answer, and `answered_by` names the
  device's owner and the device.
- G6. A native "Tasks" entry in the app's toolbar shows the count and opens the list.
- G7. A task the app cannot draw natively opens the web task screen in the app's WebView.

## 3. Non-goals

- Push while the app is closed (FCM, ntfy). The owner chose the existing socket for now. With the
  app closed, the person sees the task the next time the app opens.
- Custom task screens (`ui="module.js"`) drawn natively. They open in the WebView (G7).
- The identify/bind flow itself and the `tag.read` / `code.scanned` trigger events. They are the
  first consumer of this design and get their own design and PR (§9).
- New answer rules. Who may answer stays as #8181 and `flow_answerers` define it.

## 4. Anti-goals

- A second task model, store or channel. Tasks stay `tasks` rows; events stay `task.changed` on
  the bus; the app reads the existing realtime socket (repo rule: reuse existing infra first).
- A device credential that can call person routes in general. A device gets task routes of its
  own, scoped to its owner, as it got call routes (#8244).
- Task content in a notification beyond what a lock screen may show (§6.5).

## 5. Constraints

- New server routes go in Go core (#7667). Only core and the Python API hold SQL. Python keeps the
  answer check, the run update and the attachment rows (`api/tasks.py`, `tasks/answer_check.py`,
  `api/entity_attachments.py`); core calls them, it does not port them.
- A device token (`sbdev_…`) is checked only by core's device routes (`devices/areas.go`). The
  gate treats it as anonymous elsewhere (`auth/caller.go:120-153`); on prod `SCADBUDDY_AUTH_MODE`
  is never `enforce` (owner, 2026-11-17), but the design must hold under `enforce`.
- A device socket may subscribe only to `device:<id>` (`realtime/device.go:120-142`).
- Permissions are settings or grants with a safe default, never a hardcoded rule (owner,
  2026-11-24).
- Images move as blobs and `{name, mediaType}` pointers, never base64 (#2479).
- The app is one WebView page today (`MainPage.xaml.cs`); native screens are new MAUI pages.

## 6. Design

### 6.1 Approaches

| | How | Trade-off |
|---|---|---|
| **A. Native renderer over device task routes (chosen)** | Core adds device-scoped task routes and lets the device socket follow task topics. The app draws the schema natively. | Native feel, NFC and camera in the form, works with the page closed. Two renderers (web and app) to keep in step with #8181's widget list. |
| B. WebView task page plus native notifications | The app shows the web task screen; only the notification is native. | One renderer, but not native, and the owner asked for native. |
| C. Server-rendered UI description | The server turns a task into a UI tree the app draws. | One source of layout, but a new UI language to build and keep. |

The owner chose A (2026-11-28).

### 6.2 Server: what a device may do with tasks (Go core)

A new setting, **`device_task_answers`** (Settings → Devices), default **on**: a paired device may
read and answer the tasks its owner may answer. Off, the routes below answer 403
`device_tasks_off` and the socket refuses the task topics. Each device row also gets
`tasks_enabled` (default true), shown and switchable on the Devices page, so one phone can be cut
off without unpairing it.

Device routes. Each is an **Exact** path (ids go in the query), class `public` to the gate, with
the token checked in the handler as the call routes do, so the devices design's narrow public
list on the SSO host (#8244 G8) grows by exact paths only, never a prefix or regex (#4368):

- `GET /api/v1/devices/tasks` — the open tasks this device follows (§6.3 "Which tasks"), oldest
  first, each as `TaskQuestion` (#8181) plus `native: bool` (§6.4) and `fallback_url` (the web task
  screen's path on the device's ScadBuddy origin).
- `GET /api/v1/devices/task?id=` — one task, plus `context_view`: each `entity` context block as
  a card `{ref, kind, label, code, title, subtitle, rgba}`. A spool, spool type, item, item kind or
  place gets its code, name and colour from core's code resolve (the same lookup `/codes/{code}`
  uses, which core owns after #8470); any other kind gets `label` only, the text the web block
  shows. Core reads no new table for it.
- `POST /api/v1/devices/task-answer?id=` — body `{answer}`, an optional `Idempotency-Key`.
  Core forwards to a new internal CRUD route in Python,
  `POST /api/v1/internal/crud/devices/tasks/{id}/answer`, with core's service credential and
  `{answer, owner, device_id}`. Python runs the same checks as `POST /tasks/{id}/answer` and records
  `answered_by = "<owner> via device <device_id>"`. The device's owner is admitted as a person
  (`BY_SETTING`), so a `people_only` task is answerable from the owner's own phone. Replies and
  errors are the public route's (200, 202, 409 `not-open`, 422 `invalid-answer` with `problems`).
- `POST /api/v1/devices/task-photo?id=` — raw bytes of a photo (JPEG, PNG, WebP; sniffed as
  `devices/reads.go` `sniff` does), at most the web upload's size. Core stores the blob and
  forwards to Python's attachment insert for subject `task:<id>`, role `answer`; it answers
  `{attachment_id}`, the value a `photo` answer takes.
- `POST /api/v1/devices/task-scan?id=&field=` — what the phone read for a `scan` or `entity`
  field: `{tag: <the app's reading, as a tag_dump>}` or a photo's raw bytes. Core decodes it with
  the code it already runs for device reads (`tagreads`, `scan`) and answers `{code, resolved}`,
  checked against the field's `expects`. The phone never calls the person routes
  `POST /filament/tags/reads` or `POST /scan/codes`.
- `GET /api/v1/devices/task-image?id=&attachment=` — an `image` context block's bytes, only for
  an attachment that task's context names.

Socket: a device session may also subscribe to `tasks` and `task:<id>` while `device_task_answers`
and the device's `tasks_enabled` are on (`topicAllowed`). The frames are the existing
`task.changed` events (`task_id`, `run_id`, `state`, `place`): ids only, no content.

### 6.3 App: list, notification, screen

- **Which tasks.** A device follows the open tasks whose `assignee` is null or its owner, and
  whose `place` is null or one of the device's `places` (a per-device list on the Devices page,
  empty = every place). The device's owner is the principal recorded at pairing; the builder
  checks it is the same string `prompt(assignee=…)` and `?assignee=me` use, and maps it if not.
- **In front and behind.** The task socket runs in an Android foreground service ("ScadBuddy is
  listening for questions", a notification that stays on screen) from pairing until the app is
  swiped away, so questions notify with the app behind or the screen off (owner, §8). The NFC
  calls keep their rule: the heartbeat sends `foreground`, and core sends a call only to a phone
  with the app in front, so a web page never waits on a phone that cannot read a tag. The service
  needs `FOREGROUND_SERVICE` and the `dataSync` type's permission
  (https://developer.android.com/develop/background-work/services/fgs/service-types).
- **Live list.** On connect (and on every `resync`), `DeviceLink` subscribes to `tasks` and reads
  `GET /devices/tasks`. Each `task.changed` reads that task again (or drops it when not open). The
  list drives the toolbar entry "Tasks (n)" (G6) and the notifications.
- **Notification (G2).** A task that becomes open and was not in the list posts an Android
  notification on the channel "Questions for you", tag = task id. A task that leaves `open`
  cancels its notification. Android 13+ asks `POST_NOTIFICATIONS` the first time a task arrives.
- **Screen (G3).** A MAUI page per task: the title, the context blocks, the form, and the submit
  button. A 422 shows each problem under its field; a 409 says the task was answered elsewhere and
  closes the page.
- **Fallback (G7).** A task with `native: false` opens `fallback_url` in the WebView, with a
  native "Back" to the list.

### 6.4 Widgets on the phone

`native` is false when the task has a `ui` module or a field the table below does not draw.

| Schema | Native control |
|---|---|
| `string` (+ `format` email, date, date-time) | text entry with the matching keyboard or date picker |
| `string` + `enum` | segmented buttons up to 5, a list past 5 |
| `boolean` | switch |
| `number` / `integer` + bounds | stepper, or a number entry |
| `array` of scalars | one entry per line |
| `x-sb-widget: confirm` | one large button; the answer is `true` |
| `x-sb-widget: scan` (+ `expects`) | a code entry, "Hold a tag" (the app's NFC reader) and "Scan a code" (the camera), both sent to `task-scan`; the decoded code is shown with what it names before it is sent |
| `x-sb-widget: photo` | the camera; the photo goes to `/devices/tasks/{id}/attachments` |
| `x-sb-widget: entity` (`spool`, `location`) | as the web form draws it (#8181 `ScanField`): type or scan a code, then the resolved card |
| `x-sb-widget: weight` | grams entry (no scale on the phone) |
| `x-sb-widget: file`, `entity` of `library_file`, an unknown widget | not native: fallback |

Context blocks: `entity` (the card from `context_view`), `text` (Markdown, rendered as plain
paragraphs, lists and emphasis), `image` (the attachment's blob), `table`, `checklist`, `expect`
("Expect 640 g ± 20 g").

### 6.5 Notification content

Title: the task's title. Text: the first `entity` card's title, if any. Lock screen visibility
`private` (Android shows "ScadBuddy: Question for you" until unlocked). No answer actions on the
notification in this phase.

### 6.6 What changes where

| Area | Change |
|---|---|
| Go core `devices` | settings check, `tasks_enabled`, `places`, the six task routes, `foreground` in the heartbeat, the internal CRUD call |
| Go core `realtime` | device topics `tasks`, `task:<id>` behind the setting |
| Python | the internal device answer route; the attachment insert reused; `answered_by` text |
| Migration | `devices.tasks_enabled boolean not null default true`, `devices.places text[] not null default '{}'` (a new file) |
| eh-homelab/clusters | the six exact paths added to the device routes the SSO host passes without sign-in |
| App | `DeviceLink` task list; `TaskListPage`, `TaskPage`, widget and block views; notifications |
| Frontend | Devices page shows and switches `tasks_enabled`; Settings → Devices shows `device_task_answers` |

## 7. Testing

- Go: route tests for owner scoping, the setting off, `tasks_enabled` off, a revoked device, the
  forward to Python, and the socket's topic rule.
- Python: the internal route answers as the owner, refuses a closed task, records `answered_by`.
- App: the schema-to-control mapping and the `native` decision are pure (`DeviceProtocol`-style
  files) and unit-tested in `ScadBuddy.Tests`; a fixture set of #8181's task shapes is shared with
  the frontend's widget tests.
- On the owner's phone: a flow that asks confirm, enum, scan (tag and barcode), photo and entity;
  answered from the notification with the app in the background.

## 8. Decided by the owner

- Native UI over the existing socket; no push service for now (2026-11-28).
- Approach A (2026-11-28).
- A foreground service keeps the task socket open with the app behind (2026-11-28), at the cost
  of a notification that stays on screen and some battery.

## 9. Left to the builder

- MAUI page structure and styling, following the web app's tokens where MAUI allows.
- How the "Tasks" toolbar entry looks.
- Retry and backoff of the task list reads.
- The identify/bind flow (tag read and code scan events, the flow script, its `prompt()` calls) is
  a separate design. It reuses the core resolve built for #8470 (an exact property value resolves
  to its spool, type or place) and this design's screens.

## 10. Calls I made (for the owner to check)

- `device_task_answers` defaults **on** and is per-device switchable: a paired phone is the
  owner's own, and it already reads and writes tags for them.
- A device answers **as its owner**, `people_only` tasks included, and `answered_by` names the
  device.
- With the app swiped away, no notification arrives (the accepted cost of no push service).
- The phone uses device routes only, each an Exact public path with the id in the query; it
  never calls a person route (secure by default, and it holds under `enforce`).
- An `entity` widget is drawn as the web draws it (type or scan a code), with no options list;
  context cards come from core's code resolve, and other kinds show their label only.
- Tasks with a `place` reach a phone only if that place is in the device's `places` (empty =
  every place).
- A phone counts as online for NFC calls only while the app is in front.
