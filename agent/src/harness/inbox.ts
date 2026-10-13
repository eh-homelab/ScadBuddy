import type { SDKUserMessage } from '@anthropic-ai/claude-agent-sdk'

// A streaming-input prompt (spec 2026-10-13 §5.3): one query, many turns. push() wakes
// the iterator; close() ends it, which closes Claude Code's stdin and so the process.
export class Inbox implements AsyncIterable<SDKUserMessage> {
  private readonly queue: SDKUserMessage[] = []
  private wake: (() => void) | undefined
  private closed = false
  push(message: SDKUserMessage): void {
    if (this.closed) throw new Error('inbox closed')
    this.queue.push(message)
    this.wake?.()
  }
  close(): void {
    this.closed = true
    this.wake?.()
  }
  async *[Symbol.asyncIterator](): AsyncIterator<SDKUserMessage> {
    for (;;) {
      while (this.queue.length > 0) yield this.queue.shift() as SDKUserMessage
      if (this.closed) return
      await new Promise<void>((resolve) => (this.wake = resolve))
    }
  }
}

export function userMessage(content: SDKUserMessage['message']['content']): SDKUserMessage {
  return { type: 'user', message: { role: 'user', content }, parent_tool_use_id: null, session_id: '' }
}
