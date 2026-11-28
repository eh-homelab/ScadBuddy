import { HttpResponse, http } from 'msw'
import type { CallEvent, CallResult, Device, DeviceCallRow, DeviceMethod } from '../../api/devices'
import { emitRealtime } from '../realtime'

/**
 * #7905 — the devices routes of Go core (plan 2026-11-27-devices Tasks 2 to 5). Two paired
 * phones: `PHONE` online, `OLD_PHONE` offline. A call to an online device plays a script of
 * events on the realtime topics `device:<id>` and `device:call:<id>`, 400 ms apart (`setStepMs`), and
 * `GET /devices/calls/{id}` answers the events so far, as core replays them from its event log.
 * `nfc.read` finds the tag, loses it once and resumes, as the spec's example does.
 */

const BASE = '/api/v1/devices'

export const PHONE: Device = {
  id: '00000000-0000-4000-8000-00000000d001',
  name: 'Pixel 8',
  model: 'Google Pixel 8',
  app_version: '1.4.0',
  capabilities: ['nfc.read', 'nfc.write', 'code.scan', 'camera.capture'],
  created_at: '2026-11-27T08:00:00Z',
  last_seen_at: '2026-11-27T09:59:40Z',
  online: true,
  expires_at: '2026-11-27T10:59:40Z',
  hard_cap_at: '2026-12-27T08:00:00Z',
}

export const OLD_PHONE: Device = {
  id: '00000000-0000-4000-8000-00000000d002',
  name: 'Old phone',
  model: 'Samsung Galaxy S10',
  app_version: '1.3.2',
  capabilities: ['nfc.read', 'code.scan'],
  created_at: '2026-11-20T08:00:00Z',
  last_seen_at: '2026-11-26T18:12:00Z',
  online: false,
  expires_at: '2026-11-26T19:12:00Z',
  hard_cap_at: '2026-12-20T08:00:00Z',
}

/** The UID of the mock tag `nfc.read` reads: the spec's Bambu spool. */
export const MOCK_UID = '9CC69CE3'

interface MockCall {
  row: DeviceCallRow
  events: CallEvent[]
  result: CallResult | null
}

let devices: Device[] = []
let calls = new Map<string, MockCall>()
let seq = 0
let stepMs = 400
const timers = new Set<ReturnType<typeof setTimeout>>()

/** Tests: how far apart a call's scripted events are. */
export function setStepMs(ms: number) {
  stepMs = ms
}

/** Tests: the devices the list answers. */
export function setDevices(next: Device[]) {
  devices = next.map((one) => ({ ...one }))
}

export function reset() {
  for (const timer of timers) clearTimeout(timer)
  timers.clear()
  devices = [{ ...PHONE }, { ...OLD_PHONE }]
  calls = new Map()
  seq = 0
  stepMs = 400
}
reset()

type Step = [kind: string, data: Record<string, unknown>]

function script(method: DeviceMethod, readId: string): Step[] {
  if (method === 'nfc.read') {
    return [
      ['device.tag_found', { uid: MOCK_UID, tech: 'mifare_classic', blocks: 48 }],
      ['device.tag_progress', { read: 8, of: 48 }],
      ['device.tag_lost', { read: 8, error: 'sector 2: transceive failed' }],
      ['device.tag_found', { uid: MOCK_UID, resume_from: 8 }],
      ['device.tag_progress', { read: 24, of: 48 }],
      ['device.tag_progress', { read: 48, of: 48 }],
      ['device.call_done', { read_id: readId, uid: MOCK_UID, complete: true }],
    ]
  }
  return [['device.call_done', { read_id: readId }]]
}

function play(call: MockCall, steps: Step[]) {
  const [step, ...rest] = steps
  if (!step) return
  const timer = setTimeout(() => {
    timers.delete(timer)
    const [kind, fields] = step
    const data = { call: call.row.id, device: call.row.device_id, ...fields }
    const event: CallEvent = { id: `device-event-${++seq}`, kind, at: new Date().toISOString(), data }
    call.events.push(event)
    if (kind === 'device.call_done') {
      call.row.outcome = 'done'
      call.row.ended_at = event.at ?? null
      call.result = fields as CallResult
    }
    emitRealtime(kind, [`device:${call.row.device_id}`, `device:call:${call.row.id}`], data, event.id)
    play(call, rest)
  }, stepMs)
  timers.add(timer)
}

const problem = (status: number, detail: string, extra: Record<string, unknown> = {}) =>
  HttpResponse.json({ type: 'about:blank', title: detail, status, detail, ...extra }, { status, headers: { 'Content-Type': 'application/problem+json' } })

export const handlers = [
  http.get(BASE, () => HttpResponse.json({ devices })),

  http.post(`${BASE}/pairings`, () =>
    HttpResponse.json({
      code_url: `${location.origin}${BASE}/pairings/mock-pairing-code-${++seq}/claim`,
      expires_at: new Date(Date.now() + 10 * 60_000).toISOString(),
    }),
  ),

  http.delete(`${BASE}/:id`, ({ params }) => {
    const before = devices.length
    devices = devices.filter((one) => one.id !== params.id)
    return devices.length < before ? new HttpResponse(null, { status: 204 }) : problem(404, 'No such device')
  }),

  http.post(`${BASE}/:id/calls`, async ({ params, request }) => {
    const device = devices.find((one) => one.id === params.id)
    if (!device) return problem(404, 'No such device')
    if (!device.online) return problem(409, 'The device is offline', { error: 'offline', reason: 'offline' })
    const body = (await request.json()) as { method: DeviceMethod; args?: Record<string, unknown>; timeout_s?: number }
    if (!device.capabilities.includes(body.method)) return problem(422, 'The device did not announce this method')
    const id = `00000000-0000-4000-8000-${String(++seq).padStart(12, '0')}`
    const now = new Date()
    const call: MockCall = {
      row: {
        id,
        device_id: device.id,
        method: body.method,
        args: body.args ?? {},
        caller_kind: 'person',
        outcome: null,
        created_at: now.toISOString(),
        deadline: new Date(now.getTime() + (body.timeout_s ?? 60) * 1000).toISOString(),
        sent_at: now.toISOString(),
        ended_at: null,
        pending_action_id: null,
      },
      events: [],
      result: null,
    }
    calls.set(id, call)
    play(call, script(body.method, `read-${id}`))
    return HttpResponse.json({ call_id: id }, { status: 202 })
  }),

  http.get(`${BASE}/calls/:id`, ({ params }) => {
    const call = calls.get(String(params.id))
    if (!call) return problem(404, 'No such call')
    return HttpResponse.json({ call: call.row, events: call.events, result: call.result })
  }),
]
