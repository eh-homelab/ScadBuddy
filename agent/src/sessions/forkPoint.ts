import type { SessionStoreEntry } from '@anthropic-ai/claude-agent-sdk'
import type { EventLog } from './eventLog.js'
import type { ServerEvent } from './protocol.js'

// Fork from a message (#793, docs/superpowers/specs/2026-10-09-session-switcher-design.md §1):
// `up_to` is the panel's assistant message id, `<API message id>:<block index>`
// (sessions/sdkEvents.ts). Claude Code writes each content block of a reply as a
// transcript entry carrying that API `message.id`, so the id maps onto the entry a fork's
// cut takes last (manager.ts `fork`), as the SDK's `forkSession({upToMessageId})` slices
// at that entry's `uuid` (inclusive).

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

/**
 * Where a fork cuts the parent's log (eventLog.ts `inheritThrough`): the seq of that reply's
 * `assistant.text.done` with `upTo`, else the log's last. Undefined when `upTo` names no finished
 * reply in the log.
 */
export async function forkEventCut(events: Pick<EventLog, 'read' | 'lastSeq'>, id: string, upTo?: string): Promise<number | undefined> {
  if (upTo === undefined) return events.lastSeq(id)
  for (let after = 0; ; ) {
    const page = await events.read(id, after)
    if (page.length === 0) return undefined
    after = page.at(-1)?.seq ?? after
    for (const { seq, event: e } of page) {
      if (e.type === 'assistant.text.done' && e.messageId === upTo) return seq
    }
  }
}

/**
 * The blobs (blobs.ts) the events name: a tool result's images, and the images a user sent
 * with a turn and their previews (#2479). What forgetting a parent copies into each fork, from
 * the events it copies (store.ts `copyForkRanges`, #2150, #4212), and nothing else.
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
