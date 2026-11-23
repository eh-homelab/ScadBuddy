import { HttpResponse, http } from 'msw'
import type { components } from '../../../api/schema'
import { mockSettings } from '../../handlers'
import { bindWrittenNtag, hasBambuddyLink, holdsOverrideTag } from './index'

const WRITTEN_UID = '04A1B2C3D4E5F6'

type S = components['schemas']

/**
 * #6264 — `/api/v1/filament/rfid/*`, `/api/v1/filament/spools/:id/ntag/*` and (#7155)
 * `/api/v1/filament/spoolbuddy/devices/:device/tare` (`backend/scadbuddy/api/filament.py`): the
 * RFID override mode, the reconcile, the SpoolBuddy scan, the NTAG write, confirm and cancel, and
 * the scale's tare. This module owns those routes (one route, one
 * feature module); the spool, tag and binding routes stay in `index.ts`. Its answers are the
 * happy path: a reader that sees a tag, a write that confirms on its second poll. A test
 * serves the other answers with `server.use`.
 */

const BASE = '/api/v1/filament'

const SEED_DEVICES: S['SpoolBuddyDevice'][] = [
  {
    id: 1,
    device_id: 'sb-0001',
    hostname: 'spoolbuddy-bench',
    has_nfc: true,
    nfc_ok: true,
    has_scale: true,
    scale_ok: true,
    online: true,
    last_calibrated_at: '2026-11-20T09:30:00Z',
  },
  { id: 2, device_id: 'sb-0002', hostname: 'spoolbuddy-shelf', has_nfc: false, nfc_ok: false, has_scale: false, scale_ok: false, online: true },
]

let devices = structuredClone(SEED_DEVICES)
let overrideMode = false
let confirms = new Map<string, number>()

export function reset() {
  devices = structuredClone(SEED_DEVICES)
  overrideMode = false
  confirms = new Map()
}

/** #7155 — gives a device a pending command (`write_tag`, …), as Bambuddy's one slot holds it; `null` clears it. */
export function setPendingCommand(deviceId: string, command: string | null) {
  const device = devices.find((row) => row.device_id === deviceId)
  if (device) device.pending_command = command
}

/** #7156 — the mode and the setting `filament_auto_add_unknown_rfid` are two holds on one value. */
const status = (devices: S['SpoolBuddyDevice'][]): S['RfidStatus'] => {
  const autoAdd = mockSettings().filament_auto_add_unknown_rfid !== false
  const holds = overrideMode || !autoAdd
  return {
    override_mode: overrideMode,
    auto_add_unknown_rfid: autoAdd,
    state: holds ? 'applied' : 'off',
    bambuddy_auto_add: holds ? false : null,
    saved_auto_add: holds ? true : null,
    detail: null,
    devices,
  }
}

const conflict = (detail: string, code: string) =>
  HttpResponse.json(
    { type: 'about:blank', title: 'Conflict', status: 409, detail, code },
    { status: 409, headers: { 'Content-Type': 'application/problem+json' } },
  )

export const handlers = [
  http.get(`${BASE}/rfid/status`, () => HttpResponse.json(status(devices))),
  http.put(`${BASE}/rfid/override-mode`, async ({ request }) => {
    overrideMode = ((await request.json()) as S['RfidModeSet']).enabled
    return HttpResponse.json(status([]))
  }),
  http.post(`${BASE}/rfid/reconcile`, () =>
    HttpResponse.json({
      mode: status([]),
      tags: { pushed: 0, moved: 0, cleared: 0, unchanged: 0, failed: 0 },
      slots: { configured: 0, unchanged: 0, no_preset: 0, no_temps: 0, no_nozzle: 0, failed: 0 },
    } satisfies S['RfidReconcileReport']),
  ),
  http.post(`${BASE}/rfid/scan`, () =>
    HttpResponse.json({ status: 'tag', tag_uid: '04A1B2C3D4E5F6', tray_uuid: null, tag_type: 'NTAG215', kind: 'ntag_opentag' } satisfies S['ScanResult']),
  ),
  http.post(`${BASE}/spools/:id/ntag/write`, async ({ params, request }) => {
    const id = String(params.id)
    const body = (await request.json()) as S['NtagWrite']
    if (!hasBambuddyLink(id)) return conflict('this spool is not linked to a Bambuddy spool', 'no_bambuddy_spool')
    if (holdsOverrideTag(id) && !body.replace) return conflict("the spool's open tag was bound with override", 'override_tag_open')
    confirms.delete(id)
    return HttpResponse.json({ status: 'queued', warnings: [] } satisfies S['NtagQueued'])
  }),
  http.post(`${BASE}/spools/:id/ntag/confirm`, ({ params }) => {
    const id = String(params.id)
    const polls = (confirms.get(id) ?? 0) + 1
    confirms.set(id, polls)
    if (polls < 2) return HttpResponse.json({ bound: false })
    if (polls === 2) bindWrittenNtag(id, WRITTEN_UID)
    return HttpResponse.json({ bound: true, tag_uid: WRITTEN_UID })
  }),
  http.post(`${BASE}/spools/:id/ntag/cancel`, () => new HttpResponse(null, { status: 204 })),
  // #7155 — the backend's checks (`filament/scale.py`): a listed device with a scale and no other command pending.
  http.post(`${BASE}/spoolbuddy/devices/:device/tare`, ({ params }) => {
    const id = String(params.device)
    const device = devices.find((row) => row.device_id === id)
    if (!device)
      return HttpResponse.json(
        { type: 'about:blank', title: 'Not Found', status: 404, detail: `Bambuddy lists no SpoolBuddy device ${id}`, code: 'no_device' },
        { status: 404, headers: { 'Content-Type': 'application/problem+json' } },
      )
    if (!device.has_scale) return conflict(`the SpoolBuddy device ${id} has no scale`, 'no_scale')
    const pending = device.pending_command
    if (pending && pending !== 'tare')
      return conflict(`the SpoolBuddy device ${id} has the command ${pending} pending; a tare would replace it, so wait until it is done`, 'device_busy')
    // Bambuddy's one slot now holds the tare, until the device's next heartbeat clears it.
    device.pending_command = 'tare'
    return HttpResponse.json({ device_id: id, status: 'queued' } satisfies S['ScaleTareQueued'])
  }),
]
