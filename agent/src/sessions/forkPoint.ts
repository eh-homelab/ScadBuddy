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
