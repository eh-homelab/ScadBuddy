import { describe, expect, it, vi } from 'vitest'
import { createBackendClient } from '../src/api/backend.js'
import { tiersUpTo } from '../src/auth/principal.js'
import { ALL_TOOLS } from '../src/tools/index.js'
import { runTool, type Tool, type ToolContext } from '../src/tools/registry.js'
import {
  MAX_QUEUED_PER_PRINCIPAL,
  RECHECK_MS,
  RENDER_LIMITS,
  RenderLimiter,
  type RenderLimits,
  renderLimitsFrom,
  renderLimitsSetting,
  renderPrincipal,
} from '../src/tools/renderLimits.js'
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
  const limits = (over: Partial<RenderLimits> = {}): RenderLimits => ({
    concurrent: 2,
    global: 0,
    perWindow: 0,
    windowMs: 1000,
    holdMs: 1000,
    overrides: {},
    ...over,
  })
  const pending = async (p: Promise<unknown>) => {
    let settled = false
    void p.then(
      () => (settled = true),
      () => (settled = true),
    )
    await new Promise((r) => setTimeout(r, 0))
    return !settled
  }

  it('a render past the bound waits for a slot, in order, per principal', async () => {
    const limiter = new RenderLimiter(limits())
    const a = await limiter.acquire('p')
    await limiter.acquire('p')
    const third = limiter.acquire('p')
    expect(await pending(third)).toBe(true)
    expect(limiter.queued).toBe(1)
    // Another principal has its own count, and is not held behind p's wait.
    ;(await limiter.acquire('q'))()
    a()
    a() // releasing twice counts once
    const release = await third
    expect(limiter.queued).toBe(0)
    release()
  })

  it('a principal override and the global bound', async () => {
    const limiter = new RenderLimiter(limits({ concurrent: 1, global: 3, overrides: { big: 3 } }))
    await limiter.acquire('big')
    await limiter.acquire('big')
    const small = await limiter.acquire('small')
    // big is under its own 3, but every principal's together is at the global 3.
    const fourth = limiter.acquire('big')
    expect(await pending(fourth)).toBe(true)
    small()
    await fourth
  })

  it('reads the limits at runtime: a raised one starts a waiting render', async () => {
    vi.useFakeTimers()
    try {
      let current = limits({ concurrent: 1 })
      const limiter = new RenderLimiter(async () => current)
      await limiter.acquire('p')
      let started = false
      const second = limiter.acquire('p').then(() => (started = true))
      await vi.advanceTimersByTimeAsync(RECHECK_MS)
      expect(started).toBe(false)
      current = limits({ concurrent: 2 })
      await vi.advanceTimersByTimeAsync(RECHECK_MS)
      await second
      expect(started).toBe(true)
    } finally {
      vi.useRealTimers()
    }
  })

  it('a waiting render ends when its caller aborts, and frees its place', async () => {
    const limiter = new RenderLimiter(limits({ concurrent: 1 }))
    const first = await limiter.acquire('p')
    const controller = new AbortController()
    const waiting = limiter.acquire('p', { signal: controller.signal })
    const after = limiter.acquire('p')
    controller.abort(new Error('stopped'))
    await expect(waiting).rejects.toThrow('stopped')
    first()
    await after
    expect(limiter.queued).toBe(0)
  })

  it('a failed settings read keeps the last limits', async () => {
    let fail = false
    const limiter = new RenderLimiter(async () => {
      if (fail) throw new Error('db down')
      return limits({ concurrent: 3 })
    })
    ;(await limiter.acquire('p'))()
    fail = true
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    await limiter.acquire('p')
    await limiter.acquire('p')
    await limiter.acquire('p')
    expect(limiter.limits.concurrent).toBe(3)
    warn.mockRestore()
  })

  it('the cap per window refuses only when set', async () => {
    let now = 0
    const limiter = new RenderLimiter(limits({ concurrent: 5, perWindow: 3 }), () => now)
    for (let i = 0; i < 3; i++) (await limiter.acquire('p'))()
    await expect(limiter.acquire('p')).rejects.toThrow(/3 renders in the last .* Try again in 1s/)
    now = 1001
    ;(await limiter.acquire('p'))()
    const unbounded = new RenderLimiter(limits({ concurrent: 5 }))
    for (let i = 0; i < 50; i++) (await unbounded.acquire('p'))()
  })

  it('a burst cannot queue past the window cap: renders waiting count against it', async () => {
    const limiter = new RenderLimiter(limits({ concurrent: 1, perWindow: 3, windowMs: 60_000 }))
    const first = await limiter.acquire('p')
    const queued = [limiter.acquire('p'), limiter.acquire('p')]
    await expect(limiter.acquire('p')).rejects.toThrow(/3 renders in the last 1 min \(2 of them still waiting for a slot\)/)
    expect(limiter.queued).toBe(2)
    first()
    ;(await queued[0]!)()
    ;(await queued[1]!)()
    // Three started in the window: the fourth is still refused once nothing waits.
    await expect(limiter.acquire('p')).rejects.toThrow(/most allowed/)
  })

  it('a render waiting when the window cap is lowered is refused at its start, not started past it', async () => {
    let current = limits({ concurrent: 1, windowMs: 60_000 })
    const limiter = new RenderLimiter(async () => current)
    const first = await limiter.acquire('p')
    const waiting = limiter.acquire('p')
    expect(await pending(waiting)).toBe(true)
    current = limits({ concurrent: 1, perWindow: 1, windowMs: 60_000 })
    first()
    await expect(waiting).rejects.toThrow(/most allowed/)
    expect(limiter.queued).toBe(0)
  })

  it('bounds how many of one principal\'s renders may wait', async () => {
    const limiter = new RenderLimiter(limits({ concurrent: 1 }))
    await limiter.acquire('p')
    const waiting = Array.from({ length: MAX_QUEUED_PER_PRINCIPAL }, () => limiter.acquire('p'))
    await expect(limiter.acquire('p')).rejects.toThrow(/already waiting for a slot, the most that may queue/)
    expect(limiter.queued).toBe(MAX_QUEUED_PER_PRINCIPAL)
    // Another principal still queues and runs.
    ;(await limiter.acquire('q'))()
    void Promise.allSettled(waiting)
  })

  it('anonymous MCP sessions share one count: a new session buys no new slots', async () => {
    expect(renderPrincipal({ id: 'anonymous:s1', kind: 'anonymous' })).toBe('anonymous')
    expect(renderPrincipal({ id: 'token:t1', kind: 'bearer' })).toBe('token:t1')
    const schema = { groups: [], parameters: [] }
    const id = 'f'.repeat(32)
    let status = 'running'
    const { client, seen } = backend((s) =>
      s.path.endsWith('/schema')
        ? Response.json(schema)
        : s.path.endsWith('/render')
          ? Response.json({ job_id: id }, { status: 202 })
          : Response.json({ id, status, log_tail: [] }),
    )
    const limiter = new RenderLimiter(limits({ concurrent: 1, windowMs: 60_000, holdMs: 60_000 }))
    const anonymous = (session: string) =>
      ctx(client, {
        renderLimiter: limiter,
        renderWaitMs: 0,
        pollIntervalMs: 1,
        principal: { id: `anonymous:${session}`, kind: 'anonymous', tiers: tiersUpTo('write') },
      })
    expect(firstText(await runTool(tool('render_model'), { slug: 'plate' }, anonymous('s1')))).toMatchObject({ status: 'running' })
    const second = runTool(tool('render_model'), { slug: 'plate' }, anonymous('s2'))
    expect(await pending(second)).toBe(true)
    expect(seen.filter((s) => s.path.endsWith('/render'))).toHaveLength(1)
    expect(limiter.queued).toBe(1)
    status = 'done'
    expect((await second).isError).toBeFalsy()
  })

  it('keeps only the window\'s starts (#774)', async () => {
    let now = 0
    const limiter = new RenderLimiter(limits({ perWindow: 3 }), () => now)
    const starts = () => (limiter as unknown as { started: Map<string, number[]> }).started.get('p')
    ;(await limiter.acquire('p'))()
    now = 1500
    const release = await limiter.acquire('p')
    expect(starts()).toEqual([1500])
    release()
  })

  it('drops a principal once nothing is in flight or in its window', async () => {
    let now = 0
    const limiter = new RenderLimiter(limits(), () => now)
    ;(await limiter.acquire('anonymous:s1'))()
    const held = await limiter.acquire('anonymous:s2')
    expect(limiter.tracked).toBe(2)
    now = 1500
    ;(await limiter.acquire('anonymous:s3'))()
    // s1 has nothing left; s2 is still in flight.
    expect(limiter.tracked).toBe(2)
    held()
    now = 3000
    ;(await limiter.acquire('anonymous:s4'))()
    expect(limiter.tracked).toBe(1)
  })

  it('the stored setting: each field out of range keeps its default', () => {
    expect(renderLimitsFrom(undefined)).toEqual(RENDER_LIMITS)
    expect(
      renderLimitsFrom({
        per_principal: 8,
        global: 'x',
        per_window: 30,
        overrides: [{ principal: 'token:a', concurrent: 12 }, { principal: '', concurrent: 2 }, { principal: 'b', concurrent: 0 }],
      }),
    ).toEqual({ ...RENDER_LIMITS, concurrent: 8, perWindow: 30, overrides: { 'token:a': 12 } })
    expect(renderLimitsSetting(renderLimitsFrom({ per_principal: 6, global: 10, per_window: 0, overrides: [] }))).toEqual({
      per_principal: 6,
      global: 10,
      per_window: 0,
      overrides: [],
    })
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
    const limiter = new RenderLimiter(limits({ concurrent: 1, windowMs: 60_000, holdMs: 60_000 }))
    const c = ctx(client, { renderLimiter: limiter, renderWaitMs: 0, pollIntervalMs: 1 })
    const first = await runTool(tool('render_model'), { slug: 'plate' }, c)
    expect(firstText(first)).toMatchObject({ status: 'running', note: expect.stringContaining('still rendering') })
    // The second waits for the first's slot instead of being refused, then renders.
    const second = runTool(tool('render_model'), { slug: 'plate' }, c)
    // Polled past its first answer: the hold's poll once threw on a fractional
    // AbortSignal.timeout and freed the slot at once.
    await vi.waitFor(() => expect(seen.filter((s) => s.path.startsWith('/api/v1/jobs/')).length).toBeGreaterThan(2))
    expect(await pending(second)).toBe(true)
    expect(seen.filter((s) => s.path.endsWith('/render'))).toHaveLength(1)
    // The background poll sees the job settle and frees the slot.
    status = 'done'
    expect((await second).isError).toBeFalsy()
    expect(seen.filter((s) => s.path.endsWith('/render'))).toHaveLength(2)
  })

  it('render_model refuses past the window cap without calling the backend', async () => {
    const schema = { groups: [], parameters: [] }
    const job = { id: 'd'.repeat(32), status: 'done', log_tail: [] }
    const { client, seen } = backend((s) =>
      s.path.endsWith('/schema') ? Response.json(schema) : s.path.endsWith('/render') ? Response.json({ job_id: job.id }, { status: 202 }) : Response.json(job),
    )
    const limiter = new RenderLimiter(limits({ concurrent: 1, perWindow: 1, windowMs: 60_000, holdMs: 60_000 }))
    const c = ctx(client, { renderLimiter: limiter })
    const first = await runTool(tool('render_model'), { slug: 'plate' }, c)
    expect(first.isError, JSON.stringify(first)).toBeFalsy()
    const second = await runTool(tool('render_model'), { slug: 'plate' }, c)
    expect(second.isError).toBe(true)
    expect(String(firstText(second))).toContain('renders in the last 1 min')
    expect(seen.filter((s) => s.path.endsWith('/render'))).toHaveLength(1)
  })
})
