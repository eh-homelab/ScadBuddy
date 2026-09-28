import type { SDKMessage } from '@anthropic-ai/claude-agent-sdk'
import type { TierResolver } from '../harness/permissions.js'
import { event, type ServerEvent } from './protocol.js'

// Maps the Agent SDK's message stream to panel-protocol events (#300), per the
// table in frontend/src/agent/chat/protocol.ts (#340). The query runs with
// `includePartialMessages: true`, so for each API response the SDK yields
// `stream_event` messages wrapping the raw streaming events AND the complete
// `assistant` message. Order measured against the pinned SDK (0.3.283) with
// test/support/fakeAnthropic.ts, one text reply:
//
//   system/init, system/status, stream_event/message_start,
//   stream_event/content_block_start, stream_event/content_block_delta,
//   assistant, stream_event/content_block_stop, stream_event/message_delta,
//   stream_event/message_stop, result/success
//
// so the complete `assistant` message arrives BEFORE its block's
// content_block_stop. Text therefore comes from the deltas only; a complete
// message's text is used only when no stream event was seen for that API
// message (a query without partial messages), as one delta plus done.
//
//   stream_event content_block_delta/text_delta → assistant.text.delta
//   stream_event content_block_stop of a text block → assistant.text.done
//   assistant tool_use block                      → tool.call (risk from the tier resolver;
//                                                   unknown tools are outward, spec §8.1)
//   user tool_result block                        → tool.result
//
// `system/init` is not mapped: every resumed query emits one, while
// `session.started` is emitted once, when the session is created (manager.ts).
// `result` is handled by the manager, which owns the session's running totals.
// Messages of subagents (`parent_tool_use_id` set) are skipped: with
// `tools: []` there is no Task tool to spawn one.

/** The longest tool.result summary; the full result stays in the transcript. */
export const SUMMARY_MAX = 500

type Block = { type: string; [k: string]: unknown }

function blocks(content: unknown): Block[] {
  return Array.isArray(content) ? (content as Block[]).filter((b) => typeof b?.type === 'string') : []
}

function summarise(content: unknown): string {
  let text: string
  if (typeof content === 'string') text = content
  else
    text = blocks(content)
      .map((b) => (b.type === 'text' && typeof b.text === 'string' ? b.text : `[${b.type}]`))
      .join('\n')
  return text.length > SUMMARY_MAX ? `${text.slice(0, SUMMARY_MAX - 1)}…` : text
}

export class SdkEventMapper {
  private readonly sessionId: string
  private readonly tierOf: TierResolver
  /** API message id of the response being streamed. */
  private streaming: string | undefined
  /** Index → messageId of the text blocks open in that response. */
  private readonly openText = new Map<number, string>()
  private readonly streamedMessages = new Set<string>()
  private readonly calls = new Set<string>()

  constructor(sessionId: string, tierOf: TierResolver) {
    this.sessionId = sessionId
    this.tierOf = tierOf
  }

  map(message: SDKMessage): ServerEvent[] {
    const sessionId = this.sessionId
    switch (message.type) {
      case 'stream_event': {
        if (message.parent_tool_use_id !== null) return []
        const e = message.event
        switch (e.type) {
          case 'message_start':
            this.streaming = e.message.id
            this.streamedMessages.add(e.message.id)
            this.openText.clear()
            return []
          case 'content_block_start':
            if (e.content_block.type === 'text' && this.streaming !== undefined) {
              this.openText.set(e.index, `${this.streaming}:${e.index}`)
            }
            return []
          case 'content_block_delta': {
            const messageId = this.openText.get(e.index)
            if (messageId === undefined || e.delta.type !== 'text_delta' || e.delta.text === '') return []
            return [event({ type: 'assistant.text.delta', sessionId, messageId, delta: e.delta.text })]
          }
          case 'content_block_stop': {
            const messageId = this.openText.get(e.index)
            if (messageId === undefined) return []
            this.openText.delete(e.index)
            return [event({ type: 'assistant.text.done', sessionId, messageId })]
          }
          default:
            return []
        }
      }
      case 'assistant': {
        if (message.parent_tool_use_id !== null) return []
        const out: ServerEvent[] = []
        const apiId = message.message.id
        const streamed = this.streamedMessages.has(apiId)
        blocks(message.message.content).forEach((block, index) => {
          if (block.type === 'text' && !streamed && typeof block.text === 'string' && block.text !== '') {
            const messageId = `${apiId}:${index}`
            out.push(event({ type: 'assistant.text.delta', sessionId, messageId, delta: block.text }))
            out.push(event({ type: 'assistant.text.done', sessionId, messageId }))
          } else if (block.type === 'tool_use' && typeof block.id === 'string' && !this.calls.has(block.id)) {
            this.calls.add(block.id)
            const name = typeof block.name === 'string' ? block.name : 'unknown'
            const input =
              typeof block.input === 'object' && block.input !== null && !Array.isArray(block.input)
                ? (block.input as Record<string, unknown>)
                : {}
            out.push(
              event({ type: 'tool.call', sessionId, id: block.id, name, input, risk: this.tierOf(name) ?? 'outward' }),
            )
          }
        })
        return out
      }
      case 'user': {
        // SDKUserMessageReplay (`isReplay: true`, sdk.d.ts) echoes history, which
        // was already emitted when it happened.
        if (message.parent_tool_use_id !== null || ('isReplay' in message && message.isReplay)) return []
        return blocks(message.message.content)
          .filter((b) => b.type === 'tool_result' && typeof b.tool_use_id === 'string')
          .map((b) =>
            event({
              type: 'tool.result',
              sessionId,
              id: b.tool_use_id as string,
              ok: b.is_error !== true,
              summary: summarise(b.content),
            }),
          )
      }
      default:
        return []
    }
  }
}
