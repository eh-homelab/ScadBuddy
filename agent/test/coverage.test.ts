import { readFileSync } from 'node:fs'
import { afterEach, describe, expect, it } from 'vitest'
import { NOT_A_TOOL, PENDING_ROUTES } from '../src/tools/coverage.js'
import { ALL_TOOLS } from '../src/tools/index.js'
import { connect, testApp } from './helpers/mcp.js'

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

  it('has no pending route that has already landed in the spec', () => {
    // A PENDING_ROUTES operation that is in backend/openapi.json has merged:
    // it needs its tool, or a NOT_A_TOOL entry, now.
    const known = new Set(operations)
    expect([...pending].filter((op) => known.has(op)), 'write the tool and drop the PENDING_ROUTES entry').toEqual([])
  })

  it('gives every allowlist and pending entry a reason', () => {
    for (const entry of [...NOT_A_TOOL, ...PENDING_ROUTES]) expect(entry.reason.length, entry.operation).toBeGreaterThan(20)
  })
})

// The same check against what an external agent is actually offered: the
// tools `/mcp` answers `tools/list` with (issue #259). A tool in the registry
// that the endpoint failed to serve would leave its routes uncovered here even
// though the registry-based check above passes.
describe('openapi coverage over /mcp tools/list', () => {
  const closers: (() => Promise<void>)[] = []
  afterEach(async () => {
    await Promise.all(closers.splice(0).map((close) => close()))
  })

  async function listed(): Promise<string[]> {
    const { app, tokens } = testApp()
    const { token } = await tokens.mint({ name: 'coverage', tier: 'outward' })
    const client = await connect(app, { headers: { authorization: `Bearer ${token}` } })
    closers.push(() => client.close())
    const names: string[] = []
    let cursor: string | undefined
    do {
      const page = await client.listTools(cursor ? { cursor } : {})
      names.push(...page.tools.map((t) => t.name))
      cursor = page.nextCursor
    } while (cursor)
    return names
  }

  it('lists only registry tools, each once', async () => {
    const names = await listed()
    const registry = new Set(ALL_TOOLS.map((t) => t.name))
    expect(names.filter((n) => !registry.has(n))).toEqual([])
    expect(new Set(names).size).toBe(names.length)
  })

  it('covers every /api/v1 operation with a listed tool or an allowlist entry', async () => {
    const names = new Set(await listed())
    const coveredByListed = new Set<string>(ALL_TOOLS.filter((t) => names.has(t.name)).flatMap((t) => [...t.routes]))
    const uncovered = operations.filter((op) => !coveredByListed.has(op) && !allowlisted.has(op) && !pending.has(op))
    expect(uncovered, 'a route whose tool /mcp does not list').toEqual([])
  })
})
