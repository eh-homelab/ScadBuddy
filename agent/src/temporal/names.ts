// Names shared by the agent service's Temporal code and its workflow bundle, which
// may import nothing that touches Node (spec 2026-10-01 §4.3, #1055).

/** The agent service's task queue: every tool as an activity, and the agent's commands. */
export const TASK_QUEUE = 'agent-tools'
