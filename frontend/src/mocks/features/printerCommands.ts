import { HttpResponse, http, type JsonBodyType } from 'msw'
import type { LanCommandPending } from '../../api/types'
import { printersState } from './printers'

/**
 * #7276 — a printer command over ScadBuddy's LAN link (`backend/scadbuddy/printers/native.py`
 * `command_response`), shared by the K-profile and AMS slot mocks. A route first takes the
 * printer's row: 404 for none, 409 `not_lan_linked` for one that `lan_linked` is false on
 * (`lanRefusal`). It answers 200 with its model, or, while `setPrinterCommandSlow(n)` is on,
 * 202 `LanCommandPending`, which `GET /printers/{id}/commands/{command_id}` answers 202 for
 * `n` more reads and then with the same model, on every later read too (P27, P37). While `latchPrinterCommands()` holds,
 * every command is a 202 and its follow stays 202 until the test calls the release it returned.
 * Every command's `Idempotency-Key` is kept, in order (`printerCommandKeys`). A key sent again
 * joins the run it started (P38, P41): a handler asks `joined` before it changes anything, and
 * the command is published once (`printerCommandsPublished`).
 */

interface Running {
  printer: string
  reads: number
  body: JsonBodyType
  /** Answered once: every later follow answers the same body, as the backend reads a closed run. */
  done: boolean
}

interface Started {
  workflowId?: string
  body: JsonBodyType
}

const state = {
  slow: 0,
  latched: false,
  seq: 0,
  running: new Map<string, Running>(),
  started: new Map<string, Started>(),
  keys: [] as (string | null)[],
  published: 0,
}

export function reset(): void {
  state.slow = 0
  state.latched = false
  state.seq = 0
  state.running = new Map()
  state.started = new Map()
  state.keys = []
  state.published = 0
}

/**
 * Every command answers 202, and every follow 202, until the returned release is called: then
 * each command's next follow answers its model.
 */
export function latchPrinterCommands(): () => void {
  state.latched = true
  return () => {
    state.latched = false
    state.running.forEach((run) => (run.reads = 0))
  }
}

/** How many writes the mock published to the printer: a key sent again publishes none. */
export function printerCommandsPublished(): number {
  return state.published
}

/**
 * What names a route's run besides its printer and key, as the backend's workflow id does
 * (`workflows/lan_command.py` `lan_command_id`): the command names, and for a slot route the
 * action (`api/printer_slots.py` `_keyed`). The tray is not in it, so a key reused for `load`
 * on another tray joins the first run, and K-profile `set` and `batch` (both
 * `extrusion_cali_set`) join each other.
 */
function runName(request: Request): string {
  const path = new URL(request.url).pathname
  const slot = /\/trays\/\d+\/\d+\/([^/]+)$/.exec(path)
  if (slot) return `slot ${slot[1]}`
  if (/\/kprofiles\/delete$/.test(path)) return 'extrusion_cali_del'
  if (/\/kprofiles(\/batch)?$/.test(path)) return 'extrusion_cali_set'
  return path
}

const startedKey = (printer: string, request: Request, key: string) => `${printer}\n${runName(request)}\n${key}`

/**
 * The answer to a command whose `Idempotency-Key` already started one on `printer`: the same
 * run, followed again while it runs, else its answer. None for a new key.
 */
export function joined(printer: string, request: Request): Response | undefined {
  const key = request.headers.get('Idempotency-Key')
  const first = key ? state.started.get(startedKey(printer, request, key)) : undefined
  if (!first) return undefined
  state.keys.push(key)
  if (first.workflowId && state.running.get(first.workflowId)?.done === false) {
    return HttpResponse.json({ workflow_id: first.workflowId } satisfies LanCommandPending, { status: 202 })
  }
  return HttpResponse.json(first.body)
}

/** Every command answers 202, and its follow 202 `reads` times more before the answer; 0 turns it off. */
export function setPrinterCommandSlow(reads: number): void {
  state.slow = reads
}

/** The `Idempotency-Key` of each command sent, in order (`null` for none). */
export function printerCommandKeys(): (string | null)[] {
  return state.keys
}

const problem = (status: number, detail: string, code?: string) =>
  HttpResponse.json({ title: detail, status, detail, ...(code ? { code } : {}) }, { status })

/** The route's refusal of a printer it cannot reach over the LAN, as `printers/native.py` `lan_row` words it; none when it can. */
export function lanRefusal(id: string, feature: string): Response | undefined {
  const row = printersState().find((printer) => printer.id === id)
  if (!row) return problem(404, `no printer ${id}`)
  if (!row.lan_linked) {
    return problem(
      409,
      `${row.name} is not linked over the LAN, so ScadBuddy cannot reach its ${feature}. Link it in Settings > Printers.`,
      'not_lan_linked',
    )
  }
  return undefined
}

/** A K-profile write's 428 when it names no key, as `printer_kprofiles.py` `_key` answers. */
export function keyRequired(request: Request): Response | undefined {
  if (request.headers.get('Idempotency-Key')) return undefined
  return HttpResponse.json(
    { type: 'https://scadbuddy.dev/problems/idempotency-key-required', title: 'Idempotency-Key required', status: 428 },
    { status: 428 },
  )
}

/** Whether the next command answers in its route's own reply (no 202 to follow). */
export function answersAtOnce(): boolean {
  return state.slow <= 0 && !state.latched
}

/** The command's answer: `body` at once, or a 202 while `setPrinterCommandSlow` is on. */
export function commandAnswer(printer: string, request: Request, body: JsonBodyType): Response {
  const key = request.headers.get('Idempotency-Key')
  state.keys.push(key)
  if (request.method !== 'GET') state.published += 1
  if (state.slow <= 0 && !state.latched) {
    if (key) state.started.set(startedKey(printer, request, key), { body })
    return HttpResponse.json(body)
  }
  const workflow_id = `lan-command-mock-${++state.seq}`
  state.running.set(workflow_id, { printer, reads: state.slow, body, done: false })
  if (key) state.started.set(startedKey(printer, request, key), { workflowId: workflow_id, body })
  return HttpResponse.json({ workflow_id } satisfies LanCommandPending, { status: 202 })
}

export const handlers = [
  http.get('/api/v1/printers/:id/commands/:command_id', ({ params }) => {
    const run = state.running.get(String(params.command_id))
    if (!run || run.printer !== params.id) return problem(404, `no command ${String(params.command_id)}`, 'command_not_found')
    if (!run.done && (state.latched || run.reads > 0)) {
      run.reads = Math.max(0, run.reads - 1)
      return HttpResponse.json({ workflow_id: String(params.command_id) } satisfies LanCommandPending, { status: 202 })
    }
    run.done = true
    return HttpResponse.json(run.body)
  }),
]
