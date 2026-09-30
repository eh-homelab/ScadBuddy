import { createServer, type IncomingHttpHeaders, type ServerResponse } from 'node:http'
import type { AddressInfo } from 'node:net'

// A local stand-in for Hindsight's REST API (memory/hindsight.ts), so tests
// never reach a real Hindsight. It serves the two calls the hooks make:
//
//   POST /v1/default/banks/<bank>/memories/recall
//   POST /v1/default/banks/<bank>/memories
//
// and records every request. `respond` decides each answer; by default recall
// returns `memories` and retain accepts.

export type HindsightRequest = {
  method: string
  path: string
  headers: IncomingHttpHeaders
  body: unknown
}

export type Answer =
  | { status?: number; json?: unknown; text?: string; delayMs?: number; headers?: Record<string, string> }
  /** Never answer, until the server closes. */
  | { hang: true }

export type FakeHindsight = {
  port: number
  requests: HindsightRequest[]
  recalls(): HindsightRequest[]
  retains(): HindsightRequest[]
  memories: string[]
  respond: (request: HindsightRequest) => Answer | undefined
  close(): Promise<void>
}

export const RECALL_PATH = (bank: string) => `/v1/default/banks/${bank}/memories/recall`
export const RETAIN_PATH = (bank: string) => `/v1/default/banks/${bank}/memories`

export async function startFakeHindsight(): Promise<FakeHindsight> {
  const requests: HindsightRequest[] = []
  const fake: FakeHindsight = {
    port: 0,
    requests,
    recalls: () => requests.filter((r) => r.path.endsWith('/memories/recall')),
    retains: () => requests.filter((r) => r.path.endsWith('/memories')),
    memories: [],
    respond: () => undefined,
    close: () => Promise.resolve(),
  }
  const send = (res: ServerResponse, answer: Exclude<Answer, { hang: true }>) => {
    const body = answer.text ?? JSON.stringify(answer.json ?? {})
    res.writeHead(answer.status ?? 200, { 'content-type': 'application/json', ...answer.headers })
    res.end(body)
  }
  const server = createServer((req, res) => {
    const chunks: Buffer[] = []
    req.on('data', (c: Buffer) => chunks.push(c))
    req.on('end', () => {
      const text = Buffer.concat(chunks).toString('utf8')
      let body: unknown
      try {
        body = text ? JSON.parse(text) : undefined
      } catch {
        body = text
      }
      const request: HindsightRequest = { method: req.method ?? '', path: req.url ?? '', headers: req.headers, body }
      requests.push(request)
      const answer =
        fake.respond(request) ??
        (request.path.endsWith('/memories/recall')
          ? { json: { results: fake.memories.map((m) => ({ text: m, type: 'world' })) } }
          : { json: { success: true, operation_id: `op-${requests.length}` } })
      if ('hang' in answer) return
      if (answer.delayMs) setTimeout(() => send(res, answer), answer.delayMs)
      else send(res, answer)
    })
  })
  await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', () => resolve()))
  fake.port = (server.address() as AddressInfo).port
  fake.close = () =>
    new Promise((resolve) => {
      server.closeAllConnections()
      server.close(() => resolve())
    })
  return fake
}
