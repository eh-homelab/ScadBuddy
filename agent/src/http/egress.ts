import { lookup } from 'node:dns/promises'
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

function blockedAddress(address: string): boolean {
  const plain = plainAddress(address)
  const family = isIP(plain)
  if (family === 0) return true // not an address at all: refuse rather than guess
  return BLOCKED.check(plain, family === 4 ? 'ipv4' : 'ipv6')
}

/** Throws EgressError when `baseUrl`'s host is, or resolves to, a refused address. */
export async function assertGatewayHostAllowed(baseUrl: string, resolve: Resolver = systemResolver): Promise<void> {
  const hostname = new URL(baseUrl).hostname.toLowerCase().replace(/\.$/, '')
  const bare = hostname.startsWith('[') ? hostname.slice(1, -1) : hostname
  if (BLOCKED_NAMES.has(bare)) {
    throw new EgressError(`base_url host ${bare} is a cloud metadata service, not a model gateway`)
  }
  let addresses: string[]
  if (isIP(bare) !== 0) {
    addresses = [bare]
  } else {
    try {
      addresses = await resolve(bare)
    } catch {
      throw new EgressError(`base_url host ${bare} cannot be resolved from the agent service`)
    }
    if (addresses.length === 0) throw new EgressError(`base_url host ${bare} resolves to no address`)
  }
  const refused = addresses.find(blockedAddress)
  if (refused !== undefined) {
    throw new EgressError(
      `base_url host ${bare} resolves to ${refused}, a link-local or cloud metadata address; ` +
        'loopback and private addresses are allowed, those are not',
    )
  }
}
