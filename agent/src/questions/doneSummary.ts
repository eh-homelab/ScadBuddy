import type { TransactionSql } from 'postgres'
import { redact } from '../secrets.js'
import { cutBetweenCodePoints } from '../sessions/touched.js'

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
// "Away" is a set of windows, one per attention request of the turn that timed
// out: each starts when its request was asked and ends at the first reply the
// user gave afterwards in the same turn (an answered question or attention
// request, or an approval they decided in the panel; a grant holder's decision
// is not the user coming back). A touch inside any window is unattended, so a
// second timeout after a reply opens a second window rather than being filed as
// "After you replied". With no timed-out request the turn's changes are one list.

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
// otherwise forge a section or a line of this record. The card renders this with
// the panel's own Markdown (frontend agent/chat/Markdown.tsx), which has no
// backslash escapes, so a name is either a code span or plain words with every
// inline-syntax character dropped; escaping would show as stray backslashes and
// not stop a link.
/** One line, no control characters. */
function flat(text: string): string {
  // eslint-disable-next-line no-control-regex
  return text.replace(/[\u0000-\u001f\u007f\u2028\u2029]+/g, ' ')
}

/**
 * Plain words: ScadBuddy's own vocabulary (an action, a resource type, an id).
 * Flattened to one line nothing can start a block, and anything but letters,
 * digits, spaces and `-.,:'` becomes a space, so no inline syntax survives.
 */
function plain(text: string): string {
  return flat(text)
    .replace(/[^\p{L}\p{N} \-.,:']+/gu, ' ')
    .replace(/ {2,}/g, ' ')
    .trim()
}

/**
 * Inside a code span: no backtick can close it early. An empty or blank name
 * would make an empty span, which the renderer pairs with the next backtick on
 * the line, so it reads as plain `(unnamed)` instead.
 */
function code(text: string): string {
  const inner = flat(text).replace(/`/g, "'")
  return inner.trim() === '' ? '(unnamed)' : `\`${inner}\``
}

/** `mcp__scadbuddy__apply_patch` reads as `apply_patch`. */
function toolName(tool: string): string {
  return code(tool.replace(/^mcp__.+?__/, ''))
}

function line(t: DoneTouch): string {
  if (t.resourceType === 'unclassified' || t.resourceId === null) {
    return `- ${toolName(t.tool)}: a change ScadBuddy does not classify`
  }
  const id = t.resourceId.length > 40 ? `${cutBetweenCodePoints(t.resourceId, SHORT_ID)}…` : t.resourceId
  const of = t.model && !(t.resourceType === 'model' && t.model === t.resourceId) ? ` of ${code(t.model)}` : ''
  return `- ${plain(t.action)} ${plain(t.resourceType.replace(/_/g, ' '))} ${code(id)}${of} (${toolName(t.tool)})`
}

function section(title: string, touches: readonly DoneTouch[]): string {
  if (touches.length === 0) return `**${title}**\n- nothing`
  const shown = touches.slice(0, SECTION_MAX).map(line)
  const more = touches.length - shown.length
  if (more > 0) shown.push(`- …and ${more} more: see the session's resources.`)
  return [`**${title}**`, ...shown].join('\n')
}

/** The touch was made inside one of the away windows: while nobody answered. */
export function inAway(t: DoneTouch, away: readonly AwayWindow[]): boolean {
  return away.some((w) => t.at >= w.from && (w.until === null || t.at < w.until))
}

/** Each raw name with the turn's secrets redacted: before an id is shortened or Markdown escaped, either of which hides a secret from `redact`. */
function redacted(t: DoneTouch, secrets: readonly string[]): DoneTouch {
  const r = (text: string) => redact(text, secrets)
  return {
    ...t,
    tool: r(t.tool),
    resourceType: r(t.resourceType),
    resourceId: t.resourceId === null ? null : r(t.resourceId),
    action: r(t.action),
    model: t.model === null ? null : r(t.model),
  }
}

/**
 * The summary as the card shows it (Markdown). `touches` in the order they
 * happened, `away` in the order asked, `secrets` the turn's (redacted from
 * every name before it is formatted).
 */
export function doneSummary(
  rawTouches: readonly DoneTouch[],
  away: readonly AwayWindow[],
  secrets: readonly string[] = [],
): string {
  const touches = rawTouches.map((t) => redacted(t, secrets))
  if (touches.length === 0) return 'ScadBuddy recorded nothing created, changed or deleted in this turn.'
  const first = away[0]
  if (!first) return section('What this turn changed', touches)
  const unattended = (t: DoneTouch) => inAway(t, away)
  const before = touches.filter((t) => t.at < first.from)
  const during = touches.filter(unattended)
  const after = touches.filter((t) => t.at >= first.from && !unattended(t))
  const ids = away.map((w) => plain(w.requestId.slice(0, 8))).join(', ')
  const parts = [
    section(`While nobody answered (attention request${away.length > 1 ? 's' : ''} ${ids} timed out)`, during),
    ...(after.length ? [section('After you replied', after)] : []),
    ...(before.length ? [section('Before you were asked', before)] : []),
  ]
  return parts.join('\n\n')
}

/**
 * The summary of turn `turnId` of `sessionId`, which started at `since` by the
 * database's clock (the one `ai_session_resources.at` is stamped by): read in
 * the transaction that posts the done request, so it covers every touch
 * committed before it. A turn resumed to re-run an approved call (the
 * approval's `resume_turn_id`, sessions/manager.ts resumeApproved) carries on
 * the turn that parked on it, so that turn's away windows count too, and of
 * the touches before `since` the ones inside them: what it did while nobody
 * answered is not lost because it ended waiting for the approval, and what it
 * did while the user was there stays its own. `unattended`: some touch was
 * made while nobody answered (the first section lists something), which keeps
 * the summary from being replaced by a later turn's (questions/service.ts).
 * `secrets`: the turn's, redacted from every name before it is formatted.
 */
export async function loadDoneSummary(
  tx: TransactionSql,
  sessionId: string,
  turnId: string,
  since: Date,
  secrets: readonly string[],
): Promise<{ summary: string; unattended: boolean }> {
  // Each window closes at the user's first reply in that request's own turn: an
  // answer there, or a browser decision on an approval that turn parked on.
  // Another turn's approval is not a reply to this request.
  const away = await tx<{ id: string; away_from: Date; away_until: Date | null }[]>`
    WITH RECURSIVE turns (id) AS (
      SELECT ${turnId}::uuid
      UNION
      SELECT a.turn_id FROM ai_approvals a JOIN turns t ON a.resume_turn_id = t.id
      WHERE a.session_id = ${sessionId} AND a.turn_id IS NOT NULL
    )
    SELECT q.id, q.created_at AS away_from, reply.at AS away_until FROM ai_questions q
    CROSS JOIN LATERAL (
      SELECT min(at) AS at FROM (
        SELECT resolved_at AS at FROM ai_questions
        WHERE session_id = ${sessionId} AND turn_id = q.turn_id AND outcome = 'answered' AND resolved_at > q.created_at
        UNION ALL
        SELECT decided_at FROM ai_approvals
        WHERE session_id = ${sessionId} AND turn_id = q.turn_id
          AND decision IN ('approved', 'denied') AND decided_by_kind = 'browser' AND decided_at > q.created_at
      ) replies
    ) reply
    WHERE q.session_id = ${sessionId} AND q.turn_id IN (SELECT id FROM turns) AND q.kind = 'attention' AND q.outcome = 'timed_out'
    ORDER BY q.created_at, q.id`
  const windows = away.map((w) => ({ requestId: w.id, from: w.away_from, until: w.away_until }))
  const from = windows.reduce((earliest, w) => (w.from < earliest ? w.from : earliest), since)
  const rows = await tx<
    { at: Date; tool: string; resource_type: string; resource_id: string | null; action: string; model_slug: string | null }[]
  >`
    SELECT at, tool, resource_type, resource_id, action, model_slug FROM ai_session_resources
    WHERE session_id = ${sessionId} AND at >= ${from}
    ORDER BY id`
  // Before `since` the touches are the parked turns', and only their unattended
  // ones carry over: attended work before the approval is not this turn's to
  // report, nor sectioned by how its windows happen to fall.
  const touches = rows
    .map((r) => ({
      at: r.at,
      tool: r.tool,
      resourceType: r.resource_type,
      resourceId: r.resource_id,
      action: r.action,
      model: r.model_slug,
    }))
    .filter((t) => t.at >= since || inAway(t, windows))
  return { summary: doneSummary(touches, windows, secrets), unattended: touches.some((t) => inAway(t, windows)) }
}
