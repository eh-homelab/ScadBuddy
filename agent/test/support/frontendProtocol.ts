import path from 'node:path'
import { fileURLToPath } from 'node:url'

// The panel's protocol module (frontend/src/agent/chat/protocol.ts, #340),
// loaded as it is, so the agent's events are checked against the schema the
// panel actually parses with. The path is computed so tsc does not pull the
// frontend package into the agent's program; vitest.config.ts points its zod
// import at agent's copy.

type ParseResult = { ok: true; value: unknown } | { ok: false; error: string }

const here = path.dirname(fileURLToPath(import.meta.url))
const modulePath = path.resolve(here, '../../../frontend/src/agent/chat/protocol.ts')

export async function frontendParseServerEvent(): Promise<(raw: unknown) => ParseResult> {
  const mod = (await import(modulePath)) as { parseServerEvent: (raw: unknown) => ParseResult }
  return mod.parseServerEvent
}

/** The panel's own client-message builder and parser, to feed what it sends into the agent. */
export async function frontendClientMessages(): Promise<{
  clientMessage: (body: Record<string, unknown> & { type: string }) => Record<string, unknown>
  parseClientMessage: (raw: unknown) => ParseResult
}> {
  return (await import(modulePath)) as Awaited<ReturnType<typeof frontendClientMessages>>
}

export type PanelChatState = {
  sessions: Record<string, { status: string; owner: { kind: string }; items: Record<string, unknown>[] }>
  activeId: string | null
  awaitingStart: boolean
  notice: string | null
}

/**
 * The panel's own stream reducer (frontend/src/agent/chat/state.ts), to replay
 * what a socket delivered and assert on what the panel would show.
 */
export async function frontendChatReducer(): Promise<{
  chatReducer: (state: PanelChatState, action: Record<string, unknown>) => PanelChatState
  initialChatState: PanelChatState
}> {
  const statePath = path.resolve(here, '../../../frontend/src/agent/chat/state.ts')
  return (await import(statePath)) as Awaited<ReturnType<typeof frontendChatReducer>>
}

/** The tab's tool catalogue (frontend/src/agent/catalog.ts `TOOLS`, #254), for the browser_* parity check. */
export async function frontendBridgeCatalog(): Promise<{
  TOOLS: Record<string, { description: string; risk: 'read' | 'write' | 'outward'; scope: string; input: unknown }>
}> {
  const catalogPath = path.resolve(here, '../../../frontend/src/agent/catalog.ts')
  return (await import(catalogPath)) as Awaited<ReturnType<typeof frontendBridgeCatalog>>
}

export type FrontendBridge = {
  setRoute(route: string): void
  register(impls: Record<string, (args: never) => unknown>, options: { label: string }): () => void
  call(name: string, args?: unknown): Promise<unknown>
  liveNames(): string[]
  currentRoute(): string
  subscribe(listener: () => void): () => void
}

export type FrontendTabLink = {
  connect(): void
  close(): void
  getState(): {
    connected: boolean
    pending: { id: string; label: string; expiresAt: string }[]
    paired: { id: string; label: string; expiresAt: string }[]
    results: Record<string, { ok: boolean; message: string }>
  }
  subscribe(listener: () => void): () => void
  accept(id: string, code: string): boolean
  deny(id: string): boolean
  end(id: string): boolean
}

type CreateTabLink = (options: {
  bridge: FrontendBridge
  tabId: string
  url: string
  WebSocketImpl: unknown
  baseMs?: number
  maxMs?: number
}) => FrontendTabLink

/**
 * The tab's own bridge and its socket link (frontend/src/agent/bridge.ts and
 * link.ts, #254), to run the real tab side against the agent in Node.
 */
export async function frontendTabLink(): Promise<{
  AgentBridge: new () => FrontendBridge
  createTabLink: CreateTabLink
}> {
  const bridge = (await import(path.resolve(here, '../../../frontend/src/agent/bridge.ts'))) as {
    AgentBridge: new () => FrontendBridge
  }
  const link = (await import(path.resolve(here, '../../../frontend/src/agent/link.ts'))) as { createTabLink: CreateTabLink }
  return { AgentBridge: bridge.AgentBridge, createTabLink: link.createTabLink }
}

/** Throws, naming the event, when the panel would drop it. */
export async function expectPanelAccepts(events: readonly unknown[]): Promise<void> {
  const parse = await frontendParseServerEvent()
  for (const e of events) {
    const result = parse(e)
    if (!result.ok) throw new Error(`the panel would drop ${JSON.stringify(e)}: ${result.error}`)
  }
}
