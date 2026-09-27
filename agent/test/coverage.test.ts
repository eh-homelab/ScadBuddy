import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'
import { NOT_A_TOOL, PENDING_ROUTES } from '../src/tools/coverage.js'
import { ALL_TOOLS } from '../src/tools/index.js'

// The openapi coverage check (spec §5.1, issue #251): every /api/v1 operation
// in backend/openapi.json has a tool, or an allowlist entry with a reason.
// A new backend route fails this test until one of the two is added.

const spec = JSON.parse(readFileSync(new URL('../../backend/openapi.json', import.meta.url), 'utf8')) as {
  paths: Record<string, Record<string, unknown>>
}
const METHODS = ['get', 'put', 'post', 'delete', 'patch']

const operations = Object.entries(spec.paths)
  .filter(([path]) => path.startsWith('/api/v1/'))
  .flatMap(([path, ops]) => Object.keys(ops).filter((m) => METHODS.includes(m)).map((m) => `${m.toUpperCase()} ${path}`))

const byTool = new Map<string, string[]>()
for (const tool of ALL_TOOLS) {
  for (const route of tool.routes) byTool.set(route, [...(byTool.get(route) ?? []), tool.name])
}
const allowlisted = new Set(NOT_A_TOOL.map((e) => e.operation as string))
const pending = new Set(PENDING_ROUTES.map((e) => e.operation))

describe('openapi coverage', () => {
  it('reads a non-trivial spec', () => {
    expect(operations.length).toBeGreaterThan(50)
  })

  it('maps every /api/v1 operation to a tool or an allowlist entry with a reason', () => {
    const uncovered = operations.filter((op) => !byTool.has(op) && !allowlisted.has(op) && !pending.has(op))
    expect(uncovered, 'add a tool (src/tools/) or a NOT_A_TOOL entry (src/tools/coverage.ts)').toEqual([])
  })

  it('has no stale entries: every tool route and allowlist entry exists in the spec', () => {
    const known = new Set(operations)
    expect([...byTool.keys()].filter((op) => !known.has(op))).toEqual([])
    expect([...allowlisted].filter((op) => !known.has(op))).toEqual([])
  })

  it('does not both allowlist and map an operation', () => {
    expect([...allowlisted].filter((op) => byTool.has(op) || pending.has(op))).toEqual([])
    expect([...pending].filter((op) => byTool.has(op))).toEqual([])
  })

  it('gives every allowlist and pending entry a reason', () => {
    for (const entry of [...NOT_A_TOOL, ...PENDING_ROUTES]) expect(entry.reason.length, entry.operation).toBeGreaterThan(20)
  })
})
