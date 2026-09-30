import { describe, expect, it, vi } from 'vitest'
import { createBackendClient } from '../src/api/backend.js'
import { tiersUpTo } from '../src/auth/principal.js'
import { ALL_TOOLS } from '../src/tools/index.js'
import { runTool, type Tool, type ToolContext } from '../src/tools/registry.js'
import { RenderLimiter } from '../src/tools/renderLimits.js'
import { BLANK_TEMPLATE } from '../src/tools/templates.js'
import { BACKEND, firstText, services } from './helpers/mcp.js'

// #252: multi-file models, starting from a template, and the bound on the
// renders an agent starts.

type Seen = { method: string; path: string; body: unknown }

const RECORD = { slug: 'plate', name: 'Plate', origin: 'user', version: 'c'.repeat(40), has_thumbnail: false, has_readme: false, updated_at: 'now' }

function backend(answer: (seen: Seen) => Response = () => Response.json(RECORD)) {
  const seen: Seen[] = []
  const client = createBackendClient(BACKEND, async (request) => {
    const r = request as Request
    const raw = await r.text()
    const entry = { method: r.method, path: decodeURIComponent(new URL(r.url).pathname), body: raw ? JSON.parse(raw) : undefined }
    seen.push(entry)
    return answer(entry)
  })
  return { client, seen }
}

function tool(name: string): Tool {
  const t = ALL_TOOLS.find((x) => x.name === name)
  if (!t) throw new Error(`no tool ${name}`)
  return t
}

function ctx(client: ReturnType<typeof backend>['client'], overrides: Partial<ToolContext> = {}): ToolContext {
  return {
    ...services({ backend: client }),
    principal: { id: 'token:t1', kind: 'bearer', tiers: tiersUpTo('write') },
    progress: async () => {},
    signal: new AbortController().signal,
    ...overrides,
  }
}

describe('source files', () => {
  it('lists, reads, writes and removes the files beside model.scad', async () => {
    const { client, seen } = backend((s) =>
      s.method === 'GET' && s.path.endsWith('.scad')
        ? new Response('module bar() {}', { headers: { 'content-type': 'text/plain' } })
        : s.path.endsWith('/files')
          ? Response.json([{ name: 'model.scad', size: 10, main: true }])
          : Response.json(RECORD),
    )
    const c = ctx(client)
    await runTool(tool('list_source_files'), { slug: 'plate' }, c)
    const read = await runTool(tool('get_source_file'), { slug: 'plate', name: 'parts.scad' }, c)
    await runTool(tool('write_source_file'), { slug: 'plate', name: 'parts.scad', content: 'module bar() {}', message: 'Add bar' }, c)
    await runTool(tool('delete_source_file'), { slug: 'plate', name: 'parts.scad' }, c)
    expect(firstText(read)).toBe('module bar() {}')
    expect(seen.map((s) => `${s.method} ${s.path}`)).toEqual([
      'GET /api/v1/models/plate/files',
      'GET /api/v1/models/plate/files/parts.scad',
      'PUT /api/v1/models/plate/files/parts.scad',
      'DELETE /api/v1/models/plate/files/parts.scad',
    ])
    expect(seen[2]!.body).toEqual({ content: 'module bar() {}', message: 'Add bar' })
    expect([tool('write_source_file').risk, tool('delete_source_file').risk]).toEqual(['write', 'write'])
  })

  it('refuses a name that is not a bare .scad file, before calling the backend', async () => {
    const { client, seen } = backend()
    for (const name of ['../model.json', 'sub/x.scad', '.hidden.scad', 'notes.txt']) {
      const result = await runTool(tool('get_source_file'), { slug: 'plate', name }, ctx(client))
      expect(result.isError, name).toBe(true)
    }
    expect(seen).toEqual([])
  })
})

describe('create_from_template', () => {
  it('creates a blank model from the blank template', async () => {
    const { client, seen } = backend()
    await runTool(tool('create_from_template'), { name: 'Plate', from: 'blank', tags: ['sign'] }, ctx(client))
    expect(seen.map((s) => `${s.method} ${s.path}`)).toEqual(['POST /api/v1/models'])
    expect(seen[0]!.body).toEqual({ name: 'Plate', source: BLANK_TEMPLATE, description: '', tags: ['sign'], force: false })
  })

  it('duplicates a bundled example', async () => {
    const { client, seen } = backend()
    await runTool(tool('create_from_template'), { name: 'My box', from: 'builtin:storage-box' }, ctx(client))
    expect(seen.map((s) => `${s.method} ${s.path}`)).toEqual(['POST /api/v1/models/builtin:storage-box/duplicate'])
    expect(seen[0]!.body).toEqual({ name: 'My box' })
  })

  it('refuses a description or tags for a duplicate instead of dropping them (PR #752 review)', async () => {
    const { client, seen } = backend()
    const result = await runTool(
      tool('create_from_template'),
      { name: 'My box', from: 'builtin:storage-box', tags: ['kitchen'] },
      ctx(client),
    )
    expect(result.isError).toBe(true)
    expect(seen).toEqual([])
  })

  it("keeps the blank template to the authoring skill's conventions", () => {
    // Header naming the extruders (skill section 2).
    expect(BLANK_TEMPLATE).toContain('same file works unchanged on MakerWorld and in ScadBuddy')
    // One `// color` parameter per extruder, in order, and every solid in a color() (section 5).
    const colours = [...BLANK_TEMPLATE.matchAll(/^(\w+) = "#[0-9A-F]{6}"; \/\/ color$/gm)].map((m) => m[1])
    expect(colours).toEqual(['base_color', 'text_color'])
    expect(BLANK_TEMPLATE).toContain('color(base_color)')
    expect(BLANK_TEMPLATE).toContain('color(text_color)')
    // An installed family, typed as a font (section 6), and $fn hidden (section 3).
    expect(BLANK_TEMPLATE).toMatch(/^font = "DejaVu Sans:style=Bold"; \/\/ font$/m)
    expect(BLANK_TEMPLATE.indexOf('/* [Hidden] */')).toBeLessThan(BLANK_TEMPLATE.indexOf('$fn'))
    expect(BLANK_TEMPLATE).not.toContain('lazy-union')
  })
})

describe('render limits', () => {
  it('bounds renders in flight and per window, per principal', () => {
    let now = 0
    const limiter = new RenderLimiter({ concurrent: 2, perWindow: 3, windowMs: 1000, holdMs: 1000 }, () => now)
    const a = limiter.acquire('p')
    const b = limiter.acquire('p')
    expect(() => limiter.acquire('p')).toThrow(/still running, the most at once \(2\)/)
    // Another principal has its own count.
    limiter.acquire('q')()
    a()
    a() // releasing twice counts once
    limiter.acquire('p')()
    b()
    expect(() => limiter.acquire('p')).toThrow(/3 renders in the last .* Try again in 1s/)
    now = 1001
    expect(() => limiter.acquire('p')()).not.toThrow()
  })

  it('keeps only the window\'s starts when it refuses for renders in flight (#774)', () => {
    let now = 0
    const limiter = new RenderLimiter({ concurrent: 2, perWindow: 3, windowMs: 1000, holdMs: 1000 }, () => now)
    const starts = () => (limiter as unknown as { started: Map<string, number[]> }).started.get('p')
    limiter.acquire('p')
    limiter.acquire('p')
    now = 1500
    expect(() => limiter.acquire('p')).toThrow(/still running/)
    expect(starts()).toBeUndefined()
  })

  it('drops a principal once nothing is in flight or in its window', () => {
    let now = 0
    const limiter = new RenderLimiter({ concurrent: 2, perWindow: 3, windowMs: 1000, holdMs: 1000 }, () => now)
    limiter.acquire('anonymous:s1')()
    const held = limiter.acquire('anonymous:s2')
    expect(limiter.tracked).toBe(2)
    now = 1500
    limiter.acquire('anonymous:s3')()
    // s1 has nothing left; s2 is still in flight.
    expect(limiter.tracked).toBe(2)
    held()
    now = 3000
    limiter.acquire('anonymous:s4')()
    expect(limiter.tracked).toBe(1)
  })

  it('a render handed back still running keeps its slot until the job settles', async () => {
    const schema = { groups: [], parameters: [] }
    const id = 'e'.repeat(32)
    let status = 'running'
    const { client, seen } = backend((s) =>
      s.path.endsWith('/schema')
        ? Response.json(schema)
        : s.path.endsWith('/render')
          ? Response.json({ job_id: id }, { status: 202 })
          : Response.json({ id, status, log_tail: [] }),
    )
    const limiter = new RenderLimiter({ concurrent: 1, perWindow: 10, windowMs: 60_000, holdMs: 60_000 })
    const c = ctx(client, { renderLimiter: limiter, renderWaitMs: 0, pollIntervalMs: 1 })
    const first = await runTool(tool('render_model'), { slug: 'plate' }, c)
    expect(firstText(first)).toMatchObject({ status: 'running', note: expect.stringContaining('still rendering') })
    const second = await runTool(tool('render_model'), { slug: 'plate' }, c)
    expect(second.isError).toBe(true)
    expect(String(firstText(second))).toContain('still running, the most at once (1)')
    expect(seen.filter((s) => s.path.endsWith('/render'))).toHaveLength(1)
    // The background poll sees the job settle and frees the slot.
    status = 'done'
    await vi.waitFor(() => limiter.acquire('token:t1')())
  })

  it('render_model refuses past the bound without calling the backend, and releases after a render', async () => {
    const schema = { groups: [], parameters: [] }
    const job = { id: 'd'.repeat(32), status: 'done', log_tail: [] }
    const { client, seen } = backend((s) =>
      s.path.endsWith('/schema') ? Response.json(schema) : s.path.endsWith('/render') ? Response.json({ job_id: job.id }, { status: 202 }) : Response.json(job),
    )
    const limiter = new RenderLimiter({ concurrent: 1, perWindow: 1, windowMs: 60_000, holdMs: 60_000 })
    const c = ctx(client, { renderLimiter: limiter })
    const first = await runTool(tool('render_model'), { slug: 'plate' }, c)
    expect(first.isError, JSON.stringify(first)).toBeFalsy()
    const second = await runTool(tool('render_model'), { slug: 'plate' }, c)
    expect(second.isError).toBe(true)
    expect(String(firstText(second))).toContain('renders in the last 1 min')
    expect(seen.filter((s) => s.path.endsWith('/render'))).toHaveLength(1)
  })
})
