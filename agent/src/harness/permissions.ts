import type { CanUseTool, HookCallbackMatcher, PermissionResult } from '@anthropic-ai/claude-agent-sdk'

// The permission seam between the SDK and ScadBuddy's risk tiers (spec §8.1–§8.2,
// issue #258). Every tool call is mapped to a tier and the tier to a decision:
//
//   read, write → allow (spec §8.1: "auto-allowed within the principal's tier")
//   outward     → needs_approval (spec §8.2: "Outward tools always need a human
//                 approval in the ScadBuddy UI")
//
// The approval UI and the resume-after-approval flow are #258. Until then
// `needs_approval` is answered with a DENY whose message says the action needs
// approval, so an outward tool can never run unattended.
//
// It is enforced twice, as spec §8.2 asks ("the SDK permission callback and a
// PreToolUse hook"):
//   - the PreToolUse hook runs FIRST, before any allow rule or permission mode,
//     and "a hook deny applies even in bypassPermissions mode"
//     (https://code.claude.com/docs/en/agent-sdk/permissions); it denies
//     anything that is not allowed and passes the rest on without a verdict;
//   - canUseTool is called for whatever no earlier step resolved (same page,
//     "canUseTool callback") and returns the same decision.
// The hook alone would suffice for the deny; canUseTool is what ALLOWS read and
// write tools, because a hook `allow` "does not skip the deny and ask rules".

export const RISK_TIERS = ['read', 'write', 'outward'] as const
export type RiskTier = (typeof RISK_TIERS)[number]

/**
 * Maps a tool name as the SDK reports it (`mcp__{server}__{tool}` for MCP
 * tools, per https://code.claude.com/docs/en/agent-sdk/custom-tools) to its
 * tier. `undefined` means "not ScadBuddy's": treated as `outward` (spec §8.1,
 * "A plugin tool ScadBuddy doesn't recognise defaults to outward").
 */
export type TierResolver = (toolName: string) => RiskTier | undefined

export type ToolDecision =
  | { decision: 'allow'; tier: RiskTier }
  | { decision: 'needs_approval'; tier: RiskTier; reason: string }
  | { decision: 'deny'; tier: RiskTier; reason: string }

export function decide(toolName: string, tierOf: TierResolver): ToolDecision {
  const tier = tierOf(toolName) ?? 'outward'
  if (tier === 'read' || tier === 'write') return { decision: 'allow', tier }
  return {
    decision: 'needs_approval',
    tier,
    reason:
      `${toolName} is an outward action and needs a human approval in the ScadBuddy UI. ` +
      'Approvals are not available yet (#258), so it was not run. Tell the user what you ' +
      'wanted to do instead of retrying.',
  }
}

/** Observes each decision (session events, audit log, tests). Must not throw. */
export type DecisionListener = (toolName: string, decision: ToolDecision) => void

function toPermissionResult(decision: ToolDecision): PermissionResult {
  return decision.decision === 'allow'
    ? { behavior: 'allow' }
    : { behavior: 'deny', message: decision.reason }
}

export function makeCanUseTool(tierOf: TierResolver, onDecision?: DecisionListener): CanUseTool {
  return (toolName, input) => {
    const decision = decide(toolName, tierOf)
    onDecision?.(toolName, decision)
    const result = toPermissionResult(decision)
    // The SDK passes the input back to the tool from `updatedInput` when set;
    // hand it through unchanged.
    return Promise.resolve(result.behavior === 'allow' ? { ...result, updatedInput: input } : result)
  }
}

export function makePreToolUseHook(tierOf: TierResolver, onDecision?: DecisionListener): HookCallbackMatcher {
  return {
    hooks: [
      (input) => {
        if (input.hook_event_name !== 'PreToolUse') return Promise.resolve({})
        const decision = decide(input.tool_name, tierOf)
        if (decision.decision === 'allow') return Promise.resolve({})
        onDecision?.(input.tool_name, decision)
        return Promise.resolve({
          hookSpecificOutput: {
            hookEventName: 'PreToolUse' as const,
            permissionDecision: 'deny' as const,
            permissionDecisionReason: decision.reason,
          },
        })
      },
    ],
  }
}
