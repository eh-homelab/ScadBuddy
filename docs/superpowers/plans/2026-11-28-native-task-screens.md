# Native Task Screens Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A flow's `prompt()` task reaches the paired phone as an Android notification and a native MAUI screen, answered with the phone's NFC and camera, over the app's existing device socket.

**Architecture:** Go core adds device-scoped task routes (token checked in the handler, as the call routes) and lets a device socket follow `tasks` / `task:<id>`. Core reads tasks itself (it holds SQL) and forwards an answer to Python's existing `POST /api/v1/tasks/{id}/answer` with a signed device assertion, so Python's answer check and run update stay the one path. The app keeps the socket in a foreground service, keeps a live task list, posts notifications, and draws each task natively from its schema, else opens the web station screen.

**Tech Stack:** Go (core, pgx, `httpx`, `crud`), Python (FastAPI, only the answer route's caller check), .NET MAUI 10 Android, React (Devices page), eh-homelab/clusters (Ingress).

**Spec:** `docs/superpowers/specs/2026-11-28-native-task-screens-design.md` (PR #8574)

## Global Constraints

- New server routes in Go core (#7667). Only core and the Python API hold SQL. Python keeps the answer check and run update (`api/tasks.py` `answer_task`).
- Device routes are class `public` to the gate; the handler checks the token (`devices.Store.Authenticate`) in every mode. Each is an **Exact** path, ids in the query, never a prefix or regex (#4368).
- Every device task route answers 403 reason `device_tasks_off` when the setting `device_task_answers` is false or the device's `tasks_enabled` is false.
- `device_task_answers` defaults to true. `devices.tasks_enabled` defaults to true. `devices.places` defaults to `'{}'` (every place).
- A device follows open tasks whose `assignee` is null or the device's `owner`, and whose `place` is null or in `places` (empty `places` = every place).
- `answered_by` for a device answer is `"<owner> via device <device id>"`, cut to `MAX_BY_CHARS`.
- Socket frames stay the existing `task.changed` (`task_id`, `run_id`, `state`, `place`): ids only.
- Images move as blobs and pointers, never base64 (#2479). No task content in logs.
- Notification channel name "Questions for you"; foreground notification text "ScadBuddy is listening for questions"; lock-screen visibility private.
- A migration is a NEW file `backend/scadbuddy/migrations/$(date -u +%Y%m%dT%H%MZ)_<slug>.sql`; edit nothing else there.
- Conventional-commit titles; commits end with the session's Co-Authored-By and Claude-Session lines.

## Review Focus

1. A revoked or expired device token on every task route and on the socket's task topics: 401 `device_unauthorized`, and the socket stops following.
2. A task answered on the web while the phone shows it: the phone's answer gets 409 `not-open`, the screen says so and closes, and the notification is cancelled.
3. A forged or replayed device assertion at Python (wrong task id, expired, wrong MAC, header sent by a browser): refused as an anonymous caller would be.
4. The app socket drops (Wi-Fi change, doze): it reconnects with backoff and reads the task list again, so no open task is missed and none notifies twice.
5. A task whose schema has one field the app cannot draw (`file`, `library_file`, an unknown widget, `ui` set): `native` is false and the phone opens the web screen, never a half form.

---

## Part A — server (PR 1: `feat(devices): device task routes for the phone app`)

### Task 1: Schema and setting

**Files:**
- Create: `backend/scadbuddy/migrations/<UTC>_device_tasks.sql`
- Modify: `backend/scadbuddy/library/settings_store.py` (the settings model), `backend/scadbuddy/core/setting_docs.py`, `backend/scadbuddy/api/settings.py` (the area that lists device settings; add one if none, named "devices")
- Test: `backend/tests/api/test_settings*.py` (the file that covers a boolean setting's default and write)

**Interfaces:**
- Produces: columns `devices.tasks_enabled boolean NOT NULL DEFAULT true`, `devices.places text[] NOT NULL DEFAULT '{}'`, `devices.front_at timestamptz`; setting `device_task_answers: bool = True` stored in `settings` (core reads `SELECT value FROM settings WHERE name = 'device_task_answers'`, as `labelprint/routes.go:109` does; a missing row is true).

- [ ] Step 1: test `test_device_task_answers_defaults_on_and_can_be_turned_off`: GET settings shows `device_task_answers: true`; PUT false; GET shows false.
- [ ] Step 2: run it; FAIL (unknown setting).
- [ ] Step 3: add the migration, the field, and the doc: label "Paired phones may answer flow questions", help "A paired phone reads and answers the flow questions its owner may answer, from notifications and its own screens. Each phone can also be turned off on the Devices page."
- [ ] Step 4: run the test and the migration tests (`backend/CLAUDE.md`); PASS.
- [ ] Step 5: commit `feat(devices): device_task_answers setting and device task columns`.

### Task 2: Device task reads in core

**Files:**
- Create: `tools/go/core/internal/devices/tasks.go`, `tools/go/core/internal/devices/tasks_test.go`
- Modify: `tools/go/core/internal/devices/areas.go` (routes), `tools/go/core/internal/scan/resolve.go` (export `Resolve`)

**Interfaces:**
- Consumes: Task 1's columns and setting.
- Produces:
  - `func (s *Store) tasksAllowed(ctx context.Context, d Device) (bool, error)` — setting and `tasks_enabled`.
  - `func Native(schema map[string]any, ui *string) bool` — false when `ui` is set or any property is not in the spec §6.4 table (`x-sb-widget` in `confirm, scan, photo, weight`, or `entity` with `kind` `spool`/`location`; plain `string` (formats email/date/date-time or none), `string`+`enum`, `boolean`, `number`, `integer`, `array` of scalars).
  - `GET /api/v1/devices/tasks` → `{"tasks":[DeviceTask]}`; `GET /api/v1/devices/task?id=` → `DeviceTask` plus `context_view`.
  - `type DeviceTask struct { ID, RunID, Title string; Schema map[string]any; Context []any; Place, UI, Assignee *string; ExpiresAt *time.Time; State string; CreatedAt, UpdatedAt time.Time; Native bool; FallbackURL string }` (JSON names as `TaskQuestion`: `id, run_id, title, schema, context, place, ui, assignee, expires_at, state, created_at, updated_at`, plus `native`, `fallback_url`).
  - `fallback_url` = `/station/<url-escaped place>` when `place` is set, else `/station`.
  - `context_view`: one entry per context block, in order; an `entity` block `"spool:<uuid>"`/`"location:<id>"`/… becomes `{ref, kind, label, code, title, subtitle, rgba}` from `scan.Resolve` (spool, spool_type, item, item_kind, location); any other block is `null` (the app draws it from `context`).
  - `scan.Resolve(ctx, q, text string) (*Resolved, error)` — the current `resolve`, exported.

- [ ] Step 1: tests (testdb, as `calls_test.go`): `TestDeviceTasksListsOnlyTheOwnersOpenTasks` (rows: open/null assignee, open/owner, open/other person, answered → 2 returned, oldest first); `TestDeviceTasksFollowsThePlaces` (places `{L-7}`: place `L-7` and null listed, `L-8` not); `TestDeviceTasksOffAnswers403` (setting false → 403 reason `device_tasks_off`; then `tasks_enabled=false` → same); `TestDeviceTaskOfAnotherPersonIs404`; `TestARevokedDeviceGets401`; `TestNative` table (each row of spec §6.4, `ui` set → false, `file` → false, unknown widget → false).
- [ ] Step 2: run `cd tools/go/core && go test -race ./internal/devices/ ./internal/scan/`; FAIL.
- [ ] Step 3: implement; register both routes with `device(...)` in `Public()`.
- [ ] Step 4: run again; PASS. `go vet ./...`.
- [ ] Step 5: commit `feat(devices): the phone reads the tasks its owner may answer`.

### Task 3: Device answers through Python's answer route

**Files:**
- Create: `tools/go/core/internal/devices/assert.go`, `tools/go/core/internal/devices/assert_test.go`, `backend/scadbuddy/auth/device_assertion.py`, `backend/tests/auth/test_device_assertion.py`, `backend/tests/fixtures/device-assertion-vectors.json`
- Modify: `tools/go/core/internal/devices/areas.go` (route, `Upstream http.Handler` on `Store`), `tools/go/core/main.go` (pass `next` as the devices area's upstream, as `tagreads` gets it), `backend/scadbuddy/api/tasks.py` (`answer_task`)
- Test: `backend/tests/api/test_tasks*.py` (the answer route's file)

**Interfaces:**
- Produces:
  - Header `X-Scadbuddy-Device-Assertion: <device id>.<task id>.<base64url(owner)>.<unix expiry>.<hex HMAC-SHA256>`; key = HKDF-SHA256 of the KEK, info `scadbuddy:devices:task-answer:v1` (as `auth/run_tokens.py` derives its key); MAC over `"device-answer\n" + device + "\n" + task + "\n" + owner + "\n" + expiry`; expiry = now + 60 s.
  - Go `func Assertion(key []byte, device, task, owner string, expiry time.Time) string`; Python `verify_device_assertion(key: bytes, header: str, task_id: str, now: float) -> DeviceAnswerer | None` with `DeviceAnswerer(owner: str, device_id: str)`.
  - `POST /api/v1/devices/task-answer?id=` body `{"answer":{…}}`, optional `Idempotency-Key`: core checks `tasksAllowed` and that the task is one the device follows (Task 2's rule), then forwards to Upstream as `POST /api/v1/tasks/<id>/answer` with the same body and `Idempotency-Key`, the assertion header, and no `Authorization` or `Cookie`; Python's status and body pass through unchanged.
  - Python `answer_task`: a valid assertion for this `task_id` makes the caller a person whose `by` is `f"{owner} via device {device_id}"[:MAX_BY_CHARS]`; an invalid or absent one changes nothing.
- [ ] Step 1: vectors file (3 good, wrong MAC, wrong task, expired); Go test `TestAssertionMatchesTheVectors`, Python test `test_assertion_matches_the_vectors` reading the same file; Python `test_a_device_answer_is_the_owners_via_the_device` (answered_by == `"alice via device <id>"`), `test_a_bad_assertion_answers_as_anonymous`; Go `TestTaskAnswerForwardsWithAnAssertion` (a fake Upstream records path, headers, body; no Authorization/Cookie), `TestTaskAnswerOfAnUnfollowedTaskIs404`, `TestTaskAnswerPassesA409Through`.
- [ ] Step 2: run both suites; FAIL.
- [ ] Step 3: implement. Go key: add `func (k *Keys) Derive(info string) []byte` to `internal/seal` if it has no HKDF helper.
- [ ] Step 4: run Go and `cd backend && uv run pytest tests/auth/test_device_assertion.py tests/api/<answer test file> -q`; PASS.
- [ ] Step 5: add `tools/go/core/internal/devices` to `select-lanes.sh` `read_by_tests` only if a Go test reads the backend fixture (it does: add the fixture path to the backend and go lane patterns per root CLAUDE.md "A new cross-tree read").
- [ ] Step 6: commit `feat(devices): the phone answers a task as its owner, through the answer check`.

### Task 4: Task topics on the device socket, and "in front"

**Files:**
- Modify: `tools/go/core/internal/realtime/device.go` (`topicAllowed`, `topicShaped`), the realtime `Devices` interface (where `Live` and `CallVisible` are declared), `tools/go/core/internal/devices/heartbeat.go`, `tools/go/core/internal/devices/calls.go` (online rule)
- Test: `tools/go/core/internal/realtime/device_test.go`, `tools/go/core/internal/devices/heartbeat_test.go`, `calls_test.go`

**Interfaces:**
- Produces: `TasksVisible(ctx, deviceID string) bool` on the realtime `Devices` interface (Task 2's `tasksAllowed`); a device socket may follow `tasks` and `task:<id>` when it is true, read again on each subscribe. Heartbeat body `{"foreground": bool}` (empty body = true, for the app before this change) sets `devices.front_at = now()` or null. A call goes only to a device whose `front_at` is within `OnlineWindow`.
- [ ] Step 1: tests `TestADeviceFollowsTaskTopicsWhenAllowed`, `TestADeviceIsRefusedTaskTopicsWhenOff`, `TestAPhoneBehindIsNotOnlineForCalls` (heartbeat foreground false → a call answers as for an offline device), `TestAnEmptyHeartbeatIsInFront`.
- [ ] Step 2: run; FAIL. Step 3: implement. Step 4: run `go test -race ./internal/realtime/ ./internal/devices/`; PASS.
- [ ] Step 5: commit `feat(devices): a device socket follows task topics; calls go to a phone in front`.

### Task 5: Photo, scan and image routes

**Files:**
- Create: `tools/go/core/internal/devices/taskmedia.go`, `taskmedia_test.go`
- Modify: `areas.go`

**Interfaces:**
- Consumes: Task 2 (`tasksAllowed`, follow rule), Task 3 (Upstream, assertion), `scan.Resolve`, the scan decoder core runs for `POST /scan/codes`, `tagreads` NDEF decode.
- Produces:
  - `POST /api/v1/devices/task-photo?id=` raw JPEG/PNG/WebP (sniffed as `reads.go` `sniff`), max `blobstore.Kinds["scan_image"]`: core stores the blob, then forwards to Python `POST /api/v1/entities/task:<id>/attachments` with the assertion header (Python's attachment route accepts the assertion as Task 3's answer route does, for `task:<id>` only); answers `{"attachment_id": "<uuid>"}`.
  - `POST /api/v1/devices/task-scan?id=&field=` JSON `{"tag": <tag_dump as DeviceProtocol.Dump sends>}` or raw image bytes: answers `{"code": "S-36"|null, "resolved": Resolved|null, "fits": bool}`; `fits` checks the field's `expects` `{kinds, codes}`.
  - `GET /api/v1/devices/task-image?id=&attachment=` the bytes of an attachment named by an `image` block of that task's context, else 404.
- [ ] Step 1: tests `TestTaskPhotoStoresABlobAndAnAttachment` (fake Upstream), `TestTaskPhotoRefusesAText`, `TestTaskScanOfASpoolLabel` (tag text `scadbuddy:spool:<uuid>` → `S-<n>`, fits), `TestTaskScanOutsideExpects` (fits false), `TestTaskImageOnlyForItsContext`.
- [ ] Step 2–4: run, implement, run; PASS.
- [ ] Step 5: commit `feat(devices): the phone's photo, scan and image routes for a task`.

### Task 6: Devices page and settings in the web app

**Files:**
- Create: `PATCH /api/v1/devices/{id}` in `tools/go/core/internal/devices/list.go` (person route, owner only, body `{"tasks_enabled"?: bool, "places"?: [string ≤ 20, each ≤ 100 chars, no control character]}`); its test in `list_test.go` or a new `patch_test.go`.
- Modify: the Devices page component under `frontend/src/components/devices/` (load frontend-design first), its mocks under `frontend/src/mocks/`, the settings area page that shows `flow_answerers` for `device_task_answers`.
- Test: the Devices page's vitest file.

**Interfaces:**
- Produces: `GET /api/v1/devices` rows gain `tasks_enabled`, `places`; the page shows a switch "Flow questions" and a places field "Only these places (empty: all)".
- [ ] Step 1: Go test `TestPatchDeviceTasks` (owner can, other person 404, bad place 422); vitest `turns flow questions off for one phone`.
- [ ] Step 2–4: run, implement, run (`cd frontend && pnpm test <file>`); PASS.
- [ ] Step 5: commit `feat(devices): switch flow questions per phone`.

### Task 7: Public exact paths on the SSO host

**Files:** eh-homelab/clusters, the Ingress that lists the device routes the SSO host passes without sign-in (find with `grep -rn "devices/heartbeat" ` in clusters).

- [ ] Step 1: add `/api/v1/devices/tasks`, `/api/v1/devices/task`, `/api/v1/devices/task-answer`, `/api/v1/devices/task-photo`, `/api/v1/devices/task-scan`, `/api/v1/devices/task-image` as `Exact` paths beside the heartbeat's.
- [ ] Step 2: `kubectl apply --dry-run=server` on the changed Ingress (`GOGC=off /usr/bin/kubectl`); it applies.
- [ ] Step 3: PR in clusters after PR 1 merges and deploys.

## Part B — app (PR 2: `feat(app): native task screens and notifications`)

### Task 8: Task model and the schema-to-control map

**Files:**
- Create: `app/ScadBuddy/TaskForm.cs` (pure, no MAUI type, compiled into `ScadBuddy.Tests` as `DeepLink.Links.cs` is), `app/ScadBuddy.Tests/TaskFormTests.cs`, `app/ScadBuddy.Tests/fixtures/tasks/*.json` (one task per widget and block kind, the shapes #8181's frontend widget tests use)
- Modify: `app/ScadBuddy.Tests/ScadBuddy.Tests.csproj` (compile the file, copy fixtures)

**Interfaces:**
- Produces: `record DeviceTask(string Id, string RunId, string Title, JsonObject Schema, JsonArray Context, string? Place, string? Ui, string State, bool Native, string FallbackUrl, JsonArray? ContextView)`; `enum ControlKind { Text, Email, Date, DateTime, Choice, Switch, Number, Lines, Confirm, Scan, Photo, Entity, Weight }`; `record FieldSpec(string Name, string Label, ControlKind Kind, bool Required, JsonNode? Default, string[]? Options, double? Min, double? Max, JsonObject? Expects, string? EntityKind)`; `static IReadOnlyList<FieldSpec>? TaskForm.Fields(JsonObject schema)` — null when any field is not native (the app trusts `native` from the server and uses null only as a second guard); `static JsonObject TaskForm.Answer(IReadOnlyList<FieldSpec>, IReadOnlyDictionary<string, JsonNode?> values)` — omits unset optional fields, `Confirm` → `true`, `Weight`/`Number` → number, `Lines` → array.
- [ ] Step 1: tests `EachFixtureMapsToItsControl` (theory over fixtures), `AnUnknownWidgetIsNotNative`, `AConfirmAnswersTrue`, `TheAnswerLeavesOutAnEmptyOptionalField`, `LabelsComeFromTitleElseTheName`.
- [ ] Step 2–4: `dotnet test app/ScadBuddy.Tests`; FAIL, implement, PASS.
- [ ] Step 5: commit `feat(app): map a task's schema to native controls`.

### Task 9: The live task list on the device socket

**Files:**
- Create: `app/ScadBuddy/Platforms/Android/TaskInbox.cs`
- Modify: `app/ScadBuddy/Platforms/Android/DeviceLink.cs` (subscribe `tasks` after `device:<id>`; route `task.changed` frames to `TaskInbox`; heartbeat body `{"foreground": front}`; keep the socket while a `TaskService` holds it, Task 10), `app/ScadBuddy/DeviceProtocol.cs` (parse `task.changed`), `app/ScadBuddy.Tests/DeviceProtocolTests.cs`

**Interfaces:**
- Consumes: Task 2 routes, Task 4 topics.
- Produces: `TaskInbox.Instance` with `IReadOnlyList<DeviceTask> Open`, `event Action? Changed`, `event Action<DeviceTask>? Arrived` (a task newly open, once per id per process), `event Action<string>? Left` (task id); `Task Reload()` (GET `/devices/tasks`); `Task<DeviceTask?> Get(string id)`; `Task<AnswerResult> Answer(string id, JsonObject answer, string idempotencyKey)` with `record AnswerResult(int Status, IReadOnlyList<(string Field, string Message)> Problems)`; `Task<string> UploadPhoto(string id, Stream jpeg)`; `Task<ScanResult> Scan(string id, string field, JsonObject? tag, Stream? image)`.
- `DeviceProtocol.TaskChanged(string frame) -> (string TaskId, string State)?` pure, tested.
- [ ] Step 1: tests `ATaskChangedFrameGivesItsIdAndState`, `AnotherKindIsNoTaskChange`, `TheHeartbeatSaysWhetherTheAppIsInFront` (body text).
- [ ] Step 2–4: run, implement, run; PASS. On connect and on `resync`: `Reload()`; a 403 `device_tasks_off` empties the list and stops notifying.
- [ ] Step 5: commit `feat(app): a live list of the open tasks over the device socket`.

### Task 10: Foreground service and notifications

**Files:**
- Create: `app/ScadBuddy/Platforms/Android/TaskService.cs`, `app/ScadBuddy/Platforms/Android/TaskNotifications.cs`
- Modify: `AndroidManifest.xml` (`FOREGROUND_SERVICE`, `FOREGROUND_SERVICE_DATA_SYNC`, `POST_NOTIFICATIONS`; the service with `foregroundServiceType="dataSync"`), `MainActivity.cs` (start the service when paired; a notification tap's extra `task_id` opens `TaskPage`), `MauiProgram.cs` (OnPause no longer stops the socket while the service runs; it still stops NFC calls' "in front")

**Interfaces:**
- Consumes: Task 9 `TaskInbox`.
- Produces: `TaskService.Start(Context)`, `TaskService.Stop(Context)` (on unpair); channel ids `questions` ("Questions for you", importance high) and `listening` ("ScadBuddy is listening for questions", importance min); a task notification: tag = task id, title = task title, text = first `context_view` card's `title` or none, visibility private with a public version "ScadBuddy: Question for you", auto-cancel, tap → `MainActivity` with `task_id`. `Left` cancels by tag.
- [ ] Step 1: on the owner's phone (adb, after build): `adb shell dumpsys activity services com.ehhomelab.scadbuddy` lists `TaskService` as foreground; `adb shell dumpsys notification --noredact | grep -A3 questions` shows a posted task. Write these checks into `app/CLAUDE.md` "Phone checks".
- [ ] Step 2: implement; ask `POST_NOTIFICATIONS` the first time `Arrived` fires on Android 13+.
- [ ] Step 3: build and install (`cd app && task phone` builds main; for the branch, `dotnet build` in the worktree and `adb -P 5039 install -r`), run the checks; they show the service and a notification for a test task.
- [ ] Step 4: commit `feat(app): notify flow questions from a foreground service`.

### Task 11: Task list and task screens

**Files:**
- Create: `app/ScadBuddy/TaskListPage.xaml(.cs)`, `app/ScadBuddy/TaskPage.xaml(.cs)`, `app/ScadBuddy/TaskControls.cs` (one view per `ControlKind`), `app/ScadBuddy/ContextViews.cs` (entity card, text, image, table, checklist, expect)
- Modify: `AppShell.xaml` or `MainPage.xaml` (toolbar item "Tasks (n)" bound to `TaskInbox.Open.Count`), `MainPage.xaml.cs` (open a task's `fallback_url` in the WebView)

**Interfaces:**
- Consumes: Tasks 8, 9 (and 10 for the tap).
- Produces: `TaskPage(DeviceTask task)`: title, context views, controls from `TaskForm.Fields`, "Send" button; `native == false` → `MainPage` loads `origin + fallback_url` and pops back to it. Scan control: "Hold a tag" uses the app's NFC reader (`DeviceMethods` read path) → `TaskInbox.Scan(id, field, tag, null)`; "Scan a code" takes a camera photo → `Scan(id, field, null, photo)`; typed code entry; shows the resolved card and refuses `fits == false` with "That is not one of the codes this question asks for." Photo control: camera → `UploadPhoto`. Entity control: as Scan, kind from `x-sb-widget` `kind`. Send: `Answer(...)`; 200/202 → pop, toast "Sent"; 409 → alert "Someone already answered this question." and pop; 422 → each problem under its field; network error → "Not sent. Check the connection and send again." with the same idempotency key on retry.
- [ ] Step 1: unit tests stay in Task 8 (pure); here, a phone check: run a flow (`mcp__scadbuddy__start_flow_run` on a flow that asks confirm, enum, scan, photo and entity, e.g. the `station-demo` flow) and answer each from the phone; `get_flow_run` shows each answer and `answered_by` "… via device …".
- [ ] Step 2: implement; follow the web app's colours and type scale where MAUI allows (load frontend-design).
- [ ] Step 3: build, install, run the phone check; all answers land.
- [ ] Step 4: commit `feat(app): native task list and task screens`.

### Task 12: Docs and release

**Files:** `app/CLAUDE.md` (task screens, service, checks), `docs/architecture/` page for devices (task routes), `docs/features/` if a devices page exists.

- [ ] Step 1: write the docs; `npx markdownlint-cli2` passes on them.
- [ ] Step 2: commit `docs(app): native task screens`; open PR 2 after PR 1 merges and the clusters paths are live; install the APK on the owner's phone.
