import { createHash, createHmac, hkdfSync, timingSafeEqual } from 'node:crypto'
import type { Kek } from '../secrets.js'

// The token a durable segment's subagents present to the agent's loopback MCP server
// (#2320, routes/subagentMcp.ts). The segment runner in agent-durable mints one per
// segment (agent-durable/.../session/subagents.py, the same format) from a key both
// sidecars derive from the KEK they already share, so no secret is added and none
// enters a Temporal payload: the token goes only into the engine's MCP config.
//
//   sbsub.<session uuid>.<turn uuid>.<owner digest>.<expiry, unix seconds>.<hex HMAC-SHA256>
//
// The HMAC covers everything before it. The owner digest (`ownerDigest`) is the session's
// owner when it was minted. The token is only a claim to look up: the route resolves
// the session on every call, and refuses one no longer durable, no longer running that
// turn, or owned by anyone else since (a handoff), so the token dies with its turn and
// with its owner, as well as at its expiry. Nothing the token says grants a tier.

const PREFIX = 'sbsub'
const INFO = 'scadbuddy:durable:subagent_mcp:v1'
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/

/** The HMAC key, as subagents.py derives it (HKDF-SHA256, empty salt). */
export function subagentKey(kek: Kek): Buffer {
  return Buffer.from(hkdfSync('sha256', kek.key, Buffer.alloc(0), INFO, 32))
}

/** The first 32 hex digits of SHA-256 over `<kind>:<id>`. */
export function ownerDigest(owner: { kind: string; id: string }): string {
  return createHash('sha256').update(`${owner.kind}:${owner.id}`).digest('hex').slice(0, 32)
}

function mac(key: Buffer, body: string): string {
  return createHmac('sha256', key).update(body).digest('hex')
}

/** A token for `session`'s turn `turn`, owned by `owner`, until `expiresAt` (unix seconds). */
export function mintSubagentToken(
  key: Buffer,
  claim: { session: string; turn: string; owner: { kind: string; id: string } },
  expiresAt: number,
): string {
  const body = `${claim.session}.${claim.turn}.${ownerDigest(claim.owner)}.${Math.floor(expiresAt)}`
  return `${PREFIX}.${body}.${mac(key, body)}`
}

export type SubagentClaim = { session: string; turn: string; owner: string }

/** What a token claims, if it verifies under one of `keys` and has not expired. */
export function verifySubagentToken(token: string, keys: readonly Buffer[], now = Date.now() / 1000): SubagentClaim | undefined {
  const parts = token.split('.')
  if (parts.length !== 6 || parts[0] !== PREFIX) return undefined
  const [, session, turn, owner, expiry, given] = parts as [string, string, string, string, string, string]
  if (!UUID.test(session) || !UUID.test(turn) || !/^[0-9a-f]{32}$/.test(owner)) return undefined
  if (!/^\d{1,12}$/.test(expiry) || !/^[0-9a-f]{64}$/.test(given) || Number(expiry) <= now) return undefined
  const body = `${session}.${turn}.${owner}.${expiry}`
  const ok = keys.some((key) => timingSafeEqual(Buffer.from(mac(key, body), 'hex'), Buffer.from(given, 'hex')))
  return ok ? { session, turn, owner } : undefined
}
