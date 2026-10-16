import type { Client } from '@modelcontextprotocol/sdk/client/index.js'
import { StreamableHTTPClientTransport, StreamableHTTPError } from '@modelcontextprotocol/sdk/client/streamableHttp.js'
import { ErrorCode, McpError } from '@modelcontextprotocol/sdk/types.js'

// One MCP session per (durable session, plugin) on the replica that holds it (#2581).
//
// A classic turn's Claude Code keeps one MCP session per plugin for the whole turn, so
// a stateful plugin (Playwright MCP run `--isolated`: one browser context per MCP
// session) keeps its page from one call to the next. A durable turn's calls are
// activities, each of which used to open its own client and drop it without the
// session DELETE, so every call met a new browser and left the last one behind.
//
// Here a durable session's client on a plugin is kept in this process, keyed by the
// session, the plugin's name and its config fingerprint (URL, header, address, tool
// settings), and used for its listing and every call. The calls are pinned to this
// replica (turnTools.ts `pinned`, the queue `http_request` already uses), so they all
// reach it. A kept client is ended with the session DELETE (`terminateSession`) and
// then closed: when its session ends, when it has been idle CLIENT_IDLE_MS, when the
// plugin's config changes or the plugin is gone, when the cache is full, and when the
// process stops.

/** How long a kept client may go unused before it is ended. */
export const CLIENT_IDLE_MS = 10 * 60_000
/** The most clients kept at once on one replica; past it the least recently used idle one is ended. */
export const MAX_KEPT_CLIENTS = 64
/** How long ending one client (its DELETE) may take before it is closed regardless. */
export const CLIENT_CLOSE_MS = 5_000

/** What a kept server's `connect` gives: the client, and what to free once it is closed (its forwarder route). */
export type KeptConnection = { client: Client; release(): void }

export type KeptClient<Tools> = {
  readonly session: string
  readonly name: string
  readonly fingerprint: string
  readonly client: Client
  /** The plugin's tool list as this client last listed it. */
  tools?: Tools
}

type Entry<Tools> = KeptClient<Tools> & {
  release(): void
  /** When it was last used, by SessionClients' own clock. */
  lastUsed: number
  inUse: number
  timer?: ReturnType<typeof setTimeout>
  closed: boolean
}

/**
 * Ends a client: the session DELETE first (Streamable HTTP only; a 405 or a failure is
 * ignored, the server's own expiry then ends it), within CLIENT_CLOSE_MS, then close().
 * `client.close()` alone only aborts the transport and leaves the session on the server.
 */
export async function endClient(client: Client, timeoutMs = CLIENT_CLOSE_MS): Promise<void> {
  const transport = client.transport
  if (transport instanceof StreamableHTTPClientTransport) {
    let timer: ReturnType<typeof setTimeout> | undefined
    await Promise.race([
      transport.terminateSession().catch(() => {}),
      new Promise<void>((resolve) => {
        timer = setTimeout(resolve, timeoutMs)
        timer.unref?.()
      }),
    ])
    clearTimeout(timer)
  }
  await client.close().catch(() => {})
}

/** The server no longer knows the session (HTTP 404 on its id), or the forwarder route is gone: the request never ran. */
export function sessionGone(err: unknown): boolean {
  return err instanceof StreamableHTTPError && err.code === 404
}

const NOT_CONNECTED = new Set(['ECONNREFUSED', 'ENOTFOUND', 'EAI_AGAIN', 'EADDRNOTAVAIL'])

/**
 * The request was never sent: it failed before a connection was made, or its client was
 * already ended (evicted between the tool's lookup and its call: the SDK's "Not connected").
 */
export function neverSent(err: unknown): boolean {
  if (err instanceof Error && err.message === 'Not connected') return true
  const cause = (err as { cause?: { code?: unknown } } | null)?.cause
  return err instanceof TypeError && typeof cause?.code === 'string' && NOT_CONNECTED.has(cause.code)
}

/**
 * The client's transport broke: an HTTP failure, a fetch that failed, or the
 * connection closed under it. The session is no longer to be trusted, but the request
 * may have reached the server. A JSON-RPC error the server answered with, or a
 * request's own timeout, is none of these: the session is still good.
 */
export function transportBroken(err: unknown): boolean {
  if (err instanceof StreamableHTTPError) return true
  if (err instanceof McpError) return err.code === ErrorCode.ConnectionClosed
  return err instanceof TypeError
}

export class SessionClients<Tools = unknown> {
  private readonly entries = new Map<string, Entry<Tools>>()
  private readonly opening = new Map<string, Promise<Entry<Tools>>>()
  private readonly idleMs: number
  private readonly max: number
  /** Orders uses: the least recently used client is the one with the lowest. */
  private clock = 0

  constructor(options: { idleMs?: number; max?: number } = {}) {
    this.idleMs = options.idleMs ?? CLIENT_IDLE_MS
    this.max = options.max ?? MAX_KEPT_CLIENTS
  }

  get size(): number {
    return this.entries.size
  }

  /** The kept client for (session, plugin, fingerprint), if this replica holds one. */
  peek(session: string, name: string, fingerprint: string): KeptClient<Tools> | undefined {
    return this.entries.get(key(session, name, fingerprint))
  }

  /**
   * The kept client for (session, plugin, fingerprint), connected now if none is held.
   * A client the same session holds on the plugin under another fingerprint (its
   * config changed) is ended first.
   */
  async open(session: string, name: string, fingerprint: string, connect: () => Promise<KeptConnection>): Promise<KeptClient<Tools>> {
    const k = key(session, name, fingerprint)
    const held = this.entries.get(k)
    if (held) {
      this.touch(held)
      return held
    }
    const pending = this.opening.get(k)
    if (pending) return pending
    const opened = (async () => {
      await Promise.all(
        [...this.entries.values()].filter((e) => e.session === session && e.name === name).map((e) => this.end(e)),
      )
      const { client, release } = await connect()
      const entry: Entry<Tools> = { session, name, fingerprint, client, release, lastUsed: 0, inUse: 0, closed: false }
      this.entries.set(k, entry)
      this.touch(entry)
      void this.bound()
      return entry
    })()
    this.opening.set(k, opened)
    try {
      return await opened
    } finally {
      this.opening.delete(k)
    }
  }

  /** Runs `use` on a kept client; it is not ended for idleness while in use. */
  async use<T>(kept: KeptClient<Tools>, use: (client: Client) => Promise<T>): Promise<T> {
    const entry = kept as Entry<Tools>
    entry.inUse++
    this.touch(entry)
    try {
      return await use(entry.client)
    } finally {
      entry.inUse--
      this.touch(entry)
    }
  }

  /** Ends a kept client (a broken transport, an unknown session). */
  drop(kept: KeptClient<Tools>): Promise<void> {
    return this.end(kept as Entry<Tools>)
  }

  /** Ends every client a session holds: the session ended. */
  async closeSession(session: string): Promise<void> {
    await Promise.all([...this.entries.values()].filter((e) => e.session === session).map((e) => this.end(e)))
  }

  /** Ends the session's clients on a plugin it no longer offers, or offers with another config. */
  async retain(session: string, current: ReadonlyMap<string, string>): Promise<void> {
    await Promise.all(
      [...this.entries.values()]
        .filter((e) => e.session === session && current.get(e.name) !== e.fingerprint)
        .map((e) => this.end(e)),
    )
  }

  /** Ends every client: the process stops. */
  async closeAll(): Promise<void> {
    await Promise.all([...this.entries.values()].map((e) => this.end(e)))
  }

  private touch(entry: Entry<Tools>): void {
    entry.lastUsed = ++this.clock
    if (entry.timer) clearTimeout(entry.timer)
    if (entry.closed) return
    entry.timer = setTimeout(() => {
      // In use (a call longer than the idle time): looked at again once it is done.
      if (entry.inUse === 0) void this.end(entry)
    }, this.idleMs)
    entry.timer.unref?.()
  }

  private async bound(): Promise<void> {
    while (this.entries.size > this.max) {
      const idle = [...this.entries.values()].filter((e) => e.inUse === 0).sort((a, b) => a.lastUsed - b.lastUsed)
      const oldest = idle[0]
      // Every one in use: over the bound until one is done.
      if (!oldest) return
      await this.end(oldest)
    }
  }

  private async end(entry: Entry<Tools>): Promise<void> {
    if (entry.closed) return
    entry.closed = true
    if (entry.timer) clearTimeout(entry.timer)
    const k = key(entry.session, entry.name, entry.fingerprint)
    if (this.entries.get(k) === entry) this.entries.delete(k)
    try {
      await endClient(entry.client)
    } finally {
      entry.release()
    }
  }
}

function key(session: string, name: string, fingerprint: string): string {
  return `${session}\0${name}\0${fingerprint}`
}
