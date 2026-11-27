# Devices: the phone app, and later other hardware, as paired devices (#7905)

## Intent

On 2026-11-27 the owner described the phone app as a device that is attached to ScadBuddy:

- "what if we paired the app to a session -- this gives it a token that lives as long as the session needs to."
- "The app is like attaching a device. we call methods on it like we would from the assistant, except without the assistant."
- "We expose these as devices in the system. the RFID is a sensor and a RFID writer. GPS comes from app / devices on android so now we have a ton of hardware we can suddenly exopose."

How long a pairing lasts: "paired until the token expires", with "default 1 hour unless not idle". Off the LAN: "yes". How a call behaves: "i want them to behave like devices": a call reports what the device is doing as it goes ("found tag id XXX 38 blocks... got 8", "failed", "found it again, starting at 8, got 16"). The raw result is stored as a blob with its info. The caller gets the read's id, queries it directly, and feeds it to the filament decoder. The scan link stays: "we still want the scan link", "that is how the initial pair will happen".

The scope for now (owner, 2026-11-27): "ok, scope creep. for today just stick with what we have for nfc.read and write code scan and capture", "simple.", "nothing more than that".

So the phone stops being a page that has to be in the right state. It becomes hardware that ScadBuddy can use: an NFC reader and writer, a code scanner and a camera. A web page, a flow or the assistant can ask it for a reading.

## Goals

- G1. Pair a device to a person by a link (`scadbuddy://pair?url=…`), shown as a QR code or a tap link.
- G2. The device's token expires after 1 hour without use. Each call and each heartbeat moves the expiry forward. A hard cap ends every pairing.
- G3. The device announces its capabilities, and each capability has a method schema and a risk tier.
- G4. Pages, flows and the assistant call a device's methods through one core route, under the access model every other tool uses (#2171).
- G5. Devices are listed in ScadBuddy with their state: online, last seen, capabilities, expiry. The person can revoke a pairing.
- G6. Bytes from a device (tag dumps, photos) are stored as blobs with their info. The device layer knows nothing of filament: a consumer (the filament tag decoder, #8155's code decoder) takes the stored read's id as input.
- G7. A call streams events while it runs, as hardware reports progress, and ends with a result.
- G8. Devices work on the LAN and away from home, through the SSO host.

## Non-goals

- Waking a device that is not open. Android needs the app in front for NFC. A call to an offline device fails at once, and the caller can show the app's open link.
- iOS. The model allows it, as a second app.
- A device platform. ScadBuddy is not one. The only capabilities are `nfc.read`, `nfc.write`, `code.scan` and `camera.capture`. There is no `location.get`, no other phone sensor, no Home Assistant link (no reads of HA sensors, no publishing to HA, no HA wake notifications), and no registry of other hardware (scales and printers stay where they are).
- Moving the existing `nfc-jobs` routes now. They stay until a later change makes them device calls.

## Anti-goals

- A device token that outlives its use. A stolen token must die within an hour of idle time.
- A second permission system. Who may call which method is a grant with a safe default (#2171, "permissions always configurable").
- A device that talks to a server the person did not choose. The app confirms the origin before it pairs.
- Image bytes as base64 in JSON (#2479).

## Constraints

- New backend work is Go in `tools/go/core` (#7667). Only core and the API hold SQL.
- Migrations: one new file per change in `backend/scadbuddy/migrations/`.
- Fleet and the agent's browser bridge are prior art, not dependencies. The device hub must work while the assistant is unavailable.
- A Bambu tag is never written. `nfc.write` refuses a Bambu MIFARE tag on the device and on the server.

## Approaches

1. **A device hub in Go core** (picked; the owner answered the next question on that basis, 2026-11-27). Core gets a `devices` area with pairing, tokens, calls and reads. The transport is what ScadBuddy has: the bus and the realtime socket for calls and events, the blob store for bytes, and pending actions for approvals. One process owns pairing, tokens and calls. It is new code, but it sits where new backend work goes.
2. **Extend the agent's browser bridge.** It reuses the socket, the pairing and the tool catalogue. Rejected: it lives in the TypeScript agent sidecar, and it works only while the assistant is available.
3. **MQTT.** The standard for hardware. Rejected for now: it needs a new broker in the path, broker auth per device, and a second transport beside HTTP.

## Design

### Pairing (G1)

1. A signed-in person opens "Add device" (the Devices page, or the Scan page's prompt). Core creates a pairing request: a random code of at least 128 bits, of which only the hash is stored. The request is single-use and expires in 10 minutes. The page shows `scadbuddy://pair?url=<https://<origin>/api/v1/devices/pair/<code>>` as a QR code and as a tap link.
2. The app opens the link and shows the origin in punycode, and the app's own set address if it differs. It asks "Pair this phone with <origin>?".
3. On yes, the app POSTs to the URL with its name, its model, the app version and its capabilities. Core spends the code and answers with the device id and a device token. The app stores the token in Android's encrypted storage (`SecureStorage`).
4. The device belongs to the person who made the request. Its calls are made by that person (the actor), and the device's id is recorded beside the actor.

`DeepLink.cs` takes `scadbuddy://pair?url=…` in place of the per-scan `scadbuddy://scan?…` link of #8157. Per-scan result URLs are dropped. No server ever made a scan link, because #8157 was never built. So the pairing PR replaces the scan link at once, and no flow breaks.

### Tokens (G2)

- Idle expiry: 1 hour (the owner's default). Each authenticated device request moves `expires_at` to now + 1 hour.
- Hard cap: 30 days from pairing. This is the assistant's call; the owner may change it. When it is reached, the person pairs again.
- Both values are settings with these defaults.
- A token is at least 256 random bits. Only its hash is stored. It is sent as a bearer header on every device route and on the realtime socket.
- Revoke: the Devices page ends a pairing at once. The bus event `device.revoked` closes its realtime socket, and its token is refused from then on.

### The link between a device and core

The device uses what ScadBuddy already has. It gets no socket of its own.

- **Calls in:** while the app is in front, it holds ScadBuddy's realtime socket (`internal/realtime`, #7768) with its device token, subscribed to its own topic `device:<device id>`. A call is the bus event `device.call_requested` on that topic. Every pod's realtime socket gets every bus event, so a call reaches the device whichever pod took the request, with nothing added. The realtime socket accepts a device token for that one topic only, and closes on `device.revoked`, as it closes on `auth.session_revoked` today.
- **Events and the result out:** plain HTTP with the device token: `POST /api/v1/devices/calls/{id}/events` (one or a few events) and `POST /api/v1/devices/calls/{id}/result`. Core publishes each event on the bus (below) and ends the call.
- **Online:** `POST /api/v1/devices/heartbeat` every 30 s moves `last_seen_at` and the token's expiry. A device is online while `last_seen_at` is under 75 s old. When the app goes to the back, it stops the heartbeat and closes its realtime socket.
- Bytes never go in an event or a result. The device uploads them with its token, and puts the blob's key in its result.

### Capabilities (G3)

| Method | Tier | Arguments | Result |
|---|---|---|---|
| `nfc.read` | read | `timeout_s` | events, then `{read_id, uid, complete}` |
| `nfc.write` | outward | NDEF records | the read-back. It never writes a Bambu tag; backup and read-back as in #8069. |
| `code.scan` | read | none | `{read_id}`; #8155's decoder takes it |
| `camera.capture` | read | none | `{read_id}` |

The RFID reader is listed as a sensor with a writer, as the owner put it.

### Events and results (G6, G7)

A device's events are events on ScadBuddy's own event stream, the bus (`internal/bus` `Publish`, core/pg_events.py). That stream already has an ordered log, retention, replay and a NOTIFY, and pages follow it by topic over the realtime socket (#7768). No second event table and no second channel are made. Each event the device sends is published with kind `device.<kind>` and the topics `device:<device id>` and `device:call:<call id>`. A page or the assistant follows `device:call:<id>` and sees the call move. For example, `nfc.read` on a Bambu tag:

| # | kind | fields |
|---|---|---|
| 1 | `device.tag_found` | `{call, uid: "9CC69CE3", tech: "mifare_classic", blocks: 48}` |
| 2 | `device.tag_progress` | `{call, read: 8, of: 48}` |
| 3 | `device.tag_lost` | `{call, read: 8, error: "sector 2: transceive failed"}` |
| 4 | `device.tag_found` | `{call, uid: "9CC69CE3", resume_from: 8}` |
| 5 | `device.tag_progress` | `{call, read: 16, of: 48}` |
| … | | |
| n | `device.call_done` | `{call, read_id}` |

The calls themselves go the same way. A call is published as `device.call_requested` with the topic `device:<device id>`. The device's realtime socket, on whichever pod, delivers it.

The result is generic and knows nothing of filament:

1. The device uploads the raw read as one blob, kind `tag_dump`. It holds the UID, the tag's tech, every data block it read (never a key trailer), and any NDEF records.
2. Core writes a `device_reads` row: id, device, call, at, UID, tech, blob key, `complete`, missing blocks.
3. The call ends with `{read_id, uid, complete}`.
4. A consumer takes the `read_id`. `GET /api/v1/devices/reads/{id}` gives the info and the blob. The filament decoder accepts `{read_id}` in place of inline blocks (the tag decode route). It decodes the read, finds or makes the tag, and writes the `filament_tag_reads` row that points at the device read.

`code.scan` and `camera.capture` work in the same way. The photo is a `scan_image` blob. The result is `{read_id}`, and #8155's decoder takes that id.

### Calls (G4)

- `POST /api/v1/devices/{id}/calls {method, args, timeout_s}` answers `202 {call_id}` at once, or `409 offline` when the device is not online. `GET /api/v1/devices/calls/{id}` gives the call and its result; its events are replayed from the event log by the topic `device:call:<id>`. The realtime socket carries each new event. A caller that only wants the end can add `wait=true`, which holds the request until the result or the deadline (`504`). Core keeps a row per call: who called, the method, the outcome and the times.
- The caller is a person (a page), a flow, or the assistant. An assistant tool `device_call` uses the same route.
- An outward method (`nfc.write`) called by the assistant or a flow needs an approval, as outward tools do: a pending action confirmed in the UI. A person who calls it from a page with their own click has approved it by that click.
- Grants are settings. By default, the device's owner and the owner's flows and assistant sessions may call every read method.

### Auth mode

Prod runs in auth mode `observe`, and it never runs `enforce` (owner, 2026-11-17).
- **Device-token routes:** the pairing claim, the heartbeat, a call's events and result, the device's uploads, and the realtime socket's device-token path. These routes check the device token in every mode, as service-credential routes do (#5297 part 1). They refuse a missing, bad, revoked or expired token. Without this check, a stolen token outlives its idle hour.
- **Person routes:** creating a pairing, listing devices, revoking a device, and calling a device. These follow the global mode, as every other person route does. With auth off (on the LAN host), the person is the anonymous principal, as for `nfc-jobs`.

### The Devices view (G5)

A page lists each device with:
- its name, model and app version;
- online or offline, and when it was last seen;
- its capabilities;
- its idle expiry and its hard cap;
- a Revoke button.

The Scan page and other pages that can use a device offer "Use my phone" when an owned device is online. When none is online, they show the pairing QR code.

### Data

Core owns three new tables:
- `devices`: id, owner, name, model, app version, capabilities, created, last seen, revoked.
- `device_tokens`: device, token hash, created, expires, hard cap, revoked.
- `device_calls`: id, device, caller, method, arguments (bounded), outcome, result pointer, created, ended.
- `device_reads`: id, device, call, at, kind (`nfc`, `photo`), UID, tech, blob key, media type, complete, missing blocks.

One migration creates them.

### Away from home (G8)

- The SSO host (Pomerium) sends anything without a Pomerium login to sign-in. So the device-token routes are narrow public routes on that host: the pairing claim, the heartbeat, the call events and result, the device upload, and the realtime socket, each an Exact or narrow prefix route. The device-token check protects them. The owner accepted this pattern for `/mcp` and the plugin feeds on 2026-11-25. This needs a clusters PR, with a server-side dry run of the changed Ingress before merge.
- The person routes stay behind SSO.
- The pairing link names the host the person is on (the page's `window.location`). A phone paired on the SSO host works away from home.

## Calls the assistant made (the owner may change them)

- The 30-day hard cap.
- 10 minutes for a pairing code.
- A 30 s heartbeat.
- At most 2 minutes for a call's deadline.
- A device belongs to the person who paired it, and its calls act as that person.
- Device-token routes check the token in every auth mode, and person routes follow the global mode.
- A person's own click approves an outward call made from a page. The assistant and flows need a pending action.
- The device reads table is `device_reads`. The event kinds on the bus are `device.tag_found`, `device.tag_progress`, `device.tag_lost`, `device.call_requested`, `device.call_done` and `device.call_failed`.
- No device socket of its own: the realtime socket carries calls in, and HTTP carries events and results out.
- Device uploads: a device route of its own, with kind `scan_image` for photos.

## Left to the builder

- The exact event fields, and how a page waits for a call (the realtime socket, or `wait=true`).
- How the app shows an incoming call: a toast, or a full-screen "Tap the tag" view.
- The PR order. A sensible one: pairing, tokens and the Devices list first; then the calls, their events and the realtime socket's device path; then `nfc.read` and `code.scan`; then `nfc.write`.
