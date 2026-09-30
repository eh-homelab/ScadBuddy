import { z } from 'zod'
import { hasTier } from '../auth/principal.js'
import { defineTool, errorResult, json, type Tool } from './registry.js'
import { page, PAGED, pageInput } from './pagination.js'

// The `confirm` half of spec §8.2's prepare/confirm flow for external MCP
// clients. An outward tool call (the `prepare`, registry.ts runTool) records
// a pending approval in `ai_approvals` and returns its id; `confirm_action`
// runs the call once a human has approved it in the ScadBuddy UI. The rules
// (same principal, same input hash, approved, unused, unexpired, used once)
// are the store's: approvals/mcp.ts `claim`. Without a database the store is
// pending.ts's in-memory one, which never confirms.

export const approvalTools: Tool[] = [
  defineTool({
    name: 'list_pending_actions',
    description: 'Outward actions this caller has prepared and that are waiting for a human approval.' + PAGED,
    input: z.object({ ...pageInput }),
    risk: 'read',
    routes: [],
    handler: async (args, { pending, principal }) =>
      json(
        page(
          (await pending.list(principal)).map((a) => ({
            pending_action_id: a.id,
            tool: a.tool,
            summary: a.summary,
            expires_at: a.expiresAt.toISOString(),
          })),
          args,
          (a) => a.pending_action_id,
          'list_pending_actions',
        ),
      ),
  }),

  defineTool({
    name: 'confirm_action',
    description:
      'Complete an outward action prepared earlier (an outward tool answered pending_approval), once a human ' +
      'has approved it in the ScadBuddy UI. Pass the pending_action_id and exactly the same arguments the tool ' +
      'was called with: the approval covers that input only. Answers pending_approval until the human decides; ' +
      'runs the action once when approved.',
    input: z.object({
      pending_action_id: z.string().min(1),
      // `catchall`, not `z.record`: see `params` in common.ts.
      arguments: z
        .object({})
        .catchall(z.unknown())
        .default({})
        .describe('The same arguments the outward tool was called with when it was prepared.'),
    }),
    risk: 'outward',
    // This is the approval path itself; gating it would only prepare another pending action.
    approval: 'none',
    routes: [],
    handler: async ({ pending_action_id, arguments: args }, ctx) => {
      const { pending, principal } = ctx
      // The audit row (#258, registry.ts RunReport): a confirm that ran nothing
      // is `refused`, never `ok`; one that ran names the approval and the tool.
      const refused = (reason: string) => {
        ctx.report?.({ outcome: 'refused', detail: reason })
        return errorResult(reason)
      }
      const action = await pending.find(pending_action_id, principal)
      if (!action) return refused(`no pending action ${pending_action_id} for this caller (it may have expired)`)
      const tool = ctx.lookup?.(action.tool)
      if (!tool?.gated) return errorResult(`pending action ${pending_action_id} is for ${action.tool}, which this server cannot run`)
      if (!hasTier(principal, tool.risk)) return refused(`${tool.name} needs the "${tool.risk}" tier`)
      // Parsed as the prepare parsed them, so the hash compares like with like.
      // The tool's own schema, as the prepare parsed it (runTool: `tool.parse`),
      // so the hash compares like with like and no top-level refinement is lost.
      let input: Record<string, unknown>
      try {
        input = tool.parse(args)
      } catch (err) {
        if (!(err instanceof z.ZodError)) throw err
        return refused(
          `Not confirmed: these arguments are not valid for ${tool.name} (${z.prettifyError(err)}). ` +
            'Pass exactly the arguments the action was prepared with. Nothing was sent.',
        )
      }
      const claim = await pending.claim(pending_action_id, principal, input)
      if (claim.status === 'refused') return refused(claim.reason)
      if (claim.status === 'pending') {
        ctx.report?.({
          outcome: 'refused',
          detail: `waiting for approval (pending action ${claim.action.id}); nothing was sent`,
        })
        return json({
          status: 'pending_approval',
          pending_action_id: claim.action.id,
          summary: claim.action.summary,
          expires_at: claim.action.expiresAt.toISOString(),
          next: 'Not approved yet: nothing was sent. Ask the user to approve it in the ScadBuddy UI, then call confirm_action again.',
        })
      }
      // Approved and now used up: whatever happens next, this approval never runs again.
      // The report also names the tool, so runToolWithOutcome's untrusted-data
      // envelope (and its error attribution) says the content is that tool's, not confirm_action's.
      ctx.report?.({ approvalId: claim.action.id, ran: { tool: tool.name, input } })
      return tool.execute(input, ctx)
    },
  }),
]
