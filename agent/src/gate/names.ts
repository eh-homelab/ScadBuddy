// The DurableSession workflow's gate handlers (spec 2026-10-01 §6.6), which this
// service's client sends and agent-durable's workflow (phase 5c) registers. The
// shared vectors file holds the same names, and both suites compare them to it.

export const PENDING_INPUT_QUERY = 'pending_input'
export const RESPOND_UPDATE = 'respond'
export const CANCEL_INPUT_UPDATE = 'cancel_input'
export const INTERRUPT_SIGNAL = 'interrupt'
/** The session was marked done: its workflow completes once no turn runs (#1056). */
export const END_SIGNAL = 'end'
/**
 * A validator's refusal reaches the client as an ApplicationFailure of type
 * `GateRefused:<code>` (validate.ts RefusalCode), which the route maps to its status.
 */
export const GATE_REFUSED = 'GateRefused'
