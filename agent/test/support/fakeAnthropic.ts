import { createServer, type IncomingHttpHeaders, type Server } from 'node:http'
import type { AddressInfo } from 'node:net'

// A local Anthropic-format endpoint (Messages API) for tests, so CI never calls
// Anthropic (spec §4.4, §13). The SDK is pointed at it the way a gateway is:
// ANTHROPIC_BASE_URL + a credential ("the same seam the gateway docs describe",
// issue #255). Per https://code.claude.com/docs/en/llm-gateway-protocol, an
// Anthropic Messages gateway serves `/v1/messages` (inference posts to
// `/v1/messages?beta=true`), optionally `/v1/messages/count_tokens`, and may be
// asked for `GET /v1/models` at start-up (model discovery).
//
// Replies are scripted: `reply(request)` picks what the "model" says for each
// /v1/messages call, as text or as one tool_use block. Streaming requests get
// the Messages streaming SSE event sequence (message_start, content_block_*,
// message_delta, message_stop); others get a JSON message.

export type Reply =
  | { text: string }
  | { toolUse: { name: string; input: Record<string, unknown> } }
  /** An API error response, e.g. 401 authentication_error. */
  | { error: { status: number; type: string; message: string; headers?: Record<string, string> } }
  /** Never answer (until the server closes): a model that is still thinking. */
  | { hang: true }
  /**
   * Start a streamed text reply, send `text`, then never finish it: a model cut
   * off mid-reply (#991). `usage` is message_start's usage (10 input tokens by
   * default); no message_delta, so the output's usage is never reported.
   */
  | { stall: string; usage?: { input_tokens: number; output_tokens?: number } }

type ContentReply = Exclude<Reply, { error: unknown } | { hang: true } | { stall: string }>

export type RecordedRequest = {
  method: string
  path: string
  headers: IncomingHttpHeaders
  body: MessagesBody | undefined
}

export type MessagesBody = {
  model?: string
  stream?: boolean
  tools?: { name: string }[]
  messages?: { role: string; content: unknown }[]
  system?: unknown
  max_tokens?: number
}

export type FakeAnthropic = {
  url: string
  requests: RecordedRequest[]
  /** /v1/messages calls only. */
  messageCalls(): RecordedRequest[]
  close(): Promise<void>
}

let ids = 0
const nextId = (prefix: string) => `${prefix}_${(++ids).toString().padStart(6, '0')}`

function sse(event: string, data: unknown): string {
  return `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`
}

function contentOf(reply: ContentReply): { block: Record<string, unknown>; stopReason: string } {
  if ('text' in reply) return { block: { type: 'text', text: reply.text }, stopReason: 'end_turn' }
  return {
    block: { type: 'tool_use', id: nextId('toolu'), name: reply.toolUse.name, input: reply.toolUse.input },
    stopReason: 'tool_use',
  }
}

function stalledStart(model: string, reply: Extract<Reply, { stall: string }>): string {
  return [
    sse('message_start', {
      type: 'message_start',
      message: {
        id: nextId('msg'),
        type: 'message',
        role: 'assistant',
        model,
        content: [],
        stop_reason: null,
        stop_sequence: null,
        usage: { output_tokens: 1, cache_creation_input_tokens: 0, cache_read_input_tokens: 0, input_tokens: 10, ...reply.usage },
      },
    }),
    sse('content_block_start', { type: 'content_block_start', index: 0, content_block: { type: 'text', text: '' } }),
    sse('content_block_delta', { type: 'content_block_delta', index: 0, delta: { type: 'text_delta', text: reply.stall } }),
  ].join('')
}

function streamEvents(model: string, reply: ContentReply): string {
  const { block, stopReason } = contentOf(reply)
  const usage = { input_tokens: 10, output_tokens: 1, cache_creation_input_tokens: 0, cache_read_input_tokens: 0 }
  const start =
    block.type === 'text' ? { type: 'text', text: '' } : { type: 'tool_use', id: block.id, name: block.name, input: {} }
  const delta =
    block.type === 'text'
      ? { type: 'text_delta', text: block.text }
      : { type: 'input_json_delta', partial_json: JSON.stringify(block.input) }
  return [
    sse('message_start', {
      type: 'message_start',
      message: {
        id: nextId('msg'),
        type: 'message',
        role: 'assistant',
        model,
        content: [],
        stop_reason: null,
        stop_sequence: null,
        usage,
      },
    }),
    sse('content_block_start', { type: 'content_block_start', index: 0, content_block: start }),
    sse('content_block_delta', { type: 'content_block_delta', index: 0, delta }),
    sse('content_block_stop', { type: 'content_block_stop', index: 0 }),
    sse('message_delta', {
      type: 'message_delta',
      delta: { stop_reason: stopReason, stop_sequence: null },
      usage: { output_tokens: 5 },
    }),
    sse('message_stop', { type: 'message_stop' }),
  ].join('')
}

export async function startFakeAnthropic(reply: (request: RecordedRequest) => Reply): Promise<FakeAnthropic> {
  const requests: RecordedRequest[] = []
  const server: Server = createServer((req, res) => {
    const chunks: Buffer[] = []
    req.on('data', (chunk: Buffer) => chunks.push(chunk))
    req.on('end', () => {
      const path = (req.url ?? '/').split('?')[0] ?? '/'
      const raw = Buffer.concat(chunks).toString('utf8')
      let body: MessagesBody | undefined
      try {
        body = raw ? (JSON.parse(raw) as MessagesBody) : undefined
      } catch {
        body = undefined
      }
      const recorded: RecordedRequest = { method: req.method ?? 'GET', path, headers: req.headers, body }
      requests.push(recorded)

      if (req.method === 'GET' && path === '/v1/models') {
        res.writeHead(200, { 'content-type': 'application/json' })
        res.end(JSON.stringify({ data: [], has_more: false, first_id: null, last_id: null }))
        return
      }
      if (req.method === 'POST' && path === '/v1/messages/count_tokens') {
        res.writeHead(200, { 'content-type': 'application/json' })
        res.end(JSON.stringify({ input_tokens: 10 }))
        return
      }
      if (req.method === 'POST' && path === '/v1/messages') {
        const model = body?.model ?? 'claude-fake'
        const answer = reply(recorded)
        if ('hang' in answer) return
        if ('stall' in answer) {
          res.writeHead(200, { 'content-type': 'text/event-stream', 'cache-control': 'no-cache' })
          res.write(stalledStart(model, answer))
          return
        }
        if ('error' in answer) {
          res.writeHead(answer.error.status, { ...answer.error.headers, 'content-type': 'application/json' })
          res.end(JSON.stringify({ type: 'error', error: { type: answer.error.type, message: answer.error.message } }))
          return
        }
        if (body?.stream) {
          res.writeHead(200, { 'content-type': 'text/event-stream', 'cache-control': 'no-cache' })
          res.end(streamEvents(model, answer))
        } else {
          const { block, stopReason } = contentOf(answer)
          res.writeHead(200, { 'content-type': 'application/json' })
          res.end(
            JSON.stringify({
              id: nextId('msg'),
              type: 'message',
              role: 'assistant',
              model,
              content: [block],
              stop_reason: stopReason,
              stop_sequence: null,
              usage: { input_tokens: 10, output_tokens: 5 },
            }),
          )
        }
        return
      }
      res.writeHead(404, { 'content-type': 'application/json' })
      res.end(JSON.stringify({ type: 'error', error: { type: 'not_found_error', message: path } }))
    })
  })
  await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve))
  const { port } = server.address() as AddressInfo
  return {
    url: `http://127.0.0.1:${port}`,
    requests,
    messageCalls: () => requests.filter((r) => r.method === 'POST' && r.path === '/v1/messages'),
    close: () =>
      new Promise<void>((resolve) => {
        server.closeAllConnections()
        server.close(() => resolve())
      }),
  }
}
