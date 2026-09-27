import { http, HttpResponse } from 'msw'
import { setupServer } from 'msw/node'
import { afterAll, afterEach, beforeAll, describe, expect, it } from 'vitest'
import { tiersUpTo } from '../src/auth/principal.js'
import { ALL_TOOLS } from '../src/tools/index.js'
import { PendingActionStore } from '../src/tools/pending.js'
import { runTool, type Tool, type ToolContext } from '../src/tools/registry.js'
import { redact } from '../src/tools/settings.js'
import { validateParams } from '../src/tools/validate.js'
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
    { name: 'logo', type: 'file', initial: '' },
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
      logo: 'logo.png',
      nope: 1,
    })
    expect(report.valid).toBe(false)
    expect(Object.fromEntries(report.issues.map((i) => [i.param, i.problem]))).toEqual({
      width: 'must be at most 100',
      count: 'must be a whole number',
      style: 'must be one of "round", "square"',
      label: 'must be at most 5 characters',
      colour: 'must be a colour: a #rrggbb hex value or a colour name',
      logo: 'must be an asset id from upload_asset (a SHA-256), or "" for none',
      nope: 'is not a parameter of this model',
    })
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
        return HttpResponse.json({ id: 'out-1' }, { status: 201 })
      }),
    )
    const waiting = await runTool(tool('render_model'), { slug: 'box' }, ctx({ renderWaitMs: 20 }))
    expect(firstText(waiting)).toMatchObject({ status: 'running', note: expect.stringContaining('get_render_job') })

    status = 'done'
    const done = await runTool(tool('render_model'), { slug: 'box', save_output: true, output_name: 'v1' }, ctx())
    expect(firstText(done)).toMatchObject({ status: 'done', output: { id: 'out-1' } })
    expect(saved).toEqual({ job_id: 'j', name: 'v1' })
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

describe('print_output (as it will run once approved, #258)', () => {
  it('checks eligibility, then runs the pipeline', async () => {
    let run: unknown
    server.use(
      http.post(`${BACKEND}/api/v1/print/outputs/out-1/eligibility`, () =>
        HttpResponse.json({ library_file_id: 5, reports: [{ pipeline_id: 3, report: { ok: true, issues: [] } }] }),
      ),
      http.post(`${BACKEND}/api/v1/print/outputs/out-1/run`, async ({ request }) => {
        run = await request.json()
        return HttpResponse.json({ route: 'pipeline' })
      }),
    )
    const result = await tool('print_output').execute({ output_id: 'out-1', pipeline_id: 3, copies: 2 }, ctx())
    expect(result.isError).toBeFalsy()
    expect(run).toMatchObject({ pipeline_id: 3, copies: 2, plate_id: 1, all_plates: false, force: false, options: {} })
  })

  it('stops on a blocking eligibility issue unless forced', async () => {
    server.use(
      http.post(`${BACKEND}/api/v1/print/outputs/out-1/eligibility`, () =>
        HttpResponse.json({ library_file_id: 5, reports: [{ pipeline_id: 3, report: { ok: false, issues: [{ kind: 'nozzle' }] } }] }),
      ),
    )
    const result = await tool('print_output').execute({ output_id: 'out-1', pipeline_id: 3 }, ctx())
    expect(result.isError).toBe(true)
    expect(firstText(result)).toMatchObject({ status: 'ineligible' })
  })

  it('passes a Bambuddy scope error through with its detail', async () => {
    server.use(
      http.post(`${BACKEND}/api/v1/outputs/out-1/send`, () =>
        HttpResponse.json(
          { title: 'Bambuddy API key lacks a scope', detail: 'The API key needs "Manage Library" to upload the 3MF' },
          { status: 403 },
        ),
      ),
    )
    const result = await runTool(tool('send_to_bambuddy'), { output_id: 'out-1' }, { ...ctx(), pending: new PendingActionStore() })
    // Through runTool it is only prepared …
    expect(firstText(result)).toMatchObject({ status: 'pending_approval' })
    // … and once executed, the scope error reaches the agent verbatim.
    const executed = await runTool({ ...tool('send_to_bambuddy'), gated: false }, { output_id: 'out-1' }, ctx())
    expect(executed.isError).toBe(true)
    expect(firstText(executed)).toContain('needs "Manage Library"')
  })
})

describe('invalid arguments', () => {
  it('come back as a tool error, not an exception', async () => {
    const result = await runTool(tool('get_model'), { slug: 'Not A Slug' }, ctx())
    expect(result.isError).toBe(true)
    expect(firstText(result)).toContain('invalid arguments')
  })
})
