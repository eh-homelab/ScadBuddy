import type { CallEvent, CallOutcome, DeviceMethod } from '../../api/devices'

/** What the page asks the person to do while a call is open. */
export const PROMPT: Record<DeviceMethod, string> = {
  'nfc.read': 'Hold the tag to the back of the phone.',
  'nfc.write': 'Hold the tag to the back of the phone to write it.',
  'code.scan': 'Point the phone’s camera at the code and take the photo.',
  'camera.capture': 'Take the photo on the phone.',
}

/** What each way a call ends says. */
export const FAILED: Record<CallOutcome, string> = {
  done: 'Done.',
  error: 'The device reported an error.',
  offline: 'The device went offline. Open the ScadBuddy app on it, then try again.',
  timeout: 'The device did not finish in time. Try again.',
  denied: 'The request was denied.',
}

export const num = (value: unknown) => (typeof value === 'number' && Number.isFinite(value) ? value : undefined)
export const str = (value: unknown) => (typeof value === 'string' && value ? value : undefined)

/** What one event says, in words. */
export function eventText(event: CallEvent): string {
  const { data } = event
  switch (event.kind) {
    case 'device.tag_found': {
      const uid = str(data.uid) ?? 'with no UID'
      const from = num(data.resume_from)
      if (from !== undefined) return `Found tag ${uid} again. Reading from block ${from}.`
      const blocks = num(data.blocks)
      return blocks !== undefined ? `Found tag ${uid}, ${blocks} blocks.` : `Found tag ${uid}.`
    }
    case 'device.tag_progress':
      return `Read ${num(data.read) ?? '?'} of ${num(data.of) ?? '?'} blocks.`
    case 'device.tag_lost': {
      const error = str(data.error)
      return `Lost the tag after ${num(data.read) ?? 0} blocks${error ? ` (${error})` : ''}. Hold it to the phone again.`
    }
    case 'device.call_done':
      return data.complete === false ? 'Done. Some blocks were not read.' : 'Done.'
    case 'device.call_failed': {
      const outcome = str(data.outcome) as CallOutcome | undefined
      const error = str(data.error)
      return `${FAILED[outcome ?? 'error'] ?? FAILED.error}${error ? ` ${error}` : ''}`
    }
    default:
      return event.kind
  }
}

/** The read's events, then the live ones it did not hold yet: the read is in log order. */
export function merged(current: CallEvent[], read: CallEvent[]): CallEvent[] {
  const seen = new Set(read.map((one) => one.id))
  return [...read, ...current.filter((one) => !seen.has(one.id))]
}

/** The newest block count, for the strip. */
export function progressOf(events: CallEvent[]): { read: number; of: number } | undefined {
  for (let at = events.length - 1; at >= 0; at -= 1) {
    const event = events[at]
    if (!event) continue
    const of = num(event.data.of) ?? num(event.data.blocks)
    if ((event.kind === 'device.tag_progress' || event.kind === 'device.tag_found') && of !== undefined) {
      // A tag found again resumes where the read stopped.
      return { read: num(event.data.read) ?? num(event.data.resume_from) ?? 0, of }
    }
  }
  return undefined
}
