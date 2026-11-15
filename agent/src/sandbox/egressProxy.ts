import { createServer, type IncomingMessage, type Server } from 'node:http'
import { BlockList, connect as netConnect, isIP, type Socket } from 'node:net'
import type { Duplex } from 'node:stream'
import { assertHostAllowed, embeddedIPv4, isLoopbackHost, isPrivateAddress, type Resolver, systemResolver } from '../http/egress.js'
import { plainAddress } from '../http/origins.js'

// The sandbox pods' one way out (#5864, issue comment "Design" §4): an HTTP CONNECT proxy in its
// own pod. Its NetworkPolicy admits only the sandbox pods and lets it reach only public addresses
// on 80 and 443; this code checks each target the way WebFetch does (http/egress.ts
// `assertHostAllowed`: link-local and cloud metadata refused; loopback refused) and also refuses
// every private and shared address, because a command can run any code. It connects to the address
// it checked, never resolving the name again, so DNS rebinding reaches nothing new. Plain
// absolute-form http requests are refused: a command reaches http hosts only through a tunnel.

export const ALLOWED_PORTS: ReadonlySet<number> = new Set([80, 443])
/** Tunnels at once; past it a CONNECT is answered 503. */
export const MAX_TUNNELS = 256
/** Tunnels at once from one source address (one sandbox pod): no session holds every tunnel. */
export const MAX_TUNNELS_PER_SOURCE = 32
/** A tunnel with no traffic for this long is closed. */
export const TUNNEL_IDLE_MS = 5 * 60_000
const CONNECT_TIMEOUT_MS = 15_000

/** Shared (100.64/10, RFC 6598) and the unspecified and benchmark ranges: never a public host. */
const NOT_PUBLIC = new BlockList()
NOT_PUBLIC.addSubnet('100.64.0.0', 10, 'ipv4')
NOT_PUBLIC.addSubnet('0.0.0.0', 8, 'ipv4')
NOT_PUBLIC.addSubnet('198.18.0.0', 15, 'ipv4')
NOT_PUBLIC.addSubnet('224.0.0.0', 3, 'ipv4')
NOT_PUBLIC.addSubnet('ff00::', 8, 'ipv6')

/** Why `address` is not a public one the proxy may connect to, or undefined. */
export function notPublic(address: string): string | undefined {
  const plain = plainAddress(address)
  const v4 = isIP(plain) === 6 ? (embeddedIPv4(plain) ?? undefined) : undefined
  for (const a of [plain, ...(v4 ? [v4] : [])]) {
    if (isLoopbackHost(a)) return 'loopback'
    if (isPrivateAddress(a)) return 'private'
    const family = isIP(a)
    if (family !== 0 && NOT_PUBLIC.check(a, family === 4 ? 'ipv4' : 'ipv6')) return 'not public'
  }
  return undefined
}

export type EgressLog = {
  at: string
  source: string
  host: string
  port: number
  outcome: 'connected' | 'refused' | 'failed'
  reason?: string
  address?: string
  bytes_out?: number
  bytes_in?: number
}

export type EgressProxyOptions = {
  resolve?: Resolver
  log: (entry: EgressLog) => void
  /** Tests only: connect somewhere else than the checked address (a local server). */
  connect?: (address: string, port: number) => Socket
}

/** `host:port` of a CONNECT, or undefined. */
export function connectTarget(authority: string | undefined): { host: string; port: number } | undefined {
  if (!authority) return undefined
  const m = /^(\[[0-9a-fA-F:.]+\]|[A-Za-z0-9.-]+):(\d{1,5})$/.exec(authority)
  if (!m) return undefined
  const port = Number(m[2])
  if (port < 1 || port > 65535) return undefined
  return { host: m[1]!.startsWith('[') ? m[1]!.slice(1, -1) : m[1]!, port }
}

/** The proxy; listen on it yourself. */
export function egressProxy(options: EgressProxyOptions): Server {
  const resolve = options.resolve ?? systemResolver
  let tunnels = 0
  const bySource = new Map<string, number>()
  const server = createServer((_req, res) => {
    // Absolute-form requests (plain http through a proxy) and anything else: refused.
    res.writeHead(405, { 'content-type': 'text/plain', connection: 'close' })
    res.end('This proxy only tunnels (CONNECT) to public hosts on ports 80 and 443.\n')
  })
  server.on('connect', (req: IncomingMessage, client: Duplex, head: Buffer) => {
    const source = req.socket.remoteAddress ?? 'unknown'
    const target = connectTarget(req.url)
    const refuse = (status: string, host: string, port: number, reason: string) => {
      options.log({ at: new Date().toISOString(), source, host, port, outcome: 'refused', reason })
      client.end(`HTTP/1.1 ${status}\r\ncontent-type: text/plain\r\nconnection: close\r\n\r\n${reason}\n`)
    }
    client.on('error', () => {})
    if (!target) return refuse('400 Bad Request', String(req.url ?? ''), 0, 'the CONNECT target must be host:port')
    const { host, port } = target
    if (!ALLOWED_PORTS.has(port)) return refuse('403 Forbidden', host, port, `port ${port} is not allowed: only 80 and 443`)
    if (tunnels >= MAX_TUNNELS) return refuse('503 Service Unavailable', host, port, 'too many tunnels at once')
    const mine = bySource.get(source) ?? 0
    if (mine >= MAX_TUNNELS_PER_SOURCE) {
      return refuse('429 Too Many Requests', host, port, `too many tunnels at once from this sandbox (at most ${MAX_TUNNELS_PER_SOURCE})`)
    }
    tunnels++
    bySource.set(source, mine + 1)
    let released = false
    const release = () => {
      if (!released) {
        released = true
        tunnels--
        const left = (bySource.get(source) ?? 1) - 1
        if (left > 0) bySource.set(source, left)
        else bySource.delete(source)
      }
    }
    // Registered before the check and the connect: a client gone meanwhile frees its slot at once.
    let upstream: Duplex | undefined
    let gone = false
    client.on('close', () => {
      gone = true
      if (upstream) upstream.destroy()
      else release()
    })
    void (async () => {
      let addresses: string[]
      try {
        addresses = await assertHostAllowed(`https://${isIP(host) === 6 ? `[${host}]` : host}/`, resolve, 'host', 'a public host')
      } catch (err) {
        release()
        return refuse('403 Forbidden', host, port, (err as Error).message)
      }
      for (const address of addresses) {
        const why = notPublic(address)
        if (why) {
          release()
          return refuse('403 Forbidden', host, port, `host ${host} resolves to ${address}, a ${why} address`)
        }
      }
      if (gone) return
      const address = addresses[0]!
      const socket = options.connect ? options.connect(address, port) : netConnect({ host: plainAddress(address), port })
      upstream = socket
      socket.setTimeout(CONNECT_TIMEOUT_MS)
      let bytesOut = 0
      let bytesIn = 0
      let opened = false
      let logged = false
      const finish = (outcome: EgressLog['outcome'], reason?: string) => {
        release()
        if (logged) return
        logged = true
        options.log({
          at: new Date().toISOString(),
          source,
          host,
          port,
          outcome,
          address,
          bytes_out: bytesOut,
          bytes_in: bytesIn,
          ...(reason ? { reason } : {}),
        })
      }
      socket.once('connect', () => {
        opened = true
        socket.setTimeout(TUNNEL_IDLE_MS)
        client.write('HTTP/1.1 200 Connection Established\r\n\r\n')
        if (head.length) {
          bytesOut += head.length
          socket.write(head)
        }
        client.on('data', (chunk: Buffer) => (bytesOut += chunk.length))
        socket.on('data', (chunk: Buffer) => (bytesIn += chunk.length))
        client.pipe(socket)
        socket.pipe(client)
      })
      socket.on('timeout', () => socket.destroy(new Error(opened ? 'idle' : 'connect timed out')))
      socket.on('error', (err) => {
        if (!opened) {
          finish('failed', err.message)
          client.end('HTTP/1.1 502 Bad Gateway\r\nconnection: close\r\n\r\n')
        } else client.destroy()
      })
      socket.on('close', () => {
        if (opened) {
          finish('connected')
          client.destroy()
        } else finish('failed', 'the client went away before the connect')
      })
    })()
  })
  return server
}
