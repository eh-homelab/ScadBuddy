import type { TransactionSql } from 'postgres'
import { doneSummary } from '../../../src/gate/doneSummary.js'
import type { DoneFacts } from '../../../src/gate/crudQuestions.js'
import { WAIT_FOR_USER } from '../../../src/gate/entry.js'

// Tests only: the done summary's facts read over the test database, the SQL the agent ran
// before the API took area `questions` (#5299 step 12). The API's is backend
// tools/go/core/internal/questions.

/**
 * The summary of turn `turnId` of `sessionId`, which started at `since` by the
 * database's clock (the one `ai_session_resources.at` is stamped by): read in
 * the transaction that posts the done request, so it covers every touch
 * committed before it. A turn resumed to re-run an approved call (the
 * approval's `resume_turn_id`, written by an earlier release's resumeApproved) carries on
 * the turn that parked on it, so that turn's away windows count too, and of
 * the touches before `since` the ones inside them: what it did while nobody
 * answered is not lost because it ended waiting for the approval, and what it
 * did while the user was there stays its own. `unattended`: some touch was
 * made while nobody answered (the first section lists something), which keeps
 * the summary from being replaced by a later turn's (gate/questions.ts).
 * `secrets`: the turn's, redacted from every name before it is formatted.
 */
export async function loadDoneSummary(
  tx: TransactionSql,
  sessionId: string,
  turnId: string,
  since: string,
  secrets: readonly string[],
): Promise<{ summary: string; unattended: boolean }> {
  const { touches, windows } = await loadDoneFacts(tx, sessionId, turnId, since)
  return { summary: doneSummary(touches, windows, secrets), unattended: touches.some((t) => t.unattended) }
}

/** The facts `loadDoneSummary` writes its summary from: the API's `doneFacts`. */
export async function loadDoneFacts(tx: TransactionSql, sessionId: string, turnId: string, since: string): Promise<DoneFacts> {
  // Each window closes at the user's first reply in that request's own turn: an
  // answer there, or a browser decision on an approval that turn parked on.
  // Another turn's approval is not a reply to this request.
  // An AgentSession turn's requests and replies are the gate's (#5003): its responses since the
  // turn started (a session runs one turn at a time, and parks nothing past it), each shown by
  // its tool_use id's tail, since every one starts `toolu_01`.
  const away = await tx<{ id: string; away_from: Date; away_until: Date | null; from_text: string; until_text: string | null }[]>`
    WITH RECURSIVE turns (id) AS (
      SELECT ${turnId}::uuid
      UNION
      SELECT a.turn_id FROM ai_approvals a JOIN turns t ON a.resume_turn_id = t.id
      WHERE a.session_id = ${sessionId} AND a.turn_id IS NOT NULL
    )
    SELECT id, away_from, away_until, away_from::text AS from_text, away_until::text AS until_text FROM (
    SELECT q.id::text AS id, q.created_at AS away_from, reply.at AS away_until
    FROM ai_questions q
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
    UNION ALL
    SELECT right(substring(r.request_id FROM '[^:]*$'), 8), r.requested_at, reply.at
    FROM ai_input_responses r
    CROSS JOIN LATERAL (
      SELECT min(created_at) AS at FROM ai_input_responses
      WHERE session_id = ${sessionId} AND requested_at >= ${since}::text::timestamptz AND created_at > r.requested_at
        AND (outcome = 'answered' OR (kind = 'approval' AND outcome IN ('approved', 'denied') AND responder->>'kind' = 'browser'))
    ) reply
    WHERE r.session_id = ${sessionId} AND r.tool = ${WAIT_FOR_USER} AND r.outcome = 'timed_out'
      AND r.requested_at >= ${since}::text::timestamptz
    ) windows
    ORDER BY away_from, id`
  const windows = away.map((w) => ({ requestId: w.id, from: w.away_from, until: w.away_until }))
  // `since` and each window's edges are compared in SQL, at Postgres's microseconds:
  // as a JS Date each is rounded down to the millisecond, which lets in a touch made
  // in the last fraction of a millisecond before the claim (#1383) or a window's
  // start, or leaves out one just before its end (#3235). Cast through text:
  // postgres.js serializes a timestamptz parameter through a Date too.
  const bounds = away.map((w) => ({ f: w.from_text, u: w.until_text }))
  const earliest = windows.reduce<Date | null>((e, w) => (e === null || w.from < e ? w.from : e), null)
  const rows = await tx<
    {
      at: Date
      in_turn: boolean
      in_away: boolean
      before_asked: boolean
      tool: string
      resource_type: string
      resource_id: string | null
      action: string
      model_slug: string | null
    }[]
  >`
    SELECT at, at >= ${since}::text::timestamptz AS in_turn,
           EXISTS (SELECT 1 FROM jsonb_to_recordset(${tx.json(bounds)}) AS w(f timestamptz, u timestamptz)
                   WHERE at >= w.f AND (w.u IS NULL OR at < w.u)) AS in_away,
           -- bounds[0] is the earliest window: the query above orders them by when each was asked.
           coalesce(at < ${bounds[0]?.f ?? null}::text::timestamptz, false) AS before_asked,
           tool, resource_type, resource_id, action, model_slug
    FROM ai_session_resources
    WHERE session_id = ${sessionId} AND (at >= ${since}::text::timestamptz OR at >= ${earliest ?? null}::timestamptz)
    ORDER BY id`
  // Before `since` the touches are the parked turns', and only their unattended
  // ones carry over: attended work before the approval is not this turn's to
  // report, nor sectioned by how its windows happen to fall.
  const touches = rows
    .filter((r) => r.in_turn || r.in_away)
    .map((r) => ({
      at: r.at,
      tool: r.tool,
      resourceType: r.resource_type,
      resourceId: r.resource_id,
      action: r.action,
      model: r.model_slug,
      unattended: r.in_away,
      beforeAsked: r.before_asked,
    }))
  return { touches, windows }
}
