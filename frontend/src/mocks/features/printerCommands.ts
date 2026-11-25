import { HttpResponse, http, type JsonBodyType } from 'msw'
import type { LanCommandPending } from '../../api/types'
import { printersState } from './printers'

/**
 * #7276 — a printer command over ScadBuddy's LAN link (`backend/scadbuddy/printers/native.py`
 * `command_response`), shared by the K-profile and AMS slot mocks. A route first takes the
 * printer's row: 404 for none, 409 `not_lan_linked` for one that `lan_linked` is false on
 * (`lanRefusal`). It answers 200 with its model, or, while `setPrinterCommandSlow(n)` is on,
 * 202 `LanCommandPending`, which `GET /printers/{id}/commands/{command_id}` answers 202 for
 * `n` more reads and then with the same model (P27, P37). Every command's `Idempotency-Key`
 * is kept, in order (`printerCommandKeys`).
 */

interface Running {
  printer: string
  reads: number
  body: JsonBodyType
}

const state = {
  slow: 0,
  seq: 0,
  running: new Map<string, Running>(),
  keys: [] as (string | null)[],
}

export function reset(): void {
  state.slow = 0
  state.seq = 0
  state.running = new Map()
  state.keys = []
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

/** The command's answer: `body` at once, or a 202 while `setPrinterCommandSlow` is on. */
export function commandAnswer(printer: string, request: Request, body: JsonBodyType): Response {
  state.keys.push(request.headers.get('Idempotency-Key'))
  if (state.slow <= 0) return HttpResponse.json(body)
  const workflow_id = `lan-command-mock-${++state.seq}`
  state.running.set(workflow_id, { printer, reads: state.slow, body })
  return HttpResponse.json({ workflow_id } satisfies LanCommandPending, { status: 202 })
}

export const handlers = [
  http.get('/api/v1/printers/:id/commands/:command_id', ({ params }) => {
    const run = state.running.get(String(params.command_id))
    if (!run || run.printer !== params.id) return problem(404, `no command ${String(params.command_id)}`, 'command_not_found')
    if (run.reads > 0) {
      run.reads -= 1
      return HttpResponse.json({ workflow_id: String(params.command_id) } satisfies LanCommandPending, { status: 202 })
    }
    return HttpResponse.json(run.body)
  }),
]
