import type { Sql } from 'postgres'
import type { AppendHook } from '../../sessions/busEvents.js'
import { EventLog } from '../../sessions/eventLog.js'
import {
  DEFAULT_APPROVAL_EXPIRY_SECONDS,
  MAX_APPROVAL_EXPIRY_SECONDS,
  MIN_APPROVAL_EXPIRY_SECONDS,
  SETTING_APPROVAL_EXPIRY_SECONDS,
} from '../../approvals/service.js'
import { denyInput, type GateDb, grantInput, openInput, readInput, resolveInput } from '../../gate/store.js'
import {
  DEFAULT_QUESTION_EXPIRY_SECONDS,
  MAX_QUESTION_EXPIRY_SECONDS,
  MIN_QUESTION_EXPIRY_SECONDS,
  SETTING_QUESTION_EXPIRY_SECONDS,
} from '../../gate/questions.js'
import type { GateSettings } from './gate.js'
import { SESSION_GATE_ACTIVITIES } from './names.js'

// The gate's writers (gate/store.ts) as activities under the `gate.` names SessionGate
// schedules (names.ts), distinct from agent-durable's, on `agent-tools` (AgentSession's
// SessionGateOptions.taskQueue); and `gate.settings`, the entries' timers, which AgentSession
// reads as each turn starts. Arguments are camelCase, as store.ts's types are.

/** A timer setting as the classic services clamp it (approvals/service.ts, gate/questions.ts); the default when unset or not a number. */
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

/**
 * The gate's GateDb as main.ts builds it: the session manager's event log, or with none (no
 * credentials) a log of its own that only publishes, so a write still reaches the bus (#3365).
 * Nothing follows that log: with no session manager there are no followers on this replica.
 */
export function gateDb(sql: Sql, events: EventLog | undefined, onAppend: AppendHook | undefined): GateDb {
  return { sql, events: events ?? new EventLog(sql, onAppend ? { onAppend } : {}) }
}

/** `db.events` is this replica's session event log, so a write wakes its followers at once (#3365). */
export function sessionGateActivities(db: GateDb) {
  return {
    [SESSION_GATE_ACTIVITIES.settings]: () => gateSettings(db.sql),
    [SESSION_GATE_ACTIVITIES.open]: (args: Parameters<typeof openInput>[1]) => openInput(db, args),
    [SESSION_GATE_ACTIVITIES.grant]: (args: Parameters<typeof grantInput>[1]) => grantInput(db, args),
    [SESSION_GATE_ACTIVITIES.deny]: (args: Parameters<typeof denyInput>[1]) => denyInput(db, args),
    [SESSION_GATE_ACTIVITIES.resolve]: (args: Parameters<typeof resolveInput>[1]) => resolveInput(db, args),
    [SESSION_GATE_ACTIVITIES.read]: (args: Parameters<typeof readInput>[1]) => readInput(db.sql, args),
  }
}
