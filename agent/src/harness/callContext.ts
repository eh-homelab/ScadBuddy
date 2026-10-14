import { z } from 'zod'
import { TOOL_USE_META } from '../telemetry/trace.js'

// The parts of the MCP SDK's RequestHandlerExtra that an in-process tool handler
// reads: the call's abort signal, and its tool_use id, which Claude Code puts in
// `_meta` under TOOL_USE_META (measured on 2.1.283 and 2.1.287). ask_user
// (questions.ts) and request_user_attention (attention.ts) share it (#1222).

const ExtraSchema = z.object({
  signal: z.instanceof(AbortSignal),
  _meta: z.object({ [TOOL_USE_META]: z.string().min(1).optional() }).loose().optional(),
})

/** The call's tool_use id and abort signal, or why they are not there (a measured fact about Claude Code no longer holds). */
export function callContext(extra: unknown): { ok: true; toolUseId: string; signal: AbortSignal } | { ok: false; error: string } {
  const parsed = ExtraSchema.safeParse(extra)
  if (!parsed.success) return { ok: false, error: `the call's context is not as expected (${z.prettifyError(parsed.error)})` }
  const toolUseId = parsed.data._meta?.[TOOL_USE_META]
  if (toolUseId === undefined) return { ok: false, error: 'the call has no tool_use id' }
  return { ok: true, toolUseId, signal: parsed.data.signal }
}
