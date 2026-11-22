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
// is not the user coming back, and neither is one made over /mcp by a token:
// the same holds for the approval a turn is resumed for, so a resume a token
// approved leaves the parked turn's window open and the resumed turn's work is
// unattended too, #1416). A touch inside any window is unattended, so a
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
  /**
   * Whether it was made inside an away window, when the database said so at its
   * microseconds (the API's `doneFacts`, gate/crudQuestions.ts, #3235); otherwise `inAway` decides from `at`.
   */
  unattended?: boolean
  /** Whether it was made before the first away window opened, likewise. */
  beforeAsked?: boolean
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

/** How a kind reads where its name alone would mislead: whose library a file is in (#4507). */
const KIND_WORDS: Readonly<Record<string, string>> = {
  bambuddy_file: 'Bambuddy file',
  library_file: "ScadBuddy's library file",
}

function line(t: DoneTouch): string {
  if (t.resourceType === 'unclassified' || t.resourceId === null) {
    return `- ${toolName(t.tool)}: a change ScadBuddy does not classify`
  }
  const id = t.resourceId.length > 40 ? `${cutBetweenCodePoints(t.resourceId, SHORT_ID)}…` : t.resourceId
  const of = t.model && !(t.resourceType === 'model' && t.model === t.resourceId) ? ` of ${code(t.model)}` : ''
  return `- ${plain(t.action)} ${plain(KIND_WORDS[t.resourceType] ?? t.resourceType.replace(/_/g, ' '))} ${code(id)}${of} (${toolName(t.tool)})`
}

function section(title: string, touches: readonly DoneTouch[]): string {
  if (touches.length === 0) return `**${title}**\n- nothing`
  const shown = touches.slice(0, SECTION_MAX).map(line)
  const more = touches.length - shown.length
  if (more > 0) shown.push(`- …and ${more} more: see the session's resources.`)
  return [`**${title}**`, ...shown].join('\n')
}

/** The touch was made inside one of the away windows: while nobody answered. */
export function inAway(t: Pick<DoneTouch, 'at'>, away: readonly AwayWindow[]): boolean {
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
  const unattended = (t: DoneTouch) => t.unattended ?? inAway(t, away)
  const asked = (t: DoneTouch) => !(t.beforeAsked ?? t.at < first.from)
  const before = touches.filter((t) => !asked(t))
  const during = touches.filter(unattended)
  const after = touches.filter((t) => asked(t) && !unattended(t))
  const ids = away.map((w) => plain(w.requestId.slice(0, 8))).join(', ')
  const parts = [
    section(`While nobody answered (attention request${away.length > 1 ? 's' : ''} ${ids} timed out)`, during),
    ...(after.length ? [section('After you replied', after)] : []),
    ...(before.length ? [section('Before you were asked', before)] : []),
  ]
  return parts.join('\n\n')
}
