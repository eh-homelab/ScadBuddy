import { z } from 'zod'

/** What Claude Code puts in an MCP call's `_meta` (measured on 2.1.283 and 2.1.287). */
export const TOOL_USE_ID_META = 'claudecode/toolUseId'

/** The parts of the MCP SDK's RequestHandlerExtra a question handler reads. */
const ExtraSchema = z.object({
  signal: z.instanceof(AbortSignal),
  _meta: z.object({ [TOOL_USE_ID_META]: z.string().min(1).optional() }).loose().optional(),
})

/**
 * #1222 — ask_user's and request_user_attention's call context: the call's tool_use id
 * and abort signal, or why the call cannot be asked (a measured fact about Claude Code
 * no longer holds). One parse, so the two handlers cannot drift apart.
 */
export function callContext(
  extra: unknown,
): { ok: true; toolUseId: string; signal: AbortSignal } | { ok: false; error: string } {
  const parsed = ExtraSchema.safeParse(extra)
  if (!parsed.success) return { ok: false, error: `the call's context is not as expected (${z.prettifyError(parsed.error)})` }
  const toolUseId = parsed.data._meta?.[TOOL_USE_ID_META]
  if (toolUseId === undefined) return { ok: false, error: 'the call has no tool_use id' }
  return { ok: true, toolUseId, signal: parsed.data.signal }
}
