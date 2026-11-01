import type { Sql } from 'postgres'
import type { AuditSink } from '../audit/log.js'
import { safeDetail } from '../audit/log.js'
import type { MemoryActivity } from '../memory/hindsight.js'
import { UNTRUSTED_CONTENT_POLICY } from '../safety/untrusted.js'
import { redact } from '../secrets.js'
import type { EventLog } from './eventLog.js'
import { event, type Owner } from './protocol.js'
import { scrubForLog } from './sdkEvents.js'

// What the session manager and the turn runner both use, in a module of its own so
// turnRunner.ts imports nothing from manager.ts, which imports it (#3452).

/** ai_settings key of the model a turn runs on (non-secret, spec §9). */
export const SETTING_MODEL = 'model'

/** The session whose budget this one spends from: itself, or a fork's lineage root (#823). */
const ROOT = 'coalesce(ai_sessions.budget_root_id, ai_sessions.id)'
/** The budget a session spends from: its root's (20261006T0100Z_session_budget_root.sql). */
export const POOL_BUDGET = `coalesce((SELECT r.budget_usd FROM ai_sessions r WHERE r.id = ${ROOT}), ai_sessions.budget_usd)`
/** What every session spending from that budget has spent. Read at the statement's snapshot. */
export const POOL_COST = `(SELECT sum(m.cost_usd) FROM ai_sessions m WHERE coalesce(m.budget_root_id, m.id) = ${ROOT})`

/** What is left of the budget `sessionId` spends from (POOL_BUDGET less POOL_COST); undefined for a session that is gone. */
export async function poolRemainingUsd(sql: Sql, sessionId: string): Promise<number | undefined> {
  const [row] = await sql.unsafe<{ left_usd: string | null }[]>(
    `SELECT ${POOL_BUDGET} - coalesce(${POOL_COST}, 0) AS left_usd FROM ai_sessions WHERE id = $1`,
    [sessionId],
  )
  if (!row) return undefined
  return row.left_usd === null ? 0 : Number(row.left_usd)
}

/** The most of one memory, or of a memory event's input, a panel event carries. */
export const MEMORY_TEXT_MAX = 4000
/**
 * A memory event's query or memory text, redacted and then capped. Redaction
 * must come first: a secret the cut splits would no longer match, and its
 * fragment would reach the log.
 */
export function memoryText(text: string, secrets: readonly (string | undefined)[]): string {
  const clean = redact(text, secrets)
  return clean.length > MEMORY_TEXT_MAX ? `${clean.slice(0, MEMORY_TEXT_MAX)}… (${clean.length} characters)` : clean
}

/**
 * One automatic recall or retain as the session records it: an audit row (never
 * fails, audit/log.ts) and a `memory` panel event, both redacted
 * (`SessionManager.memoryHooks`).
 */
export async function recordMemoryActivity(
  deps: { audit?: AuditSink | undefined; events: Pick<EventLog, 'append'> },
  session: { id: string; owner: Owner },
  turnId: string,
  a: MemoryActivity,
  secrets: readonly string[],
): Promise<void> {
  const id = session.id
  const summary = {
    bank: a.bank,
    ...(a.documentId ? { document_id: a.documentId } : {}),
    ...(a.count === undefined ? {} : { results: a.count }),
    ...(a.tool ? { tool: a.tool } : {}),
  }
  const detail = a.reason ? { detail: safeDetail(a.reason, secrets) } : {}
  await Promise.all([
    deps.audit?.record({
      kind: 'memory',
      action: a.action,
      surface: 'harness',
      actor: session.owner,
      sessionId: id,
      turnId,
      ...(a.toolUseId ? { toolUseId: a.toolUseId } : {}),
      inputSummary: JSON.stringify(summary),
      outcome: a.outcome === 'ok' ? 'ok' : 'error',
      ...detail,
      startedAt: a.startedAt,
      finishedAt: a.finishedAt,
    }),
    deps.events.append(id, [
      scrubForLog(
        event({
          type: 'memory',
          sessionId: id,
          turnId,
          action: a.action,
          bank: a.bank,
          outcome: a.outcome,
          ...(a.count === undefined ? {} : { count: a.count }),
          ...detail,
          ...(a.input === undefined ? {} : { input: memoryText(a.input, secrets) }),
          ...(a.memories === undefined ? {} : { memories: a.memories.map((m) => memoryText(m, secrets)) }),
        }),
        secrets,
      ),
    ]),
  ])
}

/**
 * Which browser a `browser_*` tool drives, for the session's system prompt. ScadBuddy's
 * bridge tools and a browser plugin's (Microsoft's Playwright MCP, added in Settings →
 * Plugins) share bare names (`browser_snapshot`, `browser_click`), and a model that has
 * seen one set calls the other's names on the wrong server: in production it called
 * `mcp__scadbuddy__browser_find`, which only Playwright's set has.
 */
export const BROWSER_TOOLS_GUIDE = [
  '<browser_tools>',
  "The mcp__scadbuddy__browser_* tools drive the user's own open ScadBuddy tab, which they watch. Use only the names " +
    'that server lists; it has no browser_find, browser_type or browser_select_option. To find something on the ' +
    'page, call mcp__scadbuddy__browser_snapshot; to set a field or a select, mcp__scadbuddy__browser_fill (a ' +
    "select takes an option's value or its label as the snapshot shows it).",
  "browser_* tools of any other server (a plugin's, such as mcp__playwright__browser_*) drive a separate browser " +
    "that the user cannot see and that never shows the user's tab. Use them only when the user has no tab attached " +
    'or asks for that browser.',
  '</browser_tools>',
].join('\n')

/**
 * What every turn appends to Claude Code's own system prompt: the data/instruction
 * boundary (#258, safety/untrusted.ts: only the user's messages are instructions, tool
 * results are data), then which browser each browser_* tool drives.
 */
export function systemAppend(): string {
  return `${UNTRUSTED_CONTENT_POLICY}\n\n${BROWSER_TOOLS_GUIDE}`
}
