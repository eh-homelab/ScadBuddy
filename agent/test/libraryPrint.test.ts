import { http, HttpResponse } from 'msw'
import { setupServer } from 'msw/node'
import { afterAll, afterEach, beforeAll, describe, expect, it } from 'vitest'
import { tiersUpTo } from '../src/auth/principal.js'
import { NOT_A_TOOL } from '../src/tools/coverage.js'
import { ALL_TOOLS } from '../src/tools/index.js'
import { runTool, type Tool, type ToolContext } from '../src/tools/registry.js'
import { BACKEND, firstText, services } from './helpers/mcp.js'

// #1756 (epic #1749, rows A1-A3): a library file prints through the same tools as an
// output; only the route differs. A call names one source: `output_id` or
// `library_file_id`.

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

const FILE = 42
const LIB = `${BACKEND}/api/v1/print/library/${FILE}`
const OUT = '0123456789abcdef0123456789abcdef'

const SOURCE_TOOLS = [
  'get_print_choices',
  'get_print_filaments',
  'get_print_progress',
  'print_output',
  'file_output_under_project',
  'get_output_plates',
  'get_output_preview',
  'download_3mf',
]

describe('one source per call', () => {
  it.each(SOURCE_TOOLS)('%s refuses neither and both sources before anything runs', async (name) => {
    const t = tool(name)
    expect(() => t.parse({})).toThrow(/output_id or library_file_id/)
    expect(() => t.parse({ output_id: OUT, library_file_id: FILE })).toThrow(/output_id or library_file_id/)
    expect(t.parse({ library_file_id: FILE })).toMatchObject({ library_file_id: FILE })
  })

  it('refuses an outward call with no source at the gate, so nothing is approved', async () => {
    const result = await runTool(tool('print_output'), { printer_id: 1 }, ctx())
    expect(result.isError).toBe(true)
  })

  it('remember_model_print_choices takes a slug or a library file, one of them', () => {
    const t = tool('remember_model_print_choices')
    expect(() => t.parse({})).toThrow(/slug or library_file_id/)
    expect(() => t.parse({ slug: 'lid', library_file_id: FILE })).toThrow(/slug or library_file_id/)
  })
})

describe('the library routes', () => {
  it('reads the choices, filaments and progress of a library file', async () => {
    const asked: string[] = []
    server.use(
      http.get(`${LIB}/choices`, ({ request }) => {
        asked.push(`choices ${new URL(request.url).search}`)
        return HttpResponse.json({ printer_id: 1 })
      }),
      http.get(`${LIB}/filaments`, ({ request }) => {
        asked.push(`filaments ${new URL(request.url).search}`)
        return HttpResponse.json({ slots: [] })
      }),
      http.get(`${LIB}/progress`, () => {
        asked.push('progress')
        return HttpResponse.json({ settled: true })
      }),
    )
    expect(firstText(await tool('get_print_choices').execute({ library_file_id: FILE, printer_id: 3 }, ctx()))).toEqual({ printer_id: 1 })
    await tool('get_print_filaments').execute({ library_file_id: FILE, plate_id: 2 }, ctx())
    expect(firstText(await tool('get_print_progress').execute({ library_file_id: FILE }, ctx()))).toEqual({ settled: true })
    expect(asked).toEqual(['choices ?printer_id=3', 'filaments ?plate_id=2', 'progress'])
  })

  it('remembers choices for a library file', async () => {
    let body: unknown
    server.use(
      http.put(`${LIB}/choices`, async ({ request }) => {
        body = await request.json()
        return HttpResponse.json({})
      }),
    )
    const result = await tool('remember_model_print_choices').execute({ library_file_id: FILE, tier: 'fine' }, ctx())
    expect(result.isError).toBeFalsy()
    expect(body).toEqual({ printer_id: null, filament_plan: [], nozzles: [], tier: 'fine', process_name: null })
  })

  it('files a library print under a project', async () => {
    let body: unknown
    server.use(
      http.post(`${LIB}/project`, async ({ request }) => {
        body = await request.json()
        return HttpResponse.json({ project_id: 7, queue_item_ids: [9], archive_ids: [] })
      }),
    )
    const t = tool('file_output_under_project')
    const result = await t.execute({ library_file_id: FILE, project_id: 7 }, ctx())
    expect(firstText(result)).toMatchObject({ project_id: 7 })
    expect(body).toEqual({ project_id: 7, queue_item_ids: [] })
    expect(t.summarize({ library_file_id: FILE, project_id: 7 })).toBe(`File library file ${FILE}'s prints under Bambuddy project 7`)
  })

  it("reads a library file's plates, preview and file", async () => {
    let plate: string | null = null
    server.use(
      http.get(`${LIB}/plates`, () => HttpResponse.json([{ index: 1, has_thumbnail: true, name: null }])),
      http.get(`${LIB}/preview.glb`, ({ request }) => {
        plate = new URL(request.url).searchParams.get('plate')
        return new HttpResponse(new Uint8Array([1, 2]), { headers: { 'content-type': 'model/gltf-binary' } })
      }),
      http.get(`${LIB}/file`, () =>
        new HttpResponse(new Uint8Array([3, 4]), { headers: { 'content-type': 'application/octet-stream' } }),
      ),
    )
    expect(firstText(await tool('get_output_plates').execute({ library_file_id: FILE }, ctx()))).toEqual([
      { index: 1, has_thumbnail: true, name: null },
    ])
    const preview = await tool('get_output_preview').execute({ library_file_id: FILE, plate: 2 }, ctx({ maxInlineBytes: 16 }))
    expect(plate).toBe('2')
    expect(preview.content).toContainEqual({
      type: 'resource',
      resource: { uri: `scadbuddy://print/library/${FILE}/preview.glb`, mimeType: 'model/gltf-binary', blob: 'AQI=' },
    })
    const file = await tool('download_3mf').execute({ library_file_id: FILE }, ctx({ maxInlineBytes: 16 }))
    expect(file.content).toContainEqual({
      type: 'resource',
      resource: { uri: `scadbuddy://print/library/${FILE}/file`, mimeType: 'application/octet-stream', blob: 'AwQ=' },
    })
  })

  it('lists one folder of the library, paged', async () => {
    let query = ''
    server.use(
      http.get(`${BACKEND}/api/v1/print/library`, ({ request }) => {
        query = new URL(request.url).search
        return HttpResponse.json({
          folder_id: 3,
          all: true,
          folders: [{ id: 3, name: 'Parts', parent_id: null, depth: 0 }],
          files: [
            { id: 1, filename: 'a.3mf', file_type: '3mf', printable: true },
            { id: 2, filename: 'b.stl', file_type: 'stl', printable: true },
          ],
          hidden: 0,
        })
      }),
    )
    const result = firstText(await tool('list_library').execute({ folder_id: 3, all: true, limit: 1 }, ctx())) as {
      files: unknown[]
      folders: unknown[]
      next_cursor: string | null
      total: number
    }
    expect(query).toBe('?folder_id=3&all=true')
    expect(result.files).toEqual([{ id: 1, filename: 'a.3mf', file_type: '3mf', printable: true }])
    expect(result.folders).toHaveLength(1)
    expect(result.total).toBe(2)
    expect(result.next_cursor).toBeTruthy()
  })
})

describe('print_output on a library file', () => {
  const RUN = 'fedcba9876543210fedcba9876543210'
  const running = { id: RUN, output_id: `library:${FILE}`, subject: `library:${FILE}`, status: 'running', created_at: '' }

  it("fills omitted choices from the file's own choices, and keeps print_sequence (#907)", async () => {
    let body: Record<string, unknown> | undefined
    server.use(
      http.get(`${LIB}/choices`, () =>
        HttpResponse.json({
          printer_id: 1,
          bed_type: 'Cool Plate',
          filaments: { slots: [{ slot_id: 1, colour_matches: [10] }], suggested: [{ slot_id: 1, spool_id: 10 }] },
          model_choices: { tier: 'fine' },
        }),
      ),
      http.post(`${LIB}/run`, async ({ request }) => {
        body = (await request.json()) as Record<string, unknown>
        return HttpResponse.json(running, { status: 202 })
      }),
      http.get(`${BACKEND}/api/v1/print/runs/${RUN}`, () =>
        HttpResponse.json({ ...running, status: 'succeeded', result: { queue_item_ids: [9] } }),
      ),
    )
    const result = await tool('print_output').execute({ library_file_id: FILE, print_sequence: 'by object' }, ctx())
    expect(result.isError).toBeFalsy()
    expect(firstText(result)).toMatchObject({ id: RUN, status: 'succeeded' })
    expect(body).toMatchObject({
      printer_id: 1,
      print_sequence: 'by object',
      filament_plan: { slots: [{ slot_id: 1, spool_id: 10 }] },
      choices: { tier: 'fine', bed_type: 'Cool Plate' },
    })
  })

  it('names the library file in its approval line and title', () => {
    const t = tool('print_output')
    expect(t.summarize({ library_file_id: FILE })).toMatch(new RegExp(`^Print library file ${FILE}: 1 copy of plate 1`))
    expect(t.title({ library_file_id: FILE, copies: 2 })).toBe(`Print library file ${FILE} × 2`)
  })

  it('reads the filament step for another plate from the library route', async () => {
    let asked: URLSearchParams | undefined
    server.use(
      http.get(`${LIB}/choices`, () => HttpResponse.json({ printer_id: 1, bed_type: 'Cool Plate', filaments: { slots: [] } })),
      http.get(`${LIB}/filaments`, ({ request }) => {
        asked = new URL(request.url).searchParams
        return HttpResponse.json({ slots: [] })
      }),
      http.post(`${LIB}/run`, () => HttpResponse.json({ ...running, status: 'succeeded' }, { status: 200 })),
    )
    await tool('print_output').execute({ library_file_id: FILE, plate_id: 2 }, ctx())
    expect(asked?.get('plate_id')).toBe('2')
  })
})

describe('coverage (#1756)', () => {
  it('leaves no library print route without a tool', () => {
    const left = NOT_A_TOOL.map((entry) => entry.operation as string)
    for (const route of [
      'GET /api/v1/print/library',
      'GET /api/v1/print/library/{file_id}/plates',
      'GET /api/v1/print/library/{file_id}/choices',
      'PUT /api/v1/print/library/{file_id}/choices',
      'GET /api/v1/print/library/{file_id}/filaments',
      'POST /api/v1/print/library/{file_id}/run',
      'GET /api/v1/print/library/{file_id}/progress',
      'POST /api/v1/print/library/{file_id}/project',
      'GET /api/v1/print/library/{file_id}/preview.glb',
      'GET /api/v1/print/library/{file_id}/file',
    ]) {
      expect(left, route).not.toContain(route)
    }
  })
})
