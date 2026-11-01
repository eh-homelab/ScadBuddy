import { z } from 'zod'
import { unwrapUntrusted } from '../safety/untrusted.js'
import { TOOL_CALL_RESULT_TOOL } from '../temporal/toolCall.js'
import { defineTool, text, ToolError, type Tool } from './registry.js'

// What a session's call returned when the agent service restarted before its turn read it (unified
// runtime plan 2026-10-13 Step 9.2): the call ran as its ToolCall workflow and finished, and the
// restart message names it (sessions/turnHost.ts `restartedPrompt`) without its result, which is
// tool output and so reaches the model only as a tool result (#258). This is that result.

export const toolCallTools: Tool[] = [
  defineTool({
    name: TOOL_CALL_RESULT_TOOL,
    label: 'Read a finished call’s result',
    description:
      'Read what one of this session’s calls returned, by its call id, when the agent service restarted before the ' +
      'turn read it: the restart message names each such call. Only for a call that ran as an activity and finished.',
    input: z.object({ call_id: z.string().regex(/^[A-Za-z0-9_-]{1,128}$/, 'a tool_use id, as the restart message gives it') }),
    risk: 'read',
    routes: [],
    source: 'a tool call this session made before the agent service restarted',
    handler: async ({ call_id }, ctx) => {
      if (ctx.session === undefined) throw new ToolError(`${TOOL_CALL_RESULT_TOOL} reads a session's calls; this call is in none`)
      if (!ctx.toolCallResults) throw new ToolError('this agent has no workflow service to read a call from')
      const result = await ctx.toolCallResults(ctx.session, call_id)
      if (!result) return text(`call ${call_id} of this session has no finished result to read`)
      // Out of the call's own envelope: this tool's result is wrapped once more, as every tool's is (registry.ts).
      return { ...result, content: result.content.map((block) => (block.type === 'text' ? { ...block, text: unwrapUntrusted(block.text) } : block)) }
    },
  }),
]
