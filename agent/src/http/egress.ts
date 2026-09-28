import type { LookupAddress } from 'node:dns'
import { lookup } from 'node:dns/promises'
import { get as httpGet } from 'node:http'
import { get as httpsGet } from 'node:https'
import { BlockList, isIP } from 'node:net'
import { plainAddress } from './origins.js'

// Where a gateway `base_url` may point (the Claude credential's
// ANTHROPIC_BASE_URL, src/credentials.ts). The token is sent to that host, and
// the connection test makes the service itself call it, so a base URL is a
// server-side request the operator (or anyone who reaches Settings, spec §8.3)
// chooses.
//
// ALLOWED: public addresses, and loopback and private ranges (10/8,
// 172.16/12, 192.168/16, fc00::/7, 127/8, ::1). A LiteLLM or similar gateway on
// the LAN, or in the same cluster, is the normal reason to use kind `gateway`,
// so refusing private ranges would refuse the feature.
//
// REFUSED: link-local (169.254.0.0/16, fe80::/10), where every major cloud
// serves instance metadata and credentials (169.254.169.254, and AWS's IPv6
// fd00:ec2::254), Alibaba Cloud's 100.100.100.200, and the metadata host
// names. None of them is ever a model gateway.
//
// The HOST IS RESOLVED and every address it resolves to is checked, not just
// the literal: `metadata.example` pointing at 169.254.169.254 is refused like
// the literal is. This is a check at save and test time; Claude Code resolves
// the name again when it connects, so a name re-pointed afterwards (DNS
// rebinding) is not caught here. It narrows the SSRF, it is not a network
// policy; an egress NetworkPolicy on the pod is the boundary.

const BLOCKED = new BlockList()
BLOCKED.addSubnet('169.254.0.0', 16, 'ipv4')
BLOCKED.addAddress('100.100.100.200', 'ipv4')
BLOCKED.addSubnet('fe80::', 10, 'ipv6')
BLOCKED.addAddress('fd00:ec2::254', 'ipv6')
// 0.0.0.0 and :: reach the local host on Linux under another name; a gateway
// on loopback should say 127.0.0.1 or localhost.
BLOCKED.addAddress('0.0.0.0', 'ipv4')
BLOCKED.addAddress('::', 'ipv6')

const BLOCKED_NAMES = new Set([
  'metadata',
  'metadata.google.internal',
  'metadata.goog',
  'instance-data',
  'instance-data.ec2.internal',
])

export type Resolver = (hostname: string) => Promise<string[]>

export const systemResolver: Resolver = async (hostname) =>
  (await lookup(hostname, { all: true, verbatim: true })).map((entry) => entry.address)

export class EgressError extends Error {
  override name = 'EgressError'
}

// IPv6 forms that carry an IPv4 address inside them, and reach it: a NAT64
// gateway or 6to4 relay turns `64:ff9b::a9fe:a9fe` into 169.254.169.254.
// Each is checked as the IPv4 address it embeds (RFC 6052 §2.2 for the /96
// well-known prefix, RFC 3056 §2 for 6to4, RFC 4380 §4 for Teredo's
// obfuscated client address, RFC 4291 §2.5.5.1 for the deprecated
// IPv4-compatible form). The local-use NAT64 prefix 64:ff9b:1::/48 (RFC 8215)
// may embed at several offsets, so it is refused whole.
BLOCKED.addSubnet('64:ff9b:1::', 48, 'ipv6')

/** The 16 bytes of an IPv6 address (any textual form Node accepts), or undefined. */
export function ipv6Bytes(address: string): number[] | undefined {
  let text = address
  const dotted = /^(.*:)(\d+)\.(\d+)\.(\d+)\.(\d+)$/.exec(text)
  if (dotted) {
    const [a, b, c, d] = dotted.slice(2).map(Number) as [number, number, number, number]
    if ([a, b, c, d].some((n) => n > 255)) return undefined
    text = `${dotted[1]}${((a << 8) | b).toString(16)}:${((c << 8) | d).toString(16)}`
  }
  const halves = text.split('::')
  if (halves.length > 2) return undefined
  const head = halves[0] ? halves[0].split(':') : []
  const tail = halves.length === 2 && halves[1] ? halves[1].split(':') : []
  const fill = halves.length === 2 ? 8 - head.length - tail.length : 0
  if (fill < 0 || (halves.length === 1 && head.length !== 8)) return undefined
  const groups = [...head, ...Array<string>(fill).fill('0'), ...tail].map((g) =>
    /^[0-9a-f]{1,4}$/i.test(g) ? parseInt(g, 16) : NaN,
  )
  if (groups.length !== 8 || groups.some((g) => Number.isNaN(g))) return undefined
  return groups.flatMap((g) => [g >> 8, g & 0xff])
}

/** The IPv4 address an IPv6 address embeds and routes to, if it is one of the forms above. */
export function embeddedIPv4(address: string): string | undefined {
  const b = ipv6Bytes(address)
  if (!b) return undefined
  const v4 = (o: number, xor = 0) => [0, 1, 2, 3].map((i) => (b[o + i]! ^ xor) & 0xff).join('.')
  const zero = (from: number, to: number) => b.slice(from, to).every((x) => x === 0)
  if (zero(0, 12)) return v4(12) // ::a.b.c.d (IPv4-compatible)
  if (zero(0, 10) && b[10] === 0xff && b[11] === 0xff) return v4(12) // ::ffff:a.b.c.d
  if (b[0] === 0x00 && b[1] === 0x64 && b[2] === 0xff && b[3] === 0x9b && zero(4, 12)) return v4(12) // 64:ff9b::/96
  if (b[0] === 0x20 && b[1] === 0x02) return v4(2) // 2002::/16, 6to4
  if (b[0] === 0x20 && b[1] === 0x01 && b[2] === 0x00 && b[3] === 0x00) return v4(12, 0xff) // 2001::/32, Teredo
  return undefined
}

function blockedAddress(address: string): boolean {
  const plain = plainAddress(address)
  const family = isIP(plain)
  if (family === 0) return true // not an address at all: refuse rather than guess
  if (BLOCKED.check(plain, family === 4 ? 'ipv4' : 'ipv6')) return true
  const inner = family === 6 ? embeddedIPv4(plain) : undefined
  return inner !== undefined && BLOCKED.check(inner, 'ipv4')
}

function bareHost(hostname: string): string {
  const lower = hostname.toLowerCase().replace(/\.$/, '')
  return lower.startsWith('[') ? lower.slice(1, -1) : lower
}

/** Throws EgressError when `baseUrl`'s host is, or resolves to, a refused address. */
export async function assertGatewayHostAllowed(baseUrl: string, resolve: Resolver = systemResolver): Promise<void> {
  await assertHostAllowed(baseUrl, resolve, 'base_url', 'a model gateway')
}

/**
 * The same check for any URL a secret is sent to: the gateway base URL above,
 * or a plugin's MCP endpoint (#297, src/plugins/registry.ts). `field` names the
 * setting in the error; `purpose` says what the host was meant to be.
 * Resolves to the checked addresses, so a caller can connect to exactly those
 * (the plugin forwarder does, src/plugins/forwarder.ts).
 */
export async function assertHostAllowed(
  url: string,
  resolve: Resolver = systemResolver,
  field = 'url',
  purpose = 'an allowed host',
): Promise<string[]> {
  const bare = bareHost(new URL(url).hostname)
  if (BLOCKED_NAMES.has(bare)) {
    throw new EgressError(`${field} host ${bare} is a cloud metadata service, not ${purpose}`)
  }
  let addresses: string[]
  if (isIP(bare) !== 0) {
    addresses = [bare]
  } else {
    try {
      addresses = await resolve(bare)
    } catch {
      throw new EgressError(`${field} host ${bare} cannot be resolved from the agent service`)
    }
    if (addresses.length === 0) throw new EgressError(`${field} host ${bare} resolves to no address`)
  }
  const refused = addresses.find(blockedAddress)
  if (refused !== undefined) {
    throw new EgressError(
      `${field} host ${bare} resolves to ${refused}, a link-local or cloud metadata address; ` +
        'loopback and private addresses are allowed, those are not',
    )
  }
  return addresses
}

// ---------------------------------------------------------------------------
// Pinned JSON GETs, for the OIDC issuer's metadata and JWKS (#262,
// src/auth/oidc.ts). Unlike a gateway, which Claude Code connects to on its
// own after the check above, here the agent makes the request itself, so the
// check is on the connection: the socket's `lookup` returns only the addresses
// that passed `assertHostAllowed`, and a name re-pointed between check and
// connect (DNS rebinding) cannot reach a refused address. Redirects are not
// followed (a 3xx is an error; following it would be a second, unchecked
// request), the body is capped, and the request has a deadline.

export type EgressGetOptions = {
  /** Names the URL in errors, e.g. "issuer" or "jwks_uri". */
  label: string
  resolve?: Resolver
  /** Default 5 s. */
  timeoutMs?: number
  /** Default 512 KiB. */
  maxBytes?: number
}

/** `localhost` and loopback literals: the only hosts allowed plain `http:` (local development and tests). */
export function isLoopbackHost(hostname: string): boolean {
  const bare = bareHost(hostname)
  if (bare === 'localhost' || bare === '::1') return true
  return isIP(bare) === 4 && bare.startsWith('127.')
}

/** `https:`, or `http:` to a loopback host; anything else throws EgressError. */
export function assertSecureUrl(url: string, label: string): URL {
  let target: URL
  try {
    target = new URL(url)
  } catch {
    throw new EgressError(`${label} ${url} is not a URL`)
  }
  if (target.protocol !== 'https:' && !(target.protocol === 'http:' && isLoopbackHost(target.hostname))) {
    throw new EgressError(`${label} ${url} must use https (plain http is allowed for loopback only)`)
  }
  if (target.username || target.password) throw new EgressError(`${label} ${url} must not carry credentials`)
  return target
}

type LookupCallback = (err: NodeJS.ErrnoException | null, address: string | LookupAddress[], family?: number) => void

/** GETs `url` and parses the body as JSON. Every refusal or failure is an EgressError. */
export async function egressGetJson(url: string, options: EgressGetOptions): Promise<unknown> {
  const { label } = options
  const target = assertSecureUrl(url, label)
  const addresses = await assertHostAllowed(target.href, options.resolve ?? systemResolver, label, 'an identity provider')
  const pinned: LookupAddress[] = addresses.map((address) => {
    const plain = plainAddress(address)
    return { address: plain, family: isIP(plain) === 6 ? 6 : 4 }
  })
  const first = pinned[0]!
  // net.connect calls `lookup(host, options, cb)`, with `options.all` set when
  // it tries several addresses (autoSelectFamily).
  const lookupPinned = (_host: string, opts: unknown, cb: LookupCallback): void => {
    if (typeof opts === 'object' && opts !== null && (opts as { all?: boolean }).all === true) cb(null, pinned)
    else cb(null, first.address, first.family)
  }
  const timeoutMs = options.timeoutMs ?? 5000
  const maxBytes = options.maxBytes ?? 512 * 1024
  const get = target.protocol === 'https:' ? httpsGet : httpGet
  const fail = (detail: string) => new EgressError(`${label} ${url} ${detail}`)

  const body = await new Promise<string>((resolveBody, rejectBody) => {
    let settled = false
    const settle = (err: EgressError | undefined, text?: string) => {
      if (settled) return
      settled = true
      clearTimeout(deadline)
      if (err) {
        req.destroy()
        rejectBody(err)
      } else {
        resolveBody(text ?? '')
      }
    }
    const asEgress = (err: Error) => (err instanceof EgressError ? err : fail(`failed: ${err.message}`))
    const req = get(target, { lookup: lookupPinned as never, headers: { accept: 'application/json' } }, (res) => {
      const status = res.statusCode ?? 0
      if (status !== 200) {
        const redirect = status >= 300 && status < 400 ? ' (redirects are not followed)' : ''
        settle(fail(`answered HTTP ${status}${redirect}`))
        return
      }
      let size = 0
      const chunks: Buffer[] = []
      res.on('data', (chunk: Buffer) => {
        size += chunk.length
        if (size > maxBytes) settle(fail(`returned more than ${maxBytes} bytes`))
        else chunks.push(chunk)
      })
      res.on('end', () => settle(undefined, Buffer.concat(chunks).toString('utf8')))
      res.on('error', (err) => settle(asEgress(err)))
      res.on('aborted', () => settle(fail('was cut off')))
    })
    const deadline = setTimeout(() => settle(fail(`did not answer within ${timeoutMs} ms`)), timeoutMs)
    req.on('error', (err) => settle(asEgress(err)))
  })
  try {
    return JSON.parse(body) as unknown
  } catch {
    throw fail('did not return JSON')
  }
}
