import type { CallToolResult } from '@modelcontextprotocol/sdk/types.js'
import { z } from 'zod'
import type { ApprovalRecord } from '../approvals/service.js'
import { ownerOf } from '../auth/principal.js'
import { defineTool, errorResult, json, type Tool, type ToolContext } from './registry.js'

// The `confirm` half of spec §8.2's prepare/confirm flow for external MCP
// clients. An outward tool call (the `prepare`, registry.ts `runTool`) records
// a pending action (pending.ts) and its approval in `ai_approvals`
// (approvals/service.ts), both under one id. A human decides the approval in
// the ScadBuddy UI; `confirm_action` then runs the prepared call, once.
//
// What a confirm checks, in order:
//   - the pending action is this caller's (pending.ts `get` by principal id);
//   - its approval row is visible to this caller as the principal that asked
//     (service.ts `get`: a sessionless approval is the requester's own);
//   - the row is for this tool and this input: the HMAC of the prepared
//     arguments must equal the row's `input_hash`, so a decision applies to
//     exactly the call it was shown for (spec §8.2, "A decision binds to the
//     input hash");
//   - the row is approved and still usable, and using it succeeds
//     (`consumeById`, one conditional UPDATE on `consumed_at IS NULL`), so two
//     confirms of one approval cannot both run it.
// Without an approval store (no database) every confirm is refused.

const NOT_SENT = 'Nothing was sent.'

function why(approval: ApprovalRecord): string {
  switch (approval.decision) {
    case 'denied':
      return `The user denied it in the ScadBuddy UI. ${NOT_SENT} Do not retry it unless the user asks you to.`
    case 'expired':
      return `Nobody decided on it before it expired. ${NOT_SENT} Prepare it again if it is still wanted.`
    case 'cancelled':
      return `Its approval was cancelled (${approval.reason ?? 'no reason given'}). ${NOT_SENT}`
    case 'approved':
      return approval.consumedAt
        ? `It was already confirmed and ran (approvals are used once). ${NOT_SENT}`
        : `Its approval no longer applies (${approval.reason ?? 'its usable window has passed'}). ${NOT_SENT} Prepare it again.`
    default:
      return `It is still waiting for a human approval in the ScadBuddy UI. ${NOT_SENT} Ask the user to approve it, then call confirm_action again.`
  }
}

/** The approval row behind a pending action, as the caller sees it; undefined when there is none. */
async function approvalOf(id: string, ctx: ToolContext): Promise<ApprovalRecord | undefined> {
  try {
    return await ctx.approvals?.get(id, ownerOf(ctx.principal))
  } catch (err) {
    if ((err as { code?: string }).code === 'not_found') return undefined
    throw err
  }
}

async function confirm(id: string, ctx: ToolContext): Promise<CallToolResult> {
  const { pending, principal, approvals } = ctx
  const action = pending.get(id, principal.id)
  if (!action?.run) {
    return errorResult(
      `no pending action ${id} for this caller (it may have expired, or been prepared before a restart or on ` +
        'another ScadBuddy agent); prepare it again',
    )
  }
  if (!approvals) {
    return errorResult(
      `Not confirmed: "${action.summary}" needs a human approval in the ScadBuddy UI, and this agent has no ` +
        `database to record approvals in (#258). ${NOT_SENT} Ask the user to do this in ScadBuddy instead.`,
    )
  }
  const approval = await approvalOf(id, ctx)
  if (!approval) {
    pending.remove(id)
    return errorResult(`no approval ${id} for this caller. ${NOT_SENT} Prepare it again.`)
  }
  if (approval.tool !== action.tool || approval.inputHash !== approvals.hash(action.tool, action.args as Record<string, unknown>)) {
    pending.remove(id)
    return errorResult(`approval ${id} is for a different call than the one prepared. ${NOT_SENT} Prepare it again.`)
  }
  if (approval.decision !== 'approved' || approval.consumedAt !== null || approval.revokedAt !== null) {
    if (approval.decision !== null) pending.remove(id)
    return errorResult(`Not confirmed: "${action.summary}". ${why(approval)}`)
  }
  const used = await approvals.consumeById(id)
  if (!used) {
    pending.remove(id)
    return errorResult(`Not confirmed: "${action.summary}". ${why((await approvalOf(id, ctx)) ?? approval)}`)
  }
  pending.remove(id)
  return action.run(ctx)
}

export const approvalTools: Tool[] = [
  defineTool({
    name: 'list_pending_actions',
    description:
      'Outward actions this caller has prepared, with the state of their approval in the ScadBuddy UI ' +
      '(pending, approved, denied, expired, cancelled, used).',
    input: z.object({}),
    risk: 'read',
    routes: [],
    handler: async (_args, ctx) => {
      const listed = []
      for (const a of ctx.pending.list(ctx.principal.id)) {
        const approval = ctx.approvals ? await approvalOf(a.id, ctx) : undefined
        listed.push({
          pending_action_id: a.id,
          tool: a.tool,
          summary: a.summary,
          approval: !approval
            ? 'unavailable'
            : approval.consumedAt
              ? 'used'
              : (approval.decision ?? 'pending'),
          expires_at: approval?.decision === 'approved' && approval.usableUntil ? approval.usableUntil : (approval?.expiresAt ?? a.expiresAt.toISOString()),
        })
      }
      return json(listed)
    },
  }),

  defineTool({
    name: 'confirm_action',
    description:
      'Run an outward action prepared earlier, once a human has approved it in the ScadBuddy UI. ' +
      'Refused while the approval is pending, and after it was denied, expired or used: each approval runs its call once.',
    input: z.object({ pending_action_id: z.string().min(1) }),
    risk: 'outward',
    // This is the approval path itself; gating it would only prepare another pending action.
    approval: 'none',
    routes: [],
    handler: async ({ pending_action_id }, ctx) => confirm(pending_action_id, ctx),
  }),
]
