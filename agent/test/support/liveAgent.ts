import type { AddressInfo } from 'node:net'
import { serve, upgradeWebSocket } from '@hono/node-server'
import { getConnInfo } from '@hono/node-server/conninfo'
import { WebSocket, WebSocketServer } from 'ws'
import { type AppDeps, createApp } from '../../src/app.js'
import { originPolicy } from '../../src/http/origins.js'
import type { SessionManager } from '../../src/sessions/manager.js'
import { MemoryCredentials } from './memoryCredentials.js'

// The agent's real HTTP server (createApp on @hono/node-server, with the `ws`
// WebSocket server main.ts uses) on a loopback port, for tests that drive the
// chat socket and the session routes as the browser would.

export type LiveAgent = {
  /** http://127.0.0.1:<port> */
  url: string
  /** The Origin the UI sends from: the loopback pair is allowed from a loopback peer (http/origins.ts). */
  origin: string
  close(): Promise<void>
}

export async function startLiveAgent(sessions: SessionManager, overrides: Partial<AppDeps> = {}): Promise<LiveAgent> {
  const credentials = new MemoryCredentials()
  const app = createApp({
    database: { ping: () => Promise.resolve(true), ready: () => Promise.resolve(true) },
    backend: () => Promise.resolve(true),
    kek: { ok: false, reason: 'not needed here' },
    credentials,
    testConnection: () => Promise.resolve({ ok: true, detail: 'ok', duration_ms: 0, model: 'm' }),
    remoteAddress: (c) => {
      try {
        return getConnInfo(c).remote.address
      } catch {
        return undefined
      }
    },
    origins: originPolicy(undefined, undefined),
    approvals: sessions.approvals,
    sessions,
    upgradeWebSocket,
    ...overrides,
  })
  const wss = new WebSocketServer({ noServer: true, maxPayload: 256 * 1024 })
  const server = serve({ fetch: app.fetch, hostname: '127.0.0.1', port: 0, websocket: { server: wss } })
  await new Promise<void>((resolve) => server.once('listening', () => resolve()))
  const { port } = server.address() as AddressInfo
  const url = `http://127.0.0.1:${port}`
  return {
    url,
    origin: url,
    close: async () => {
      for (const socket of wss.clients) socket.terminate()
      wss.close()
      await app.close()
      await new Promise<void>((resolve) => {
        if ('closeAllConnections' in server) server.closeAllConnections()
        server.close(() => resolve())
      })
    },
  }
}

/** A panel's end of the chat socket: every frame it received, parsed, in order. */
export type PanelSocket = {
  frames: Record<string, unknown>[]
  send(message: unknown): void
  /** Resolves with the frames up to and including the first match after `from`. */
  until(match: (frame: Record<string, unknown>) => boolean, options?: { from?: number; timeoutMs?: number }): Promise<Record<string, unknown>[]>
  close(): void
}

export async function openPanelSocket(agent: LiveAgent, origin = agent.origin): Promise<PanelSocket> {
  const socket = new WebSocket(`${agent.url.replace(/^http/, 'ws')}/api/v1/ai/chat`, { headers: { origin } })
  const frames: Record<string, unknown>[] = []
  const waiters = new Set<() => void>()
  socket.on('message', (data) => {
    frames.push(JSON.parse(String(data)) as Record<string, unknown>)
    for (const wake of waiters) wake()
  })
  await new Promise<void>((resolve, reject) => {
    socket.once('open', () => resolve())
    socket.once('unexpected-response', (_req, res) => reject(new Error(`handshake refused: ${res.statusCode}`)))
    socket.once('error', reject)
  })
  return {
    frames,
    send: (message) => socket.send(JSON.stringify(message)),
    until: (match, { from = 0, timeoutMs = 30_000 } = {}) =>
      new Promise((resolve, reject) => {
        const check = () => {
          const index = frames.findIndex((f, i) => i >= from && match(f))
          if (index < 0) return false
          waiters.delete(check)
          clearTimeout(timer)
          resolve(frames.slice(from, index + 1))
          return true
        }
        const timer = setTimeout(() => {
          waiters.delete(check)
          reject(new Error(`timed out; frames: ${frames.slice(from).map((f) => String(f.type)).join(', ')}`))
        }, timeoutMs)
        if (!check()) waiters.add(check)
      }),
    close: () => socket.close(),
  }
}
