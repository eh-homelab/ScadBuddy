import type { CanUseTool, HookCallbackMatcher, PermissionResult } from '@anthropic-ai/claude-agent-sdk'

// The permission seam between the SDK and ScadBuddy's risk tiers (spec §8.1–§8.2,
// issue #258). Every tool call is mapped to a tier and the tier to a decision:
//
//   read, write → allow (spec §8.1: "auto-allowed within the principal's tier")
//   outward     → needs_approval (spec §8.2: "Outward tools always need a human
//                 approval in the ScadBuddy UI")
//
// needs_approval with an ApprovalGate (a session turn, #258: src/approvals/)
// PARKS the call: `canUseTool` awaits the gate, which settles once a human
// decides, the approval expires, or the turn is interrupted. Measured on SDK
// 0.3.283 (test/approvals.sdk.test.ts): the call waits with no deadline of the
// SDK's own, the model is sent nothing meanwhile, and on approval the tool
// runs with the input the gate returns. sdk.d.ts says the same: on
// `CanUseTool`, "permission prompts have no park deadline"; the `dialogExpiry`
// option (default 5m) covers dialogs "forwarded to a remote client", and
// "Local-only permission prompts (no remote client) are unaffected".
// Without a gate (a bare harness run) needs_approval is a DENY whose message
// says the action needs approval, so an outward tool can never run unattended.
//
// Before the tier, an optional InputGuard may DENY a call by its arguments
// (the headless browser's origin allow-list and file names, #349); that
// denial holds at every tier and is never parked for approval.
//
// It is enforced twice, as spec §8.2 asks ("the SDK permission callback and a
// PreToolUse hook"):
//   - the PreToolUse hook runs FIRST, before any allow rule or permission mode,
//     and "a hook deny applies even in bypassPermissions mode"
//     (https://code.claude.com/docs/en/agent-sdk/permissions). Without a gate
//     it denies anything that is not allowed. With a gate it answers `ask` for
//     outward calls, which forces the permission prompt, i.e. `canUseTool`
//     (sdk.d.ts, 0.3.283: "With a permission prompt surface (stdio/SDK
//     canUseTool), the 'ask' path surfaces via a can_use_tool
//     control_request"), so no allow rule can let an outward call skip the
//     gate. Allowed tools pass on without a verdict;
//   - canUseTool is called for whatever no earlier step resolved (same page,
//     "canUseTool callback") and returns the same decision, awaiting the gate
//     for outward calls.
// The hook alone would suffice for the deny; canUseTool is what ALLOWS read and
// write tools, because a hook `allow` "does not skip the deny and ask rules".

export const RISK_TIERS = ['read', 'write', 'outward'] as const
export type RiskTier = (typeof RISK_TIERS)[number]

/**
 * Maps a tool name as the SDK reports it (`mcp__{server}__{tool}` for MCP
 * tools, per https://code.claude.com/docs/en/agent-sdk/custom-tools) to its
 * tier. `undefined` means "not ScadBuddy's": treated as `outward` (spec §8.1,
 * "A plugin tool ScadBuddy doesn't recognise defaults to outward").
 *
 * `input` is the call's arguments as the model sent them, for the one tool
 * whose tier depends on them: `http_request` (#827, httpRequest.ts), `read`
 * for GET and HEAD and `outward` for every other method. Every other resolver
 * ignores it. A resolver that reads it must fail closed (`outward`) on input
 * it does not recognise, since the input has not been validated yet.
 */
export type TierResolver = (toolName: string, input?: unknown) => RiskTier | undefined

/**
 * Refuses a call by its INPUT, whatever its tier: the reason it must not run,
 * or undefined. The headless browser's origin and file-name checks
 * (headlessBrowser.ts `browserInputProblem`, #349) are one.
 */
export type InputGuard = (toolName: string, input: unknown) => string | undefined

export type ToolDecision =
  | { decision: 'allow'; tier: RiskTier }
  | { decision: 'needs_approval'; tier: RiskTier; reason: string }
  | { decision: 'deny'; tier: RiskTier; reason: string }

export function decide(toolName: string, tierOf: TierResolver, input?: unknown, guard?: InputGuard): ToolDecision {
  const tier = tierOf(toolName, input) ?? 'outward'
  const refused = guard?.(toolName, input)
  if (refused !== undefined) return { decision: 'deny', tier, reason: refused }
  if (tier === 'read' || tier === 'write') return { decision: 'allow', tier }
  return {
    decision: 'needs_approval',
    tier,
    reason: `${toolName} is an outward action and needs a human approval in the ScadBuddy UI.`,
  }
}

/** The deny message for a call that is not allowed and has no gate to wait on. */
export function noGateMessage(reason: string): string {
  return (
    `${reason} This run has no approval surface (approvals belong to a session, #258), so it ` +
    'was not run. Tell the user what you wanted to do instead of retrying.'
  )
}

/** One outward call waiting for a human (#258). */
export type ApprovalRequest = {
  toolName: string
  /** The input exactly as the SDK passed it; an approval binds to its hash. */
  input: Record<string, unknown>
  /** The tool_use block's id: the panel's `tool.call` id. */
  toolUseId: string
  tier: RiskTier
  /** Aborted when the SDK drops the request (the query stops). */
  signal: AbortSignal
}

/** Which approval a verdict came from, for the audit log (#258); absent when none was recorded. */
export type VerdictSource = {
  approvalId?: string
  /** The approval's decision; `undefined` when it was never decided (the turn stopped). */
  decision?: 'approved' | 'denied' | 'expired' | 'cancelled' | undefined
}

export type ApprovalVerdict =
  /** Run the tool with exactly this input: the one that was approved. */
  | ({ approved: true; input: Record<string, unknown> } & VerdictSource)
  /** Refused; `message` is what the model reads as the tool's error result. */
  | ({ approved: false; message: string } & VerdictSource)

/**
 * Parks an outward call until it is decided. It may take as long as it needs
 * (see the header); a rejection counts as a denial.
 */
export type ApprovalGate = (request: ApprovalRequest) => Promise<ApprovalVerdict>

/** Observes each decision (session events, audit log, tests). Must not throw. */
export type DecisionListener = (toolName: string, decision: ToolDecision) => void

function describeError(err: unknown): string {
  return err instanceof Error ? err.message : String(err)
}

export function makeCanUseTool(
  tierOf: TierResolver,
  onDecision?: DecisionListener,
  gate?: ApprovalGate,
  guard?: InputGuard,
): CanUseTool {
  return async (toolName, input, options): Promise<PermissionResult> => {
    const decision = decide(toolName, tierOf, input, guard)
    onDecision?.(toolName, decision)
    // The SDK passes the input to the tool from `updatedInput` when set; an
    // allowed call gets its own input back unchanged.
    if (decision.decision === 'allow') return { behavior: 'allow', updatedInput: input }
    if (decision.decision === 'deny') return { behavior: 'deny', message: decision.reason }
    if (!gate) return { behavior: 'deny', message: noGateMessage(decision.reason) }
    try {
      const verdict = await gate({ toolName, input, toolUseId: options.toolUseID, tier: decision.tier, signal: options.signal })
      // The approved input, not a later copy: the approval binds to it.
      return verdict.approved
        ? { behavior: 'allow', updatedInput: verdict.input }
        : { behavior: 'deny', message: verdict.message }
    } catch (err) {
      return {
        behavior: 'deny',
        message: `${decision.reason} The approval could not complete (${describeError(err)}), so it was not run.`,
      }
    }
  }
}

export function makePreToolUseHook(
  tierOf: TierResolver,
  onDecision?: DecisionListener,
  gate?: ApprovalGate,
  guard?: InputGuard,
): HookCallbackMatcher {
  return {
    hooks: [
      (input) => {
        if (input.hook_event_name !== 'PreToolUse') return Promise.resolve({})
        const decision = decide(input.tool_name, tierOf, input.tool_input, guard)
        if (decision.decision === 'allow') return Promise.resolve({})
        if (decision.decision === 'needs_approval' && gate) {
          // Force the prompt; canUseTool parks the call and reports the decision.
          return Promise.resolve({
            hookSpecificOutput: {
              hookEventName: 'PreToolUse' as const,
              permissionDecision: 'ask' as const,
              permissionDecisionReason: decision.reason,
            },
          })
        }
        onDecision?.(input.tool_name, decision)
        return Promise.resolve({
          hookSpecificOutput: {
            hookEventName: 'PreToolUse' as const,
            permissionDecision: 'deny' as const,
            permissionDecisionReason:
              decision.decision === 'deny' ? decision.reason : noGateMessage(decision.reason),
          },
        })
      },
    ],
  }
}
