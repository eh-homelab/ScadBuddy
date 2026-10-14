// Names of the session workflow (spec 2026-10-01 §6, unified runtime phase 4), shared by
// the workflow, the agent service's client and its worker. The gate's own handlers are
// named in src/gate/names.ts. Free of Node imports: the workflow bundle loads it.

export const SESSION_WORKFLOW = 'AgentSession'
export const SESSION_QUEUE = 'agent-sessions'
export const SESSION_TURN_ACTIVITY = 'session_turn'
export const OPEN_INPUT_UPDATE = 'open_input'
export const OWNER_CHANGED_SIGNAL = 'owner_changed'

/**
 * The gate's activities (temporal/session/gate.ts). Under the agent's own `gate.` prefix, never
 * agent-durable's names (`open_input`, `gate_settings` ...): Temporal routes by queue plus name,
 * so a shared name would let one half's worker be sent the other's payload shape.
 * `gate.describe_call` and `turn_tool_tier` are the agent-tools worker's own.
 */
export const SESSION_GATE_ACTIVITIES = {
  settings: 'gate.settings',
  describe: 'gate.describe_call',
  turnToolTier: 'turn_tool_tier',
  grantCheck: 'gate.flow_grant_check',
  open: 'gate.open_input',
  grant: 'gate.grant_input',
  deny: 'gate.deny_input',
  resolve: 'gate.resolve_input',
} as const
