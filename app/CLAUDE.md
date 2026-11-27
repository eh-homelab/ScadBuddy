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

CI's `android` job (`ci.yml`, lane `android` in `select-lanes.sh`) runs the tests and the same
build on `ubuntu-latest` with JDK 17. It pins the .NET SDK (`10.0.301`, in `app/global.json` too: the runner image also has a .NET 11
SDK, which `dotnet` would otherwise pick, and whose manifests conflict with this workload set) and the workload set
(`dotnet workload install maui-android --version 10.0.303.1`), the pair measured working here on
2026-11-27 (`dotnet --version`, `dotnet workload --version`); move them together. The tests (`app/ScadBuddy.Tests`, xunit on `net10.0`, no
Android) compile the app's pure files by link, today `DeepLink.Links.cs` and `DeviceProtocol.cs`:

```bash
cd app/ScadBuddy.Tests && dotnet test
```

## The published APK

`build-app.yml` (#8376) runs on every push to main that changes `app/` (not only a
`CLAUDE.md`), and on a dispatch. It runs the tests and the `android` job's build with the same
pins, with version code `github.run_number` and version name `1.0.<run number>`. Then it
pushes `app/Dockerfile`, a `FROM scratch` image with only `/scadbuddy.apk` and
`/scadbuddy.apk.sha256`, to `ghcr.io/eh-homelab/scadbuddy-app`, tagged `sha-<7>` and
`latest`. It publishes no GitHub release. To install the latest:

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
  `SecureStorage`. While the app is in front, it holds the realtime socket (`/api/v1/ws`,
  `Authorization: Bearer sbdev_…`, subscribed to `device:<id>`), and posts
  `/api/v1/devices/heartbeat` every 30 s. It runs one `device.call_requested` at a time, and
  posts its events, uploads and result. A 401, or close code 4401, ends the pairing.
- `Platforms/Android/DeviceMethods.cs`: the four methods (`nfc.read`, `nfc.write`,
  `code.scan`, `camera.capture`), and the full-screen "Tap the tag" view.
- `Platforms/Android/NfcBridge.cs`: reader mode, MIFARE reads (keys from the
  [RFID Tag Guide](https://github.com/Bambu-Research-Group/RFID-Tag-Guide)'s HKDF), NDEF
  reads and writes, and the bridge to `Resources/Raw/nfc-polyfill.js` (Web NFC's `NDEFReader`).
  A running device call takes every tap (`StartCall`), and a read tells it its progress
  (`ITagProgress`).
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
- The socket and the heartbeat run only while the app is in front: they stop in `OnPause`
  and start in `OnResume`. A running call goes on while a photo pauses the app.
- Bytes go up as one blob per read (`tag_dump` or `scan_image`, `POST /api/v1/devices/uploads`),
  never in an event or a result. A tag dump never holds a sector trailer (its keys).
- Take the routes, the JSON shapes and the event kinds from core's code and tests, not from
  the plan. Change `DeviceProtocol.cs` and its tests with them.
- Telemetry (spans, metrics, OTLP logs) never carries a UID or block data. A call's span is
  `device.call`, with `device.method`, `device.outcome` and the error's code only
  (`DeviceProtocol.CodeOf`). The local logcat line (tag `ScadBuddyNfc`) names the UID and the
  block counts, never block data.
