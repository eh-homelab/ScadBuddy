import { Client } from '@modelcontextprotocol/sdk/client/index.js'
import { InMemoryTransport } from '@modelcontextprotocol/sdk/inMemory.js'
import { http, HttpResponse } from 'msw'
import { setupServer } from 'msw/node'
import { afterAll, afterEach, beforeAll, describe, expect, it } from 'vitest'
import { markUntrusted } from '../src/safety/untrusted.js'
import { EXTRACTORS, resultJson, type TouchedCall, touchesOf } from '../src/sessions/touched.js'
import { harnessTools } from '../src/tools/harness.js'
import { ALL_TOOLS } from '../src/tools/index.js'
import { SERVER_NAME } from '../src/tools/projections.js'
import { harnessPrincipal } from '../src/auth/principal.js'
import { z } from 'zod'
import { defineTool, json, type Risk, runToolWithOutcome } from '../src/tools/registry.js'
import { BACKEND, services } from './helpers/mcp.js'

// What a session touched (#931, src/sessions/touched.ts): the per-tool
// extractors, and that the harness projection reports each successful call.
// test/touched.pg.test.ts stores and reads them.

const C1 = 'a'.repeat(40)
const C2 = 'b'.repeat(40)

function result(data: unknown, tool = 'x') {
  // As the projection sees it: re-encoded as untrusted data (registry.ts runToolWithOutcome).
  return markUntrusted(json(data), tool)
}

function touches(name: string, input: Record<string, unknown>, data: unknown, risk: Risk = 'write') {
  return touchesOf({ name, risk }, input, result(data, name))
}

describe('the extractor registry', () => {
  it('names only registered tools', () => {
    const names = new Set(ALL_TOOLS.map((t) => t.name))
    expect(Object.keys(EXTRACTORS).filter((name) => !names.has(name))).toEqual([])
  })

  it('reads a result out of its untrusted-data envelope', () => {
    expect(resultJson(result({ slug: 'box' }))).toEqual({ slug: 'box' })
    expect(resultJson({ content: [{ type: 'text', text: 'not json' }] })).toBeUndefined()
    expect(resultJson({ content: [] })).toBeUndefined()
  })
})

describe('extractors', () => {
  it('records a revision with its parent and new commit', () => {
    expect(touches('update_source', { slug: 'box', base: C1, source: '' }, { slug: 'box', version: C2 })).toEqual([
      { type: 'revision', id: C2, action: 'created', model: 'box', before: C1, after: C2 },
    ])
    expect(touches('apply_patch', { slug: 'box', base: C1 }, { slug: 'box', version: C2 })[0]).toMatchObject({
      before: C1,
      after: C2,
    })
    expect(touches('write_source_file', { slug: 'box', name: 'lib.scad' }, { slug: 'box', version: C2 })).toEqual([
      { type: 'revision', id: C2, action: 'created', model: 'box', before: null, after: C2 },
    ])
  })

  it('takes a restored revision as the parent of the restore', () => {
    expect(touches('restore_version', { slug: 'box', commit: C1 }, { slug: 'box', version: C2 })).toEqual([
      { type: 'revision', id: C2, action: 'created', model: 'box', before: C1, after: C2 },
    ])
  })

  it('records models created, duplicated, changed and deleted', () => {
    expect(touches('create_model', { name: 'Box' }, { slug: 'box', version: C1 })).toEqual([
      { type: 'model', id: 'box', action: 'created', model: 'box', before: null, after: C1 },
    ])
    expect(touches('duplicate_model', { slug: 'builtin:box', name: 'Mine' }, { slug: 'mine', version: C1 })[0]).toMatchObject({
      id: 'mine',
      before: 'builtin:box',
    })
    expect(touches('create_from_template', { from: 'blank', name: 'B' }, { slug: 'b' })[0]).toMatchObject({ before: null })
    expect(touches('update_model_details', { slug: 'box', name: 'Big box' }, { slug: 'box', version: C2 })).toEqual([
      { type: 'model', id: 'box', action: 'modified', model: 'box', after: C2 },
    ])
    expect(touches('delete_model', { slug: 'box' }, { deleted: 'box' }, 'outward')).toEqual([
      { type: 'model', id: 'box', action: 'deleted', model: 'box' },
    ])
  })

  it('records presets and assets', () => {
    expect(touches('save_preset', { slug: 'box', name: 'Tall' }, { id: 'p1', name: 'Tall' })).toEqual([
      { type: 'preset', id: 'p1', action: 'created', model: 'box' },
    ])
    expect(touches('duplicate_preset', { slug: 'box', preset_id: 'p1', name: 'Copy' }, { id: 'p2' })).toEqual([
      { type: 'preset', id: 'p2', action: 'created', model: 'box', before: 'p1' },
    ])
    expect(touches('update_preset', { slug: 'box', preset_id: 'p1' }, { id: 'p1' })[0]).toMatchObject({ action: 'modified' })
    expect(touches('delete_preset', { slug: 'box', preset_id: 'p1' }, { deleted: 'p1' }, 'outward')[0]).toMatchObject({
      action: 'deleted',
    })
    expect(touches('upload_asset', { slug: 'box', filename: 'a.svg' }, { id: 'sha', name: 'a.svg' })).toEqual([
      { type: 'asset', id: 'sha', action: 'created', model: 'box' },
    ])
  })

  it('records a render, and the output it saved', () => {
    const job = { job_id: 'j1', status: 'done', model_version: C1 }
    expect(touches('render_model', { slug: 'box', params: {} }, job)).toEqual([
      { type: 'render_job', id: 'j1', action: 'created', model: 'box', after: C1 },
    ])
    expect(touches('render_model', { slug: 'box' }, { ...job, output: { id: 'o1', slug: 'box' } })).toEqual([
      { type: 'render_job', id: 'j1', action: 'created', model: 'box', after: C1 },
      { type: 'output', id: 'o1', action: 'created', model: 'box' },
    ])
    expect(touches('save_output', { slug: 'box', job_id: 'j1' }, { id: 'o2', slug: 'box' })).toEqual([
      { type: 'output', id: 'o2', action: 'created', model: 'box' },
    ])
  })

  it('records prints, with what they printed', () => {
    // `print` is Bambuddy's queue item id from either tool, so the two are one id space.
    expect(
      touches('print_output', { output_id: 'o1' }, { id: 'r1', status: 'done', result: { queue_item_ids: [12, 13] } }, 'outward'),
    ).toEqual([
      { type: 'print_run', id: 'r1', action: 'created', before: 'o1' },
      { type: 'print', id: '12', action: 'created', before: 'o1' },
      { type: 'print', id: '13', action: 'created', before: 'o1' },
    ])
    // Still slicing: the run, and no queue item yet.
    expect(touches('print_output', { output_id: 'o1' }, { id: 'r1', status: 'running', result: null }, 'outward')).toEqual([
      { type: 'print_run', id: 'r1', action: 'created', before: 'o1' },
    ])
    expect(touches('print_again', { archive_id: 7 }, { queue_item_id: 12, printer_id: 1 }, 'outward')).toEqual([
      { type: 'print', id: '12', action: 'created', before: '7' },
    ])
  })

  it('records nothing it cannot name, rather than a row with no id', () => {
    expect(touches('save_preset', { slug: 'box' }, 'not an object')).toEqual([])
    expect(touches('update_source', {}, {})).toEqual([])
  })

  it('records a write with no extractor as unclassified, and a read with none as nothing', () => {
    expect(touches('set_print_options', {}, {})).toEqual([{ type: 'unclassified', id: null, action: 'modified' }])
    expect(touches('get_model', { slug: 'box' }, { slug: 'box' }, 'read')).toEqual([])
  })
})

describe('the harness projection', () => {
  const backend = setupServer()
  beforeAll(() => backend.listen({ onUnhandledRequest: 'error' }))
  afterEach(() => backend.resetHandlers())
  afterAll(() => backend.close())

  async function connect(session: string | undefined, seen: TouchedCall[]) {
    const wired = harnessTools(services({ touched: { record: async (call) => void seen.push(call) } }))
    const servers = wired.mcpServers({
      ...(session ? { id: session } : {}),
      owner: { kind: 'browser', id: 'browser', label: 'You' },
    })
    const [clientSide, serverSide] = InMemoryTransport.createLinkedPair()
    await servers[SERVER_NAME]!.instance.connect(serverSide)
    const mcp = new Client({ name: 'touched-test', version: '0' })
    await mcp.connect(clientSide)
    return mcp
  }

  it("reports a session's successful calls with the parsed input and the result", async () => {
    backend.use(http.put(`${BACKEND}/api/v1/models/box/source`, () => HttpResponse.json({ slug: 'box', version: C2 })))
    const seen: TouchedCall[] = []
    const mcp = await connect('sess-1', seen)
    await mcp.callTool({ name: 'update_source', arguments: { slug: 'box', source: 'cube(1);', base: C1 } })
    await mcp.close()
    expect(seen).toHaveLength(1)
    expect(seen[0]).toMatchObject({ sessionId: 'sess-1', tool: { name: 'update_source', risk: 'write' } })
    // Defaults applied, as the handler saw them.
    expect(seen[0]!.input).toMatchObject({ slug: 'box', base: C1, force: false })
    expect(touchesOf(seen[0]!.tool, seen[0]!.input, seen[0]!.result)).toEqual([
      { type: 'revision', id: C2, action: 'created', model: 'box', before: C1, after: C2 },
    ])
  })

  it('reports nothing for a failed call, or outside a session', async () => {
    backend.use(
      http.put(`${BACKEND}/api/v1/models/box/source`, () => HttpResponse.json({ detail: 'nope' }, { status: 422 })),
      http.put(`${BACKEND}/api/v1/models/ok/source`, () => HttpResponse.json({ slug: 'ok', version: C2 })),
    )
    const seen: TouchedCall[] = []
    const inSession = await connect('sess-1', seen)
    const failed = await inSession.callTool({ name: 'update_source', arguments: { slug: 'box', source: '' } })
    expect(failed.isError).toBe(true)
    await inSession.close()
    const outside = await connect(undefined, seen)
    await outside.callTool({ name: 'update_source', arguments: { slug: 'ok', source: '' } })
    await outside.close()
    expect(seen).toEqual([])
  })

  it('records from runToolWithOutcome itself, so a path that bypasses the projection still records', async () => {
    backend.use(http.put(`${BACKEND}/api/v1/models/box/source`, () => HttpResponse.json({ slug: 'box', version: C2 })))
    const seen: TouchedCall[] = []
    const tool = ALL_TOOLS.find((t) => t.name === 'update_source')!
    const ctx = {
      ...services({ touched: { record: async (call: TouchedCall) => void seen.push(call) } }),
      principal: harnessPrincipal({ kind: 'browser', id: 'browser', label: 'You' }),
      progress: async () => {},
      signal: new AbortController().signal,
    }
    await runToolWithOutcome(tool, { slug: 'box', source: '' }, { ...ctx, session: 'sess-2' })
    // Over /mcp there is no session: nothing to record against.
    await runToolWithOutcome(tool, { slug: 'box', source: '' }, ctx)
    expect(seen.map((c) => [c.sessionId, c.tool.name])).toEqual([['sess-2', 'update_source']])
  })

  it('records the tool confirm_action ran, by name, even without a lookup', async () => {
    const seen: TouchedCall[] = []
    // Stands in for confirm_action: reports the approved call it ran.
    const confirm = defineTool({
      name: 'confirm_action',
      description: 'test',
      input: z.object({}),
      risk: 'outward',
      approval: 'none',
      routes: [],
      handler: async (_args, ctx) => {
        ctx.report?.({ ran: { tool: 'delete_model', input: { slug: 'box' } } })
        return json({ deleted: 'box' })
      },
    })
    await runToolWithOutcome(confirm, {}, {
      ...services({ touched: { record: async (call: TouchedCall) => void seen.push(call) } }),
      principal: harnessPrincipal({ kind: 'browser', id: 'browser', label: 'You' }),
      progress: async () => {},
      signal: new AbortController().signal,
      session: 'sess-3',
    })
    expect(seen.map((c) => c.tool.name)).toEqual(['delete_model'])
    expect(touchesOf(seen[0]!.tool, seen[0]!.input, seen[0]!.result)).toEqual([
      { type: 'model', id: 'box', action: 'deleted', model: 'box' },
    ])
  })
})
