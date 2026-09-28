import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'
import { AGENT_ACTOR_HEADER } from '../src/harness/headlessBrowser.js'
import { ALL_TOOLS } from '../src/tools/index.js'

// The backend's agent-actor gate (backend/scadbuddy/api/agent_actor.py, #349,
// spec §5.3) lets a request from the headless browser make only the non-safe
// operations listed in AGENT_ALLOWED_WRITES. That list must be exactly the
// read/write-tier tools' non-GET routes that no outward tool also uses: a route
// an outward tool shares (a library pin from a URL uses the same PUT as a plain
// pin) is outward for the gate, since the gate cannot tell them apart.

const GATE = fileURLToPath(new URL('../../backend/scadbuddy/api/agent_actor.py', import.meta.url))

function backendList(): string[] {
  const source = readFileSync(GATE, 'utf8')
  const block = /# agent-allowed-writes:begin([\s\S]*?)# agent-allowed-writes:end/.exec(source)?.[1] ?? ''
  return [...block.matchAll(/"((?:POST|PUT|PATCH|DELETE) \/[^"]+)"/g)].map((m) => m[1]!)
}

function derived(): string[] {
  const nonSafe = (op: string) => !op.startsWith('GET ')
  const outward = new Set(ALL_TOOLS.filter((t) => t.risk === 'outward').flatMap((t) => t.routes))
  const allowed = new Set(
    ALL_TOOLS.filter((t) => t.risk !== 'outward')
      .flatMap((t) => t.routes)
      .filter((op) => nonSafe(op) && !outward.has(op)),
  )
  return [...allowed]
}

describe('the agent-actor gate', () => {
  it('lists exactly the non-outward write routes of the tool registry', () => {
    const listed = backendList()
    expect(listed.length).toBeGreaterThan(0)
    expect(new Set(listed).size).toBe(listed.length)
    expect([...listed].sort()).toEqual(derived().sort())
  })

  it('reads the same header the harness sends', () => {
    const source = readFileSync(GATE, 'utf8')
    expect(source).toContain(`AGENT_ACTOR_HEADER = "${AGENT_ACTOR_HEADER.toLowerCase()}"`)
  })
})
