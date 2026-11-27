import { ApiError, request } from './client'
import type { Resolved, TagReadResult } from './filament'

/**
 * #7905 — the devices routes (plan `docs/superpowers/plans/2026-11-27-devices.md` Tasks 2 to 6).
 * Go core serves them (`tools/go/core/internal/devices`), so `pnpm gen:api`, which exports
 * Python's OpenAPI only, has no types for them. These types are written by hand from the plan's
 * route and event shapes. When the core devices PR merges, check them against its JSON and
 * change them here, in this one module.
 */

/** The four methods a device can announce (spec §Capabilities). */
export type DeviceMethod = 'nfc.read' | 'nfc.write' | 'code.scan' | 'camera.capture'

/** A paired device, as `GET /devices` lists it. */
export interface Device {
  id: string
  name: string
  model: string
  app_version: string
  capabilities: DeviceMethod[]
  created_at: string
  last_seen_at: string | null
  /** `last_seen_at` is under 75 s old and the device is not revoked. */
  online: boolean
  /** The token's idle expiry: each device request moves it to now + 1 hour. */
  expires_at: string | null
  /** The pairing ends here, whatever its use. */
  hard_cap_at: string | null
}

export interface Pairing {
  /** `https://<request host>/api/v1/devices/pair/<code>`. */
  code_url: string
  expires_at: string
}

/** How a call ended. `null` while it is open. */
export type CallOutcome = 'done' | 'error' | 'offline' | 'timeout' | 'denied'

export interface DeviceCallRow {
  id: string
  device_id: string
  method: DeviceMethod
  args: Record<string, unknown>
  caller_kind: 'person' | 'assistant' | 'flow'
  outcome: CallOutcome | null
  created_at: string
  deadline: string
  sent_at: string | null
  ended_at: string | null
  pending_action_id: string | null
}

/** The end of a read call: the stored read's id, and for `nfc.read` the tag's UID and whether every block was read. */
export interface CallResult {
  read_id?: string
  uid?: string
  complete?: boolean
  error?: string
}

/** One event of a call, from the event stream: kind `device.<kind>`, its fields in `data`. */
export interface CallEvent {
  id: string
  kind: string
  at?: string
  data: Record<string, unknown>
}

export interface CallView {
  call: DeviceCallRow
  /** The call's events so far, in log order; the realtime socket carries the new ones. */
  events: CallEvent[]
  result: CallResult | null
}

/** The bus event kinds that end a call. */
export const ENDING_KINDS = new Set(['device.call_done', 'device.call_failed'])

/** The realtime topic of one call's events. */
export const callTopic = (callId: string) => `device:call:${callId}`

/**
 * The pairing URL on the host the person is on (`window.location`): the app then pairs with
 * the address this page has, the SSO host away from home or the LAN host at home. Core
 * builds it from the request's host, which a dev proxy can change.
 */
export function onThisHost(codeUrl: string): string {
  try {
    const url = new URL(codeUrl, window.location.origin)
    return new URL(`${url.pathname}${url.search}`, window.location.origin).toString()
  } catch {
    return codeUrl
  }
}

/** The `scadbuddy://pair` link the app opens. */
export const pairLink = (codeUrl: string) => `scadbuddy://pair?url=${encodeURIComponent(codeUrl)}`

const seg = encodeURIComponent

export const listDevices = () => request<{ devices: Device[] }>('/devices')

export const createPairing = () => request<Pairing>('/devices/pairings', { method: 'POST' })

export const revokeDevice = (id: string) => request<void>(`/devices/${seg(id)}`, { method: 'DELETE' })

/** The device is not online: no pod holds its socket (409). */
export class DeviceOfflineError extends Error {
  constructor() {
    super('The device is offline. Open the ScadBuddy app on it, then try again.')
    this.name = 'DeviceOfflineError'
  }
}

/** Starts a call. A 409 is the device offline. */
export async function startCall(deviceId: string, method: DeviceMethod, args: Record<string, unknown> = {}, timeoutS = 60) {
  try {
    return await request<{ call_id: string }>(`/devices/${seg(deviceId)}/calls`, {
      method: 'POST',
      body: JSON.stringify({ method, args, timeout_s: timeoutS }),
    })
  } catch (error) {
    if (error instanceof ApiError && error.status === 409) throw new DeviceOfflineError()
    throw error
  }
}

export const getCall = (callId: string) => request<CallView>(`/devices/calls/${seg(callId)}`)

/** Task 6: the tag decoder takes a device read in place of inline records. */
export const tagReadFromDevice = (readId: string) =>
  request<TagReadResult>('/filament/tags/reads', { method: 'POST', body: JSON.stringify({ read_id: readId }) })

/** One code a scan read (#8155), and the thing it names. */
export interface ScannedCode {
  format: string
  text: string
  resolved: Resolved | null
}

/** Task 6: the code decoder (#8155) takes a device read in place of a blob key. */
export const codeScanFromDevice = (readId: string) =>
  request<{ id: string; codes: ScannedCode[]; error: string | null }>('/scan/codes', {
    method: 'POST',
    body: JSON.stringify({ read_id: readId, source: 'app', location: null }),
  })
