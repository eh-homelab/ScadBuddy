import type { Sql } from 'postgres'
import {
  DEFAULT_APPROVAL_EXPIRY_SECONDS,
  MAX_APPROVAL_EXPIRY_SECONDS,
  MIN_APPROVAL_EXPIRY_SECONDS,
  SETTING_APPROVAL_EXPIRY_SECONDS,
} from '../../approvals/service.js'
import { denyInput, grantInput, openInput, resolveInput } from '../../gate/store.js'
import {
  DEFAULT_QUESTION_EXPIRY_SECONDS,
  MAX_QUESTION_EXPIRY_SECONDS,
  MIN_QUESTION_EXPIRY_SECONDS,
  SETTING_QUESTION_EXPIRY_SECONDS,
} from '../../questions/service.js'
import type { GateSettings } from './gate.js'
import { SESSION_GATE_ACTIVITIES } from './names.js'

// The gate's writers (gate/store.ts) as activities under the `gate.` names SessionGate
// schedules (names.ts), distinct from agent-durable's, on `agent-tools` (AgentSession's
// SessionGateOptions.taskQueue); and `gate.settings`, the entries' timers, which AgentSession
// reads as each turn starts. Arguments are camelCase, as store.ts's types are.

/** A timer setting as the classic services clamp it (approvals/service.ts, questions/service.ts); the default when unset or not a number. */
function seconds(value: unknown, fallback: number, min: number, max: number): number {
  if (typeof value !== 'number' || !Number.isFinite(value)) return fallback
  return Math.min(Math.max(Math.round(value), min), max)
}

/**
 * agent-durable's `gate_settings` (session/activities.py) without its tool overrides: an
 * AgentSession's calls are tiered in its turn (the classic seam), so the gate needs only the
 * timers.
 */
export async function gateSettings(sql: Sql): Promise<GateSettings> {
  const rows = await sql<{ key: string; value: unknown }[]>`
    SELECT key, value FROM ai_settings WHERE key IN (${SETTING_APPROVAL_EXPIRY_SECONDS}, ${SETTING_QUESTION_EXPIRY_SECONDS})`
  const value = (key: string) => rows.find((r) => r.key === key)?.value
  return {
    approvalExpiryS: seconds(value(SETTING_APPROVAL_EXPIRY_SECONDS), DEFAULT_APPROVAL_EXPIRY_SECONDS, MIN_APPROVAL_EXPIRY_SECONDS, MAX_APPROVAL_EXPIRY_SECONDS),
    questionExpiryS: seconds(value(SETTING_QUESTION_EXPIRY_SECONDS), DEFAULT_QUESTION_EXPIRY_SECONDS, MIN_QUESTION_EXPIRY_SECONDS, MAX_QUESTION_EXPIRY_SECONDS),
  }
}

export function sessionGateActivities(sql: Sql) {
  return {
    [SESSION_GATE_ACTIVITIES.settings]: () => gateSettings(sql),
    [SESSION_GATE_ACTIVITIES.open]: (args: Parameters<typeof openInput>[1]) => openInput(sql, args),
    [SESSION_GATE_ACTIVITIES.grant]: (args: Parameters<typeof grantInput>[1]) => grantInput(sql, args),
    [SESSION_GATE_ACTIVITIES.deny]: (args: Parameters<typeof denyInput>[1]) => denyInput(sql, args),
    [SESSION_GATE_ACTIVITIES.resolve]: (args: Parameters<typeof resolveInput>[1]) => resolveInput(sql, args),
  }
}
