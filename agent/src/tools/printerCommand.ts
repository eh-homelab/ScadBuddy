import { setTimeout as sleep } from 'node:timers/promises'
import { z } from 'zod'
import { ok } from './call.js'
import { answered, COMMAND_FOLLOW_MS, type FetchResult } from './command.js'
import { defineTool, idempotencyKey, json, type Tool, type ToolContext, ToolError } from './registry.js'

// A printer command over ScadBuddy's LAN link (#7276 P27, P37, P51; backend
// printers/native.py `command_response`). Its route answers 200 with its own model, or 202
// with `LanCommandPending` (`{workflow_id}`) while the printer is slow. That 202 is not an
// operation, so `command()`'s follow does not apply: it is followed here at
// `GET /api/v1/printers/{printer_id}/commands/{command_id}`, which answers the same model the
// route would have, or 202 while the command runs.

/** The source a printer command tool names. */
export const LAN_SOURCE = "ScadBuddy's LAN link to the printer"

/** The headers a printer command sends: the call's one key (32 or 64 hex digits). */
export type PrinterCommandHeaders = { 'Idempotency-Key': string }

/** A printer command still running when the follow window ended: read it with printer_command_get. */
export type PrinterCommandRunning = { status: 'running'; command_id: string; next: string }

function pendingOf(result: FetchResult<unknown>): string | undefined {
  if (result.response.status !== 202) return undefined
  const id = (result.data as { workflow_id?: unknown } | undefined)?.workflow_id
  if (typeof id !== 'string' || id === '') throw new ToolError('ScadBuddy answered 202 with no command id.')
  return id
}

/**
 * Send a printer command with this call's `Idempotency-Key`, sent again with the same key while
 * it goes unanswered (so a lost answer reaches the printer once), and follow a 202 for
 * `COMMAND_FOLLOW_MS`. Answers the route's model, a problem as a ToolError, or past the window
 * the running command, for the model to read with printer_command_get.
 */
export async function printerCommand<T>(
  ctx: ToolContext,
  printerId: string,
  what: string,
  send: (headers: PrinterCommandHeaders) => Promise<FetchResult<T>>,
): Promise<T | PrinterCommandRunning> {
  const headers: PrinterCommandHeaders = { 'Idempotency-Key': idempotencyKey(ctx) }
  const gaveUp = ' It may have reached the printer anyway: read the printer before trying again.'
  let result: FetchResult<unknown> = await answered(ctx, () => send(headers), what, gaveUp)
  const deadline = performance.now() + (ctx.commandFollowMs ?? COMMAND_FOLLOW_MS)
  for (let step = 1, id = pendingOf(result); id !== undefined; step++, id = pendingOf(result)) {
    if (performance.now() >= deadline) {
      return {
        status: 'running',
        command_id: id,
        next:
          `${what} is still running on the printer. Read its outcome with printer_command_get ` +
          `(printer_id, command_id) until it is no longer running; do not send it again.`,
      }
    }
    await ctx.progress(step, undefined, `${what}: running`)
    await sleep(ctx.pollIntervalMs, undefined, { signal: ctx.signal })
    const command_id = id
    result = await answered(
      ctx,
      () =>
        ctx.backend.GET('/api/v1/printers/{printer_id}/commands/{command_id}', {
          params: { path: { printer_id: printerId, command_id } },
          signal: ctx.signal,
        }),
      `read ${what}`,
    )
  }
  return (await ok(Promise.resolve(result), what)) as T
}

const COMMAND_ID = /^lan-command-[A-Za-z0-9_+-]{1,480}$/

/** The read of a printer command a tool handed back as running (P51). */
export const printerCommandGet: Tool = defineTool({
  name: 'printer_command_get',
  label: 'Read a printer command',
  description:
    "Read the outcome of a printer command that a K-profile or AMS slot tool answered `status: \"running\"` " +
    'for: the answer that tool would have given, or `running` again while the printer has not answered. It ' +
    'sends nothing to the printer. Never send the write again in its place. 404 `command_not_found` for an ' +
    'unknown id or one of another printer.',
  input: z.object({
    printer_id: z.string().uuid().describe("A registry printer's id"),
    command_id: z.string().regex(COMMAND_ID).describe('The `command_id` the tool answered'),
  }),
  risk: 'read',
  source: LAN_SOURCE,
  routes: ['GET /api/v1/printers/{printer_id}/commands/{command_id}'],
  summarize: ({ printer_id, command_id }) => `Read command ${command_id} of printer ${printer_id}`,
  handler: async ({ printer_id, command_id }, ctx) =>
    json(
      await printerCommand(ctx, printer_id, `command ${command_id}`, () =>
        ctx.backend.GET('/api/v1/printers/{printer_id}/commands/{command_id}', {
          params: { path: { printer_id, command_id } },
          signal: ctx.signal,
        }),
      ),
    ),
})
