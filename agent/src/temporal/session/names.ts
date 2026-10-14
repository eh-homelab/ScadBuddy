// Names of the session workflow (spec 2026-10-01 §6, unified runtime phase 4), shared by
// the workflow, the agent service's client and its worker. The gate's own handlers are
// named in src/gate/names.ts. Free of Node imports: the workflow bundle loads it.

export const SESSION_WORKFLOW = 'AgentSession'
export const SESSION_QUEUE = 'agent-sessions'
/** The Worker Deployment of the `agent-sessions` worker (D4). */
export const SESSION_DEPLOYMENT = 'scadbuddy-agent'
export const SESSION_TURN_ACTIVITY = 'session_turn'
/**
 * Session affinity (spec 2026-10-13 §5.2): `acquire_worker` on the shared `agent-tools` reserves
 * a warm slot on whichever replica answers and returns that replica's session host queue;
 * `release_worker` frees it there.
 */
export const ACQUIRE_WORKER_ACTIVITY = 'acquire_worker'
export const RELEASE_WORKER_ACTIVITY = 'release_worker'
/**
 * A replica's own queue for `session_turn` and `release_worker`: `agent-sessions.<identity>`, the
 * identity behind its `agent-tools.<identity>` (main.ts HOST_QUEUE). A queue of its own, not that
 * one: `agent-tools.<identity>` carries DurableSession's pinned calls (#2320, #2581), so a shared
 * activity limit would either starve them or let them make a live home look dead to a turn's
 * 5 s schedule-to-start.
 */
export const sessionHostQueue = (identity: string): string => `${SESSION_QUEUE}.${identity}`
/** `acquire_worker`'s refusal when every warm slot on its replica runs a turn: retryable, so another replica may answer. */
export const NO_CAPACITY = 'NoCapacity'
/** `session_turn`'s failure when its worker stops under it: retryable, the turn resumes on another replica. */
export const SHUTTING_DOWN_FAILURE = 'ShuttingDown'
/** `session_turn`'s failure when the session no longer runs this turn (it ended, or was forgotten). */
export const SESSION_GONE = 'SessionGone'
export const OPEN_INPUT_UPDATE = 'open_input'
export const OWNER_CHANGED_SIGNAL = 'owner_changed'
export const SEND_MESSAGE_UPDATE = 'send_message'
export const TURN_QUERY = 'turn'
/** A local activity on the `agent-sessions` worker: the runtime settings, read as each turn starts. */
export const SESSION_SETTINGS_ACTIVITY = 'session_settings'
/** The turn's end (events and status), on `agent-tools`; agent-durable's is `finish_turn`. */
export const SESSION_FINISH_ACTIVITY = 'session.finish_turn'
/** `ai_settings` key of the flag that starts new durable sessions on AgentSession; read, never written through a route, until phase 6. */
export const SESSION_RUNTIME_SETTING = 'session_runtime'

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
  open: 'gate.open_input',
  grant: 'gate.grant_input',
  deny: 'gate.deny_input',
  resolve: 'gate.resolve_input',
} as const

/**
 * CALLED by the gate, never registered by TS: the backend projects worker's activity on the
 * `projects` queue (spec 2026-10-11 §3.4), so it keeps its own bare name.
 */
export const FLOW_GRANT_CHECK_ACTIVITY = 'flow_grant_check'
