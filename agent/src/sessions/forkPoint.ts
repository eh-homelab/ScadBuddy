import type { SessionStoreEntry } from '@anthropic-ai/claude-agent-sdk'
import type { EventLog } from './eventLog.js'
import type { ServerEvent } from './protocol.js'

// Fork from a message (#793, docs/superpowers/specs/2026-10-09-session-switcher-design.md §1):
// `up_to` is the panel's assistant message id, `<API message id>:<block index>`
// (sessions/sdkEvents.ts). Claude Code writes each content block of a reply as a
// transcript entry carrying that API `message.id`, so the id maps onto the entry whose
// `uuid` the SDK's `forkSession({upToMessageId})` slices at (inclusive).

/** The longest `up_to` a fork takes: an API message id and a block index, with room to spare. */
export const MESSAGE_ID_MAX = 300

/** Splits `<API message id>:<block index>`; undefined for anything else. */
export function parseMessageId(messageId: string): { apiId: string; index: number } | undefined {
  const at = messageId.lastIndexOf(':')
  if (at <= 0) return undefined
  const index = Number(messageId.slice(at + 1))
  if (!Number.isInteger(index) || index < 0 || messageId.slice(at + 1) === '') return undefined
  return { apiId: messageId.slice(0, at), index }
}

type Block = { type?: unknown }

function blocksOf(entry: SessionStoreEntry): Block[] {
  const message = entry.message as { content?: unknown } | undefined
  return Array.isArray(message?.content) ? (message.content as Block[]) : []
}

const hasText = (entry: SessionStoreEntry) => blocksOf(entry).some((b) => b.type === 'text')

/**
 * The uuid of the main-transcript entry holding the text block the panel's
 * `messageId` names: the entry at the block's index among the reply's entries
 * when that one is text, else the reply's last text entry. Undefined when the
 * reply is not in the transcript (or only a subagent's sidechain has it).
 */
export function transcriptCut(entries: readonly SessionStoreEntry[], messageId: string): string | undefined {
  const parsed = parseMessageId(messageId)
  if (!parsed) return undefined
  const reply = entries.filter(
    (e) =>
      e.type === 'assistant' &&
      e.isSidechain !== true &&
      typeof e.uuid === 'string' &&
      (e.message as { id?: unknown } | undefined)?.id === parsed.apiId,
  )
  const exact = reply[parsed.index]
  if (exact && hasText(exact)) return exact.uuid
  return reply.filter(hasText).at(-1)?.uuid
}

/** The conversation events a fork copies; lifecycle events (status, owner, result) are the parent's own. */
const CONVERSATION = new Set<ServerEvent['type']>([
  'user.turn',
  'assistant.text.delta',
  'assistant.text.done',
  'tool.call',
  'tool.result',
])

/**
 * The parent's conversation events, for the child: all of them, or with `upTo` those
 * through that reply's `assistant.text.done`. Undefined when `upTo` names no finished
 * reply in the log.
 */
export async function forkHistory(events: Pick<EventLog, 'read'>, id: string, upTo?: string): Promise<ServerEvent[] | undefined> {
  const history: ServerEvent[] = []
  for (let after = 0; ; ) {
    const page = await events.read(id, after)
    if (page.length === 0) return upTo === undefined ? history : undefined
    after = page.at(-1)?.seq ?? after
    for (const { event: e } of page) {
      if (!CONVERSATION.has(e.type)) continue
      history.push(e)
      if (upTo !== undefined && e.type === 'assistant.text.done' && e.messageId === upTo) return history
    }
  }
}

/**
 * The blobs (blobs.ts) the events name: a tool result's images, and the images a user sent
 * with a turn and their previews (#2479). What a fork copies into the child (#2150), and nothing else.
 */
export function blobNames(events: readonly ServerEvent[]): string[] {
  const names = new Set<string>()
  for (const e of events) {
    if (e.type === 'tool.result') for (const image of e.images ?? []) names.add(image.name)
    if (e.type === 'user.turn') {
      for (const image of e.images ?? []) {
        if (image.name !== undefined) names.add(image.name)
        names.add(image.preview.name)
      }
    }
  }
  return [...names]
}

// A durable session's fork (#2688). Its conversation is the transcript its workflow holds
// (DurableSession's `fork_point` Query), and its panel replies are `durable-<offset>`: the
// live `text` event they come from names no transcript entry (agent-durable
// session/events.py). Each such event is one text block of a main-chain assistant entry,
// in order, so the replies are matched to the entries by their text.

/** The longest text a durable `text` event carries, in code points (the plugin's FIELD_LIMIT). */
export const DURABLE_TEXT_MAX = 32 * 1024

/** Entry types the engine resumes from (the plugin's `_TRANSCRIPT_TYPES`). */
const TRANSCRIPT_TYPES = new Set(['user', 'assistant', 'attachment', 'system'])

const isTranscript = (e: SessionStoreEntry): boolean =>
  typeof e.uuid === 'string' && TRANSCRIPT_TYPES.has(e.type) && e.isSidechain !== true

function sameText(entry: string, event: string): boolean {
  if (entry === event) return true
  // A text past the cap reaches the log cut to its first DURABLE_TEXT_MAX code points.
  return [...event].length === DURABLE_TEXT_MAX && entry.startsWith(event)
}

/**
 * The parent's finished replies, by panel message id, each mapped to the uuid of the
 * transcript entry holding its text, in the order they were written. A reply whose text
 * the transcript does not hold next (an attempt that never committed: a retried
 * segment's, a stopped turn's) is left out. When the next entry is not it, a later entry
 * is taken only if no other remaining entry has the same text, so a reply is never
 * matched to an entry another reply could be.
 */
export function durableReplies(entries: readonly SessionStoreEntry[], events: readonly ServerEvent[]): Map<string, string> {
  const texts: { uuid: string; text: string }[] = []
  for (const e of entries) {
    if (e.type !== 'assistant' || !isTranscript(e)) continue
    for (const block of blocksOf(e) as { type?: unknown; text?: unknown }[]) {
      if (block.type === 'text' && typeof block.text === 'string' && block.text.trim()) texts.push({ uuid: e.uuid!, text: block.text })
    }
  }
  const replies = new Map<string, string>()
  const written = new Map<string, string>()
  let next = 0
  for (const e of events) {
    if (e.type === 'assistant.text.delta') written.set(e.messageId, (written.get(e.messageId) ?? '') + e.delta)
    if (e.type !== 'assistant.text.done') continue
    const text = written.get(e.messageId) ?? ''
    const rest = texts.slice(next)
    let at = rest[0] && sameText(rest[0].text, text) ? 0 : -1
    if (at < 0) {
      const matches = rest.flatMap((t, i) => (sameText(t.text, text) ? [i] : []))
      if (matches.length === 1) at = matches[0]!
    }
    if (at < 0) continue
    replies.set(e.messageId, rest[at]!.uuid)
    next += at + 1
  }
  return replies
}

/**
 * The entries through `uuid` (inclusive): a reply's entry, or the workflow's checkpoint,
 * which names whatever entry its runner committed last. Undefined when no main-chain
 * entry has it.
 */
export function entriesThrough(entries: readonly SessionStoreEntry[], uuid: string): SessionStoreEntry[] | undefined {
  const at = entries.findIndex((e) => e.uuid === uuid && e.isSidechain !== true)
  return at < 0 ? undefined : entries.slice(0, at + 1)
}
