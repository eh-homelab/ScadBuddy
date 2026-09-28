import { BlockList, isIP } from 'node:net'

// Which requests come from the ScadBuddy UI's own origin (spec §8.4, "An
// `Origin` check ... prevents DNS rebinding"). Shared by every route that needs
// that answer: the credential writes (routes/guard.ts) and `/mcp`
// (auth/authenticate.ts `mcpTransportProblem`, #251) now, and the agent's own
// sockets (#264) when they land, so there is one allowlist.
// The UI's `/api/v1/ws` is the backend's (`backend/scadbuddy/api/realtime.py`, #266),
// which applies the same allowlist rule against the stored public URL.
//
// WHY AN ALLOWLIST, NOT "Origin equals Host". Under DNS rebinding an attacker's
// page at http://evil.test:8081 has its name re-pointed at this service, so the
// browser sends `Host: evil.test:8081` and `Origin: http://evil.test:8081`: they
// match, and the page is same-origin as far as the browser is concerned (no
// CORS preflight, JSON bodies allowed). Only a list of the names this service
// is really served under tells the two apart.
//
// THE LIST. `SCADBUDDY_PUBLIC_URL`, the same variable the backend reads for the
// URL Bambuddy's sidebar points at (backend/scadbuddy/core/settings.py
// `public_url`; README "Running it"). Its origin is the one public origin. The
// loopback pair (Host `localhost`/`127.0.0.1`/`[::1]` with the same Origin, from
// a loopback peer) is always accepted, for local development and
// `kubectl port-forward`; with the variable unset it is the only thing
// accepted.
//
// FORWARDED HEADERS. `X-Forwarded-Proto` and `X-Forwarded-Host` are read only
// from a peer inside `SCADBUDDY_AGENT_TRUSTED_PROXIES` (a comma-separated CIDR
// list, e.g. the ingress controller's pod range). From any other peer they are
// ignored: the request is what arrived on the socket, plain HTTP to its `Host`.
// When several values are present the LAST one is used, the one appended by
// the nearest proxy; earlier ones may have come from the client.
//
// Default ports are normalised on both sides (`https://x:443` is `https://x`)
// through WHATWG URL parsing, so `Host: x:443` matches `Origin: https://x`.

export type OriginPolicy = {
  /** Normalised public origins (`scheme://host[:port]`, default port dropped). */
  publicOrigins: ReadonlySet<string>
  /** Peers whose X-Forwarded-* headers are believed. */
  trustedProxies: BlockList
}

/** A request as the checks see it; routes adapt their framework's request to this. */
export type RequestFacts = {
  /** The socket peer's address, when known. */
  peer: string | undefined
  header(name: string): string | undefined
}

export class OriginConfigError extends Error {
  override name = 'OriginConfigError'
}

/** `scheme://host[:port]` with the default port dropped and the host lower-cased; undefined if not http(s). */
export function normaliseOrigin(raw: string | undefined): string | undefined {
  if (!raw) return undefined
  let url: URL
  try {
    url = new URL(raw.trim())
  } catch {
    return undefined
  }
  if (url.protocol !== 'http:' && url.protocol !== 'https:') return undefined
  return url.origin
}

// A Host header value: a DNS name or IPv4 address, or a bracketed IPv6 literal,
// with an optional port. Anything else (userinfo, a path) is not a host.
const HOST_HEADER = /^(?:[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?|\[[0-9A-Fa-f:.]+\])(?::\d{1,5})?$/

/** The origin a request was addressed to, from its scheme and Host; default port dropped. */
export function requestOrigin(scheme: 'http' | 'https', host: string | undefined): string | undefined {
  if (!host || !HOST_HEADER.test(host)) return undefined
  return normaliseOrigin(`${scheme}://${host}`)
}

const LOOPBACK_HOSTNAMES = new Set(['localhost', '127.0.0.1', '[::1]'])

/** Unwraps an IPv4-mapped IPv6 peer (`::ffff:10.0.0.1`), which Node reports on dual-stack sockets. */
export function plainAddress(address: string): string {
  const mapped = /^::ffff:(\d+\.\d+\.\d+\.\d+)$/i.exec(address)
  return mapped?.[1] ?? address
}

const LOOPBACK_PEERS = new BlockList()
LOOPBACK_PEERS.addSubnet('127.0.0.0', 8, 'ipv4')
LOOPBACK_PEERS.addAddress('::1', 'ipv6')

export function isLoopbackPeer(address: string | undefined): boolean {
  return inBlockList(LOOPBACK_PEERS, address)
}

export function inBlockList(list: BlockList, address: string | undefined): boolean {
  if (address === undefined) return false
  const plain = plainAddress(address)
  const family = isIP(plain)
  if (family === 0) return false
  return list.check(plain, family === 4 ? 'ipv4' : 'ipv6')
}

/** Parses `10.0.0.0/8, fd00::/8, 192.168.1.10` (a bare address is a /32 or /128). */
export function parseCidrList(raw: string | undefined, name = 'SCADBUDDY_AGENT_TRUSTED_PROXIES'): BlockList {
  const list = new BlockList()
  for (const entry of (raw ?? '').split(',').map((s) => s.trim()).filter(Boolean)) {
    const [address = '', prefixText, extra] = entry.split('/')
    const family = isIP(address)
    const max = family === 4 ? 32 : 128
    const prefix = prefixText === undefined ? max : Number(prefixText)
    if (family === 0 || extra !== undefined || !Number.isInteger(prefix) || prefix < 0 || prefix > max) {
      throw new OriginConfigError(`${name}: "${entry}" is not an IP address or CIDR range`)
    }
    list.addSubnet(address, prefix, family === 4 ? 'ipv4' : 'ipv6')
  }
  return list
}

/** Builds the policy from SCADBUDDY_PUBLIC_URL and SCADBUDDY_AGENT_TRUSTED_PROXIES. Throws on malformed values. */
export function originPolicy(publicUrl: string | undefined, trustedProxies: string | undefined): OriginPolicy {
  const publicOrigins = new Set<string>()
  if (publicUrl !== undefined) {
    const origin = normaliseOrigin(publicUrl)
    if (!origin) throw new OriginConfigError('SCADBUDDY_PUBLIC_URL must be an http(s) URL')
    publicOrigins.add(origin)
  }
  return { publicOrigins, trustedProxies: parseCidrList(trustedProxies) }
}

/** The last comma-separated value of a header: the one the nearest proxy added. */
function lastValue(value: string | undefined): string | undefined {
  const last = value?.split(',').at(-1)?.trim()
  return last ? last : undefined
}

export type EffectiveRequest = {
  /** How the client reached us: the trusted proxy's word, else the socket (always plain HTTP here). */
  scheme: 'http' | 'https' | undefined
  host: string | undefined
  /** True when the peer is a trusted proxy and its forwarded headers were used. */
  viaTrustedProxy: boolean
}

export function effectiveRequest(req: RequestFacts, policy: OriginPolicy): EffectiveRequest {
  if (inBlockList(policy.trustedProxies, req.peer)) {
    const proto = lastValue(req.header('x-forwarded-proto'))?.toLowerCase()
    return {
      scheme: proto === 'https' || proto === 'http' ? proto : proto === undefined ? 'http' : undefined,
      host: lastValue(req.header('x-forwarded-host')) ?? req.header('host'),
      viaTrustedProxy: true,
    }
  }
  return { scheme: 'http', host: req.header('host'), viaTrustedProxy: false }
}

/**
 * The client a trusted proxy names: the last `X-Forwarded-For` value, the one
 * the nearest proxy appended. Undefined from any other peer, whose header is
 * not believed.
 */
export function forwardedClient(req: RequestFacts, policy: OriginPolicy): string | undefined {
  return inBlockList(policy.trustedProxies, req.peer) ? lastValue(req.header('x-forwarded-for')) : undefined
}

export type OriginVerdict =
  | { ok: true; origin: string; via: 'public' | 'loopback' }
  | { ok: false; reason: 'no-origin' | 'malformed-origin' | 'not-allowed' }

/**
 * Whether `Origin` names the UI and the request was addressed to the same
 * allowed origin. Both sides must be on the list: an allowed Origin sent to an
 * unknown Host is a rebinding (or a misrouted request) just as much as the
 * reverse.
 */
export function checkOrigin(req: RequestFacts, policy: OriginPolicy): OriginVerdict {
  const rawOrigin = req.header('origin')
  if (!rawOrigin) return { ok: false, reason: 'no-origin' }
  const origin = normaliseOrigin(rawOrigin)
  if (!origin) return { ok: false, reason: 'malformed-origin' }
  const effective = effectiveRequest(req, policy)
  if (effective.scheme === undefined) return { ok: false, reason: 'not-allowed' }
  const target = requestOrigin(effective.scheme, effective.host)
  if (!target) return { ok: false, reason: 'not-allowed' }

  if (policy.publicOrigins.has(origin) && policy.publicOrigins.has(target) && origin === target) {
    return { ok: true, origin, via: 'public' }
  }
  // Local development: a browser on this machine talking straight to the port.
  if (
    !effective.viaTrustedProxy &&
    isLoopbackPeer(req.peer) &&
    origin === target &&
    LOOPBACK_HOSTNAMES.has(new URL(target).hostname)
  ) {
    return { ok: true, origin, via: 'loopback' }
  }
  return { ok: false, reason: 'not-allowed' }
}

/**
 * Transport (spec §8.4, "HTTPS only ... `X-Forwarded-Proto` is trusted from the
 * ingress only ... The only exception is loopback"). True when a trusted proxy
 * says the client used HTTPS, or the peer is loopback and no proxy is involved.
 */
export function isSecureTransport(req: RequestFacts, policy: OriginPolicy): boolean {
  const effective = effectiveRequest(req, policy)
  if (effective.viaTrustedProxy) return effective.scheme === 'https'
  return isLoopbackPeer(req.peer)
}
