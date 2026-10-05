import type { TransactionSql } from 'postgres'

// The record ScadBuddy adds to a `done` attention request (#815 §4,
// harness/attention.ts): what the turn created, changed or deleted, read from
// `ai_session_resources` (#931, sessions/touched.ts), never from the model.
// The agent's message says what it meant to do; this says what it did.
//
// Split around the user's absence, as #815's follow-up asks: the actions taken
// while an attention request went unanswered come first, so the user checks
// the unattended decisions before the rest. A timeout never approves anything
// (an outward call still parks for its own approval), and the list makes that
// checkable: an outward write in the first section would be visible there.
//
// "Away" starts at the turn's first attention request that timed out, and ends
// at the first reply the user gave afterwards in the same turn (an answered
// question or attention request, or an approval they decided in the panel; a
// grant holder's decision is not the user coming back). With no timed-out
// request the turn's changes are one list.

/** One recorded touch, as the summary reads it. */
export type DoneTouch = {
  at: Date
  tool: string
  resourceType: string
  resourceId: string | null
  action: string
  model: string | null
}

/** The span in which nobody answered: from the timed-out request until the user's next reply, if any. */
export type AwayWindow = { requestId: string; from: Date; until: Date | null }

/** Lines one section shows before it says how many more there are. */
export const SECTION_MAX = 20

const SHORT_ID = 12

// Every name below came from a tool's input or result (a preset's name, a model's
// slug), so none may shape the Markdown: a newline or a backtick in one could
// otherwise forge a section or a line of this record.
/** One line, no control characters. */
function flat(text: string): string {
  // eslint-disable-next-line no-control-regex
  return text.replace(/[\u0000-\u001f\u007f\u2028\u2029]+/g, ' ')
}

/**
 * Plain text in Markdown. Flattened to one line nothing can start a block, so
 * only inline syntax is escaped: emphasis, code, links, HTML, strikethrough and
 * tables. An underscore inside a word (`save_preset`) is left alone, as
 * CommonMark never reads one as emphasis.
 */
function plain(text: string): string {
  return flat(text).replace(/[\\`*[\]<>&~|]|(?<![A-Za-z0-9])_|_(?![A-Za-z0-9])/g, (c) => `\\${c}`)
}

/** Inside a code span: no backtick can close it early. */
function code(text: string): string {
  return `\`${flat(text).replace(/`/g, "'")}\``
}

/** `mcp__scadbuddy__apply_patch` reads as `apply_patch`. */
function toolName(tool: string): string {
  return plain(tool.replace(/^mcp__.+?__/, ''))
}

function line(t: DoneTouch): string {
  if (t.resourceType === 'unclassified' || t.resourceId === null) {
    return `- ${toolName(t.tool)}: a change ScadBuddy does not classify`
  }
  const id = t.resourceId.length > 40 ? `${t.resourceId.slice(0, SHORT_ID)}…` : t.resourceId
  const of = t.model && !(t.resourceType === 'model' && t.model === t.resourceId) ? ` of ${plain(t.model)}` : ''
  return `- ${plain(t.action)} ${plain(t.resourceType.replace(/_/g, ' '))} ${code(id)}${of} (${toolName(t.tool)})`
}

function section(title: string, touches: readonly DoneTouch[]): string {
  if (touches.length === 0) return `**${title}**\n- nothing`
  const shown = touches.slice(0, SECTION_MAX).map(line)
  const more = touches.length - shown.length
  if (more > 0) shown.push(`- …and ${more} more: see the session's resources.`)
  return [`**${title}**`, ...shown].join('\n')
}

/** The summary as the card shows it (Markdown). `touches` in the order they happened. */
export function doneSummary(touches: readonly DoneTouch[], away: AwayWindow | null): string {
  if (touches.length === 0) return 'ScadBuddy recorded nothing created, changed or deleted in this turn.'
  if (!away) return section('What this turn changed', touches)
  const before = touches.filter((t) => t.at < away.from)
  const during = touches.filter((t) => t.at >= away.from && (away.until === null || t.at < away.until))
  const until = away.until
  const after = until === null ? [] : touches.filter((t) => t.at >= until)
  const parts = [
    section(`While nobody answered (attention request ${plain(away.requestId.slice(0, 8))} timed out)`, during),
    ...(after.length ? [section('After you replied', after)] : []),
    ...(before.length ? [section('Before you were asked', before)] : []),
  ]
  return parts.join('\n\n')
}

/**
 * The summary of turn `turnId` of `sessionId`, which started at `since`: read in
 * the transaction that posts the done request, so it covers every touch
 * committed before it.
 */
export async function loadDoneSummary(tx: TransactionSql, sessionId: string, turnId: string, since: Date): Promise<string> {
  const rows = await tx<
    { at: Date; tool: string; resource_type: string; resource_id: string | null; action: string; model_slug: string | null }[]
  >`
    SELECT at, tool, resource_type, resource_id, action, model_slug FROM ai_session_resources
    WHERE session_id = ${sessionId} AND at >= ${since}
    ORDER BY id`
  const [timedOut] = await tx<{ id: string; created_at: Date }[]>`
    SELECT id, created_at FROM ai_questions
    WHERE session_id = ${sessionId} AND turn_id = ${turnId} AND kind = 'attention' AND outcome = 'timed_out'
    ORDER BY created_at LIMIT 1`
  let away: AwayWindow | null = null
  if (timedOut) {
    const [reply] = await tx<{ at: Date }[]>`
      SELECT min(at) AS at FROM (
        SELECT resolved_at AS at FROM ai_questions
        WHERE session_id = ${sessionId} AND turn_id = ${turnId} AND outcome = 'answered' AND resolved_at > ${timedOut.created_at}
        UNION ALL
        SELECT decided_at FROM ai_approvals
        WHERE session_id = ${sessionId} AND decision IN ('approved', 'denied') AND decided_by_kind = 'browser'
          AND decided_at > ${timedOut.created_at}
      ) replies`
    away = { requestId: timedOut.id, from: timedOut.created_at, until: reply?.at ?? null }
  }
  return doneSummary(
    rows.map((r) => ({
      at: r.at,
      tool: r.tool,
      resourceType: r.resource_type,
      resourceId: r.resource_id,
      action: r.action,
      model: r.model_slug,
    })),
    away,
  )
}
