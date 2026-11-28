# The phone app (app/ScadBuddy)

A .NET MAUI Android app (#7905). Its screen is ScadBuddy's `/scan` page in a WebView. The
app adds what the browser lacks: native NFC, including Bambu MIFARE Classic reads. It also
pairs with ScadBuddy as a device (#8244, spec `docs/superpowers/specs/2026-11-27-devices-design.md`),
so pages, flows and the assistant can call its NFC and camera.

## Commands

```bash
cd app/ScadBuddy
dotnet workload install maui-android   # once
dotnet build -f net10.0-android -c Release -p:AndroidSdkDirectory=$ANDROID_HOME -p:JavaSdkDirectory=$JAVA_HOME
adb install -r bin/Release/net10.0-android/com.ehhomelab.scadbuddy-Signed.apk
```

`app/Taskfile.yml` ([go-task](https://taskfile.dev) v3) does both in one step: `cd app && task
phone` builds the latest origin/main in a worktree of its own (`~/.cache/scadbuddy-app-build`) and
installs it over adb on port 5039. `REINSTALL=1` uninstalls first (needed over a CI build's key).

CI's `android` job (`ci.yml`, lane `android` in `select-lanes.sh`) runs the tests and the same
build on `ubuntu-latest` with JDK 17. It pins the .NET SDK (`10.0.301`, in `app/global.json` too: the runner image also has a .NET 11
SDK, which `dotnet` would otherwise pick, and whose manifests conflict with this workload set) and the workload set
(`dotnet workload install maui-android --version 10.0.303.1`), the pair measured working here on
2026-11-27 (`dotnet --version`, `dotnet workload --version`); move them together. The tests (`app/ScadBuddy.Tests`, xunit on `net10.0`, no
Android) compile the app's pure files by link, today `DeepLink.Links.cs`, `DeviceProtocol.cs` and
`TaskForm.cs` (with the task fixtures in `ScadBuddy.Tests/fixtures/tasks/`):

```bash
cd app/ScadBuddy.Tests && dotnet test
```

## Wireless adb

From WSL, run adb's server on port 5039: `~/android-sdk/platform-tools/adb -P 5039 nodaemon server`
(5037 hangs: under mirrored networking, Windows holds it). A phone with Advanced Protection takes
an APK only over wireless debugging: pair once with `adb -P 5039 pair <ip:port> <code>` (the code
from "Pair device with pairing code"), then the phone shows in `adb -P 5039 devices` through mDNS.
The app logs each read under the logcat tag `ScadBuddyNfc`.

## Phone checks

With the app installed and paired, and the app behind:

```bash
adb -P 5039 shell dumpsys activity services com.ehhomelab.scadbuddy   # TaskService, isForeground=true
adb -P 5039 shell dumpsys notification --noredact | grep -A3 questions  # a posted task
```

Then open a flow task the phone's owner may answer (a confirm, an enum, a scan by tag and by
code, a photo, an entity), answer each from the phone, and read the run: each answer is there
with `answered_by` "… via device …".

## The published APK

`build-app.yml` (#8376) runs on every push to main that changes `app/` (not only a
`CLAUDE.md`), and on a dispatch. It runs the tests and the `android` job's build with the same
pins, with version code `github.run_number` and version name `1.0.<run number>`. Then it
pushes `app/Dockerfile`, a `FROM scratch` image with only `/scadbuddy.apk` and
`/scadbuddy.apk.sha256`, to `ghcr.io/eh-homelab/scadbuddy-app`, tagged `sha-<7>`, and
`latest` only for a main build: a dispatch of another branch publishes `sha-<7>` alone.
It publishes no GitHub release. To install the latest:

```bash
crane export ghcr.io/eh-homelab/scadbuddy-app:latest - | tar -x scadbuddy.apk scadbuddy.apk.sha256
# or: id=$(docker create ghcr.io/eh-homelab/scadbuddy-app:latest x) && docker cp "$id:/scadbuddy.apk" . && docker rm "$id"
sha256sum -c scadbuddy.apk.sha256
adb -P 5039 install -r scadbuddy.apk
```

CI builds are signed with the runner's debug key, and each runner makes its own. So
`install -r` over an earlier build (CI's or a local one) fails on the signature: uninstall
first (`adb -P 5039 uninstall com.ehhomelab.scadbuddy`), which drops the stored pairing.
That stays until a release key signs every build (the #8376 follow-up).

## Layout

- `MainPage.xaml.cs`: loads the set ScadBuddy address, and runs pairing links (from the
  browser, or a tap link on its own page): it asks "Pair this phone?", then
  `DeviceLink.Pair` claims the code.
- `DeepLink.cs`: `scadbuddy://pair?url=<claim address>` (#8244; it replaced #8157's
  `scadbuddy://scan`). Its pure part (which links are accepted, `OriginOf`, the pairing
  question) is `DeepLink.Links.cs`, which uses no MAUI type.
- `DeviceProtocol.cs`: the pure part of the device link: the claim body, the stored pairing,
  the realtime socket's frames, a call's events (`tag_found`, `tag_progress`, `tag_lost`) and
  result, the tag dump, and the `nfc.write` record check. It uses no MAUI or Android type.
  Core's side is `tools/go/core/internal/devices` and `internal/realtime` (`device.go`).
- `Platforms/Android/DeviceLink.cs`: the phone as a paired device. The token is in
  `SecureStorage`. While the app is in front, or while `TaskService` holds it, it holds the
  realtime socket (`/api/v1/ws`, `Authorization: Bearer sbdev_…`, subscribed to `device:<id>`,
  then to `tasks` on a frame of its own), and posts `/api/v1/devices/heartbeat` every 30 s with
  `{"foreground": bool}` (a change of front sends one at once). It runs one
  `device.call_requested` at a time, and posts its events, uploads and result. It hands
  `task.changed` and `resync` to `TaskInbox`, and sends the `tasks` subscribe again and reads
  the list every 5 minutes. A 401, or close code 4401, ends the pairing.
- Flow questions on the phone (#8574, spec
  `docs/superpowers/specs/2026-11-28-native-task-screens-design.md`, plan
  `docs/superpowers/plans/2026-11-28-native-task-screens.md` Part B):
  - `TaskForm.cs` (pure, tested): a task as core's device task routes send it (`TaskQuestion`'s
    JSON plus `native`, `fallback_url`, `context_view`), the control each field takes (spec
    §6.4; `Fields` is null for a field the app does not draw, the second guard after
    `native`), the answer and the local check, the context blocks, and the replies of the
    answer, photo and scan routes.
  - `Platforms/Android/TaskInbox.cs`: the live list over `GET /api/v1/devices/tasks` and
    `GET /api/v1/devices/task?id=`, and `task-answer`, `task-photo`, `task-scan` and
    `task-image`. A task notifies once per id in a process (`Arrived`). A 404 from the list
    (a core without the routes) is no tasks; a 403 `device_tasks_off` empties the list.
  - `Platforms/Android/TaskService.cs`: the `dataSync` foreground service that keeps the socket
    with the app behind, from pairing (or the next resume) until the app is swiped away or the
    pairing ends. Android 15 stops it after 6 hours a day (`OnTimeout`); the next resume starts
    it again.
  - `Platforms/Android/TaskNotifications.cs`: channel `questions` ("Questions for you", high),
    one notification per task tagged with its id, private on the lock screen with the public
    text "ScadBuddy: Question for you", cancelled when the task leaves the list; channel
    `listening` (min) for the service's own. Android 13+ asks `POST_NOTIFICATIONS` when the
    first task arrives, from the activity. A tap carries the extra `task_id` to `MainActivity`
    (`DeepLink.RaiseTask`).
  - `TaskListPage.cs`, `TaskPage.cs`, `TaskControls.cs` (one view per `ControlKind`),
    `ContextViews.cs`, `TaskTheme.cs` (the web's tokens) and `Platforms/Android/TaskReads.cs`
    (a tag read for a scan field, read only, and a photo). The toolbar's "Tasks (n)" opens the
    list. A task with `native` false, a `ui`, or a field `Fields` refuses opens
    `fallback_url` on the web view's own origin (`TaskForm.FallbackOf`). An answer keeps its
    `Idempotency-Key` across retries and takes a new one after a 422.
- `Platforms/Android/DeviceMethods.cs`: the four methods (`nfc.read`, `nfc.write`,
  `code.scan`, `camera.capture`), and the full-screen "Tap the tag" view.
- `Platforms/Android/NfcBridge.cs`: reader mode, MIFARE reads (keys from the
  [RFID Tag Guide](https://github.com/Bambu-Research-Group/RFID-Tag-Guide)'s HKDF), NDEF
  reads and writes, and the bridge to `Resources/Raw/nfc-polyfill.js` (Web NFC's `NDEFReader`).
  A running device call takes every tap (`StartCall`), and a read tells it its progress
  (`ITagProgress`).
  A page write reads the tag back on the write's own connection (Android dispatches a tag only
  when it finds it, and a new connection just after the write failed on prod, #8362); with no
  read-back the page asks for a second tap. A failed NDEF read is a `readingerror`, never a
  reading with no records. Each NDEF reading carries `tag`: the NTAG chip from GET_VERSION, its
  user memory, `Ndef.getMaxSize()` and `isWritable()`.
- `Platforms/Android/ScanChromeClient.cs`: the camera and the location, for the set origin only.
- `Telemetry.cs`: OTLP/HTTP traces, metrics and logs (`scadbuddy-app`).

## Rules

- A Bambu tag is never written. `nfc.write` refuses every MIFARE Classic tag
  (`bambu_tag`), and core never sends a raw block, sector or key.
- The app announces only `nfc.read`, `nfc.write`, `code.scan` and `camera.capture`. ScadBuddy
  is not a device platform: add no other method.
- Only the set origin's page gets the NFC bridge, the camera and the location. A pairing
  link always asks first. The question names the link's origin in punycode, and the set
  address too when it differs.
- Without `TaskService`, the socket and the heartbeat run only while the app is in front: they
  stop in `OnPause` and start in `OnResume`. With it they go on behind, and the heartbeat says
  `foreground: false`, so core sends no NFC call to a phone behind. A running call goes on
  while a photo pauses the app.
- A notification shows a task's title and its first entity card's title only, never other task
  content, and is private on the lock screen. Nothing logs a task's schema, context or answer.
- Bytes go up as one blob per read (`tag_dump` or `scan_image`, `POST /api/v1/devices/uploads`),
  never in an event or a result. A tag dump never holds a sector trailer (its keys).
- Take the routes, the JSON shapes and the event kinds from core's code and tests, not from
  the plan. Change `DeviceProtocol.cs` and its tests with them.
- Telemetry (spans, metrics, OTLP logs) never carries a UID or block data. A call's span is
  `device.call`, with `device.method`, `device.outcome` and the error's code only
  (`DeviceProtocol.CodeOf`). The local logcat line (tag `ScadBuddyNfc`) names the UID and the
  block counts, never block data.
