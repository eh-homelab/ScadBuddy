// Warm Claude Code processes (unified runtime spec 2026-10-13 §5.3, plan Phase 2).
// A process is reused only while the fingerprint of everything fixed at its spawn
// still matches the turn's.

import { createHash } from 'node:crypto'
import type { FingerprintParts } from './turnRunner.js'

type Json = null | boolean | number | string | Json[] | { [key: string]: Json }

/** Object keys sorted at every depth; arrays sorted (primitives by value, objects by their canonical JSON), so order never matters. */
function canonical(value: unknown): Json {
  if (Array.isArray(value)) {
    return value
      .map((item) => canonical(item))
      .sort((a, b) => {
        const x = JSON.stringify(a)
        const y = JSON.stringify(b)
        return x < y ? -1 : x > y ? 1 : 0
      })
  }
  if (value !== null && typeof value === 'object') {
    const out: { [key: string]: Json } = {}
    for (const key of Object.keys(value).sort()) {
      const item = (value as Record<string, unknown>)[key]
      if (item !== undefined) out[key] = canonical(item)
    }
    return out
  }
  return (value ?? null) as Json
}

/** The sha256 hex of the parts' canonical JSON. The parts hold no secret in clear (plugin header versions are HMACs). */
export function fingerprint(parts: FingerprintParts): string {
  return createHash('sha256').update(JSON.stringify(canonical(parts))).digest('hex')
}
