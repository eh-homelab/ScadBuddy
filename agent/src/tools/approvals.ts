import { z } from 'zod'
import { defineTool, errorResult, json, type Tool } from './registry.js'

// The `confirm` half of spec §8.2's prepare/confirm flow for external MCP
// clients. An outward tool call (the `prepare`) records a pending action and
// returns its id; `confirm_action` is meant to complete it once a human has
// approved it in the ScadBuddy UI. That UI is #258 and does not exist yet, so
// confirm_action ALWAYS refuses: no outward action can run from an agent
// until approvals land. This is deliberate, not a stub that forgot to work.

export const approvalTools: Tool[] = [
  defineTool({
    name: 'list_pending_actions',
    description: 'Outward actions this caller has prepared and that are waiting for a human approval.',
    input: z.object({}),
    risk: 'read',
    routes: [],
    handler: async (_args, { pending, principal }) =>
      json(
        pending.list(principal.id).map((a) => ({
          pending_action_id: a.id,
          tool: a.tool,
          summary: a.summary,
          expires_at: a.expiresAt.toISOString(),
        })),
      ),
  }),

  defineTool({
    name: 'confirm_action',
    description:
      'Complete an outward action prepared earlier, once a human has approved it in the ScadBuddy UI. ' +
      'The approval UI is not available yet (#258), so this currently always refuses.',
    input: z.object({ pending_action_id: z.string().min(1) }),
    risk: 'outward',
    // This is the approval path itself; gating it would only prepare another pending action.
    approval: 'none',
    routes: [],
    handler: async ({ pending_action_id }, { pending, principal }) => {
      const action = pending.get(pending_action_id, principal.id)
      if (!action) return errorResult(`no pending action ${pending_action_id} for this caller (it may have expired)`)
      return errorResult(
        `Not confirmed: "${action.summary}" needs a human approval in the ScadBuddy UI, and that approval ` +
          'flow is not available yet (#258). Nothing was sent. Ask the user to do this in ScadBuddy instead.',
      )
    },
  }),
]
