// Names of the session workflow (spec 2026-10-01 §6, unified runtime phase 4), shared by
// the workflow, the agent service's client and its worker. The gate's own handlers are
// named in src/gate/names.ts. Free of Node imports: the workflow bundle loads it.

export const SESSION_WORKFLOW = 'AgentSession'
export const SESSION_QUEUE = 'agent-sessions'
export const SESSION_TURN_ACTIVITY = 'session_turn'
export const OPEN_INPUT_UPDATE = 'open_input'
export const OWNER_CHANGED_SIGNAL = 'owner_changed'
