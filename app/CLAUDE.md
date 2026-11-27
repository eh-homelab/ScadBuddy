# The phone app (app/ScadBuddy)

A .NET MAUI Android app (#7905). Its screen is ScadBuddy's `/scan` page in a WebView. The
app adds what the browser lacks: native NFC, including Bambu MIFARE Classic reads.

## Commands

```bash
cd app/ScadBuddy
dotnet workload install maui-android   # once
dotnet build -f net10.0-android -c Release -p:AndroidSdkDirectory=$ANDROID_HOME -p:JavaSdkDirectory=$JAVA_HOME
adb install -r bin/Release/net10.0-android/com.ehhomelab.scadbuddy-Signed.apk
```

CI's `android` job (`ci.yml`, lane `android` in `select-lanes.sh`) runs the tests and the same
build on `ubuntu-latest` with JDK 17. It pins the .NET SDK (`10.0.301`) and the workload set
(`dotnet workload install maui-android --version 10.0.303.1`), the pair measured working here on
2026-11-27 (`dotnet --version`, `dotnet workload --version`); move them together. The tests (`app/ScadBuddy.Tests`, xunit on `net10.0`, no
Android) compile the app's pure files by link, today `DeepLink.Links.cs`:

```bash
cd app/ScadBuddy.Tests && dotnet test
```

## Layout

- `MainPage.xaml.cs`: loads the set ScadBuddy address and runs scan links.
- `DeepLink.cs`: `scadbuddy://scan?kind=nfc|code&url=<one-time result URL>` (#8157); its pure
  part (which links are accepted, `OriginOf`) is `DeepLink.Links.cs`, which uses no MAUI type.
- `Platforms/Android/NfcBridge.cs`: reader mode, MIFARE reads (keys from the
  [RFID Tag Guide](https://github.com/Bambu-Research-Group/RFID-Tag-Guide)'s HKDF), NDEF
  reads and writes, and the bridge to `Resources/Raw/nfc-polyfill.js` (Web NFC's `NDEFReader`).
- `Platforms/Android/ScanChromeClient.cs`: the camera and the location, for the set origin only.
- `Telemetry.cs`: OTLP/HTTP traces, metrics and logs (`scadbuddy-app`).

## Rules

- A Bambu tag is never written.
- Only the set origin's page gets the NFC bridge, the camera and the location. A scan
  link's read or photo goes only to the set origin, or to one the person accepted once.
- Telemetry (spans, metrics, OTLP logs) never carries a UID or block data. The local
  logcat line (tag `ScadBuddyNfc`) names the UID and the block counts, never block data.
