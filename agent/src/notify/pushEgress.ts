import { BlockList, isIP } from 'node:net'
import { assertHostAllowed, EgressError, embeddedIPv4, isPrivateAddress, type Resolver, systemResolver } from '../http/egress.js'
import { plainAddress } from '../http/origins.js'

// Where a push may be sent (#815). A push endpoint comes from the browser, so the agent
// POSTing to it is a server-side request a page chooses. It must therefore be stricter than
// the other egress (http/egress.ts), which allows LAN hosts for gateways and plugins:
//
//   - https only, with no credentials in the URL;
//   - its host on the push-service allowlist (`push_service_hosts` in ai_settings, edited in
//     Settings; DEFAULT_PUSH_HOSTS until then), exactly or as `*.<suffix>` for subdomains;
//   - every address it resolves to public: never loopback, RFC 1918 or ULA, CGNAT,
//     link-local, unspecified or multicast (nor an IPv6 form that embeds one).
//
// It is checked when a subscription is stored and again at every push, and the push
// connects to the address checked here (http/pinned.ts), so a name re-pointed between
// the check and the connection cannot reach a refused address.

/** The browsers' push services (Chrome/Edge's FCM, Firefox's autopush, WNS, Apple's). */
export const DEFAULT_PUSH_HOSTS: readonly string[] = [
  'fcm.googleapis.com',
  'updates.push.services.mozilla.com',
  '*.notify.windows.com',
  '*.push.apple.com',
  'web.push.apple.com',
]

/** The ai_settings key of the push-service allowlist. */
export const SETTING_PUSH_HOSTS = 'push_service_hosts'
export const PUSH_HOSTS_MAX = 50

const PATTERN = /^(\*\.)?([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$/

/** Whether `pattern` is a host name, or `*.` and one, in lower case. */
export function isHostPattern(pattern: string): boolean {
  return pattern.length <= 253 && PATTERN.test(pattern)
}

/** The stored allowlist, or the default when unset or not a list of patterns. */
export function pushHostsOf(stored: unknown): readonly string[] {
  if (!Array.isArray(stored) || !stored.every((p) => typeof p === 'string' && isHostPattern(p))) return DEFAULT_PUSH_HOSTS
  return stored as string[]
}

/** Whether `host` is on `patterns`: exactly, or under a `*.` pattern's suffix (never the suffix itself). */
export function hostOnAllowlist(host: string, patterns: readonly string[]): boolean {
  const h = host.toLowerCase().replace(/\.$/, '')
  return patterns.some((p) => (p.startsWith('*.') ? h.endsWith(p.slice(1)) && h.length > p.length - 1 : h === p))
}

const NOT_PUBLIC = new BlockList()
NOT_PUBLIC.addSubnet('127.0.0.0', 8, 'ipv4')
NOT_PUBLIC.addSubnet('100.64.0.0', 10, 'ipv4') // CGNAT, RFC 6598
NOT_PUBLIC.addSubnet('169.254.0.0', 16, 'ipv4')
NOT_PUBLIC.addSubnet('0.0.0.0', 8, 'ipv4')
NOT_PUBLIC.addSubnet('224.0.0.0', 4, 'ipv4')
NOT_PUBLIC.addSubnet('240.0.0.0', 4, 'ipv4')
NOT_PUBLIC.addAddress('::1', 'ipv6')
NOT_PUBLIC.addAddress('::', 'ipv6')
NOT_PUBLIC.addSubnet('fe80::', 10, 'ipv6')
NOT_PUBLIC.addSubnet('ff00::', 8, 'ipv6')

/** Whether `address` is a public unicast address: none of the ranges above, nor private. */
export function isPublicAddress(address: string): boolean {
  const plain = plainAddress(address).replace(/^::ffff:(?=\d+\.)/i, '')
  const family = isIP(plain)
  if (family === 0) return false
  if (isPrivateAddress(plain) || NOT_PUBLIC.check(plain, family === 4 ? 'ipv4' : 'ipv6')) return false
  const inner = family === 6 ? embeddedIPv4(plain) : undefined
  return inner === undefined || isPublicAddress(inner)
}

/** A checked push endpoint and the address to connect to. */
export type PinnedEndpoint = { url: string; address: string }

/** Checks `endpoint` as above; every refusal is an EgressError. */
export async function checkPushEndpoint(
  endpoint: string,
  hosts: readonly string[],
  resolve: Resolver = systemResolver,
): Promise<PinnedEndpoint> {
  let target: URL
  try {
    target = new URL(endpoint)
  } catch {
    throw new EgressError('endpoint is not a URL')
  }
  if (target.protocol !== 'https:') throw new EgressError('endpoint must use https')
  if (target.username || target.password) throw new EgressError('endpoint must not carry credentials')
  if (!hostOnAllowlist(target.hostname, hosts)) {
    throw new EgressError(`endpoint host ${target.hostname} is not a known push service (Settings → Assistant → Notifications)`)
  }
  const addresses = await assertHostAllowed(target.href, resolve, 'endpoint', 'a push service')
  const refused = addresses.find((a) => !isPublicAddress(a))
  if (refused !== undefined) throw new EgressError(`endpoint host ${target.hostname} resolves to ${refused}, which is not a public address`)
  return { url: target.href, address: addresses[0]! }
}
