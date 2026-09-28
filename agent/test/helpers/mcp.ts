import { Client } from '@modelcontextprotocol/sdk/client/index.js'
import { StreamableHTTPClientTransport } from '@modelcontextprotocol/sdk/client/streamableHttp.js'
import type { Hono } from 'hono'
import { createBackendClient } from '../../src/api/backend.js'
import { type AgentApp, type AppDeps, createApp } from '../../src/app.js'
import { DEFAULT_MCP_AUTH, type McpAuthSettings } from '../../src/auth/authenticate.js'
import { InMemoryTokenStore, type TokenStore } from '../../src/auth/tokens.js'
import { originPolicy } from '../../src/http/origins.js'
import type { McpEndpointDeps } from '../../src/mcp/http.js'
import { ALL_TOOLS } from '../../src/tools/index.js'
import { PendingActionStore } from '../../src/tools/pending.js'
import type { ToolServices } from '../../src/tools/registry.js'
import { MemoryCredentials } from '../support/memoryCredentials.js'

// An in-process ScadBuddy agent app and MCP SDK Streamable HTTP clients that
// talk to it without a socket: the client's `fetch` calls `app.fetch`, passing
// the env binding @hono/node-server would (`incoming.socket.remoteAddress`),
// so the loopback rule sees whatever address a test chooses. The backend is
// reached through the global fetch, which msw intercepts.

export const BACKEND = 'http://backend.test'
export const MCP_URL = 'http://scadbuddy.test/mcp'
export const LOOPBACK = '127.0.0.1'
/** The ingress controller: inside the trusted-proxy range, so its X-Forwarded-* are believed. */
export const INGRESS = '10.0.0.7'
/** Some other LAN host: its X-Forwarded-* headers are ignored. */
export const UNTRUSTED = '192.168.1.50'
/** SCADBUDDY_PUBLIC_URL and SCADBUDDY_AGENT_TRUSTED_PROXIES as the tests deploy them. */
export const ORIGINS = originPolicy('https://scadbuddy.test', '10.0.0.0/24')

export function services(overrides: Partial<ToolServices> = {}): ToolServices {
  return {
    // Created per test, after msw is listening, and calling the global fetch
    // at request time so msw's patch is the one used.
    backend: createBackendClient(BACKEND, (request) => fetch(request)),
    pending: new PendingActionStore(),
    pollIntervalMs: 5,
    renderWaitMs: 5000,
    ...overrides,
  }
}

export type TestApp = { app: AgentApp; tokens: TokenStore; settings: McpAuthSettings; services: ToolServices }

/** The non-MCP app dependencies (#255's credential routes and health), with a database that is up and migrated. */
export function baseDeps(overrides: Partial<AppDeps> = {}): AppDeps {
  return {
    database: { ping: async () => true, ready: async () => true },
    backend: async () => true,
    kek: { ok: false, reason: 'not needed by /mcp tests' },
    credentials: new MemoryCredentials(),
    testConnection: async () => ({ ok: true, detail: 'connected', duration_ms: 1, model: 'm' }),
    origins: ORIGINS,
    // What @hono/node-server's getConnInfo reads; appFetch passes the same binding.
    remoteAddress: (c) =>
      (c.env as { incoming?: { socket?: { remoteAddress?: string } } } | undefined)?.incoming?.socket?.remoteAddress,
    ...overrides,
  }
}

export function testApp(
  options: {
    settings?: Partial<McpAuthSettings>
    tokens?: TokenStore
    services?: ToolServices
    /** Replaces the settings reader, e.g. with one that throws. */
    authSettings?: () => McpAuthSettings | Promise<McpAuthSettings>
    /** Overrides for the rest of the app, e.g. a database whose migrations failed. */
    deps?: Partial<AppDeps>
    /** Overrides for the /mcp endpoint itself (session limits, sweep timing). */
    mcp?: Partial<McpEndpointDeps>
  } = {},
): TestApp {
  const settings = { ...DEFAULT_MCP_AUTH, ...options.settings }
  const tokens = options.tokens ?? new InMemoryTokenStore()
  const svc = options.services ?? services()
  const app = createApp(
    baseDeps({
      ...options.deps,
      mcp: {
        tools: ALL_TOOLS,
        services: svc,
        tokens,
        authSettings: options.authSettings ?? (() => settings),
        ...options.mcp,
      },
    }),
  )
  return { app, tokens, settings, services: svc }
}

export type Via = { address?: string; headers?: Record<string, string> }

/** A fetch into the app as if from `address`, adding `headers` to every request. */
export function appFetch(app: Hono, via: Via = {}): typeof fetch {
  return async (input, init) => {
    const request = new Request(input, init)
    // A socket request always carries Host; a constructed Request does not.
    request.headers.set('host', new URL(request.url).host)
    for (const [k, v] of Object.entries(via.headers ?? {})) request.headers.set(k, v)
    return app.fetch(request, { incoming: { socket: { remoteAddress: via.address ?? LOOPBACK } } })
  }
}

export async function connect(app: Hono, via: Via = {}): Promise<Client> {
  const client = new Client({ name: 'scadbuddy-test', version: '0.0.0' })
  const transport = new StreamableHTTPClientTransport(new URL(MCP_URL), { fetch: appFetch(app, via) })
  await client.connect(transport)
  return client
}

/** The text of a tool result's first content block, parsed as JSON when it is. */
export function firstText(result: unknown): unknown {
  const content = (result as { content?: { type: string; text?: string }[] }).content ?? []
  const text = content.find((c) => c.type === 'text')?.text ?? ''
  try {
    return JSON.parse(text)
  } catch {
    return text
  }
}
