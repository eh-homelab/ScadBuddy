import { Client } from '@modelcontextprotocol/sdk/client/index.js'
import { InMemoryTransport } from '@modelcontextprotocol/sdk/inMemory.js'
import { http, HttpResponse } from 'msw'
import { setupServer } from 'msw/node'
import { afterAll, afterEach, beforeAll, describe, expect, it } from 'vitest'
import { markUntrusted } from '../src/safety/untrusted.js'
import { EXTRACTORS, RECORDED_WHEN_FAILED, resultJson, TOUCHES_NOTHING, type TouchedCall, touchesOf } from '../src/sessions/touched.js'
import { harnessTools } from '../src/tools/harness.js'
import { ALL_TOOLS } from '../src/tools/index.js'
import { SERVER_NAME } from '../src/tools/projections.js'
import { harnessPrincipal } from '../src/auth/principal.js'
import { z } from 'zod'
import { defineTool, ERROR_DETAIL_SOURCE, json, type Risk, runToolWithOutcome } from '../src/tools/registry.js'
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
    expect([...TOUCHES_NOTHING].filter((name) => !names.has(name))).toEqual([])
    expect([...RECORDED_WHEN_FAILED].filter((name) => !names.has(name))).toEqual([])
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

  it('never takes the restored revision as the parent of the restore', () => {
    // `commit` is what was restored FROM; the new commit's parent is not in the call.
    expect(touches('restore_version', { slug: 'box', commit: C1 }, { slug: 'box', version: C2 })).toEqual([
      { type: 'revision', id: C2, action: 'created', model: 'box', before: null, after: C2 },
    ])
  })

  it('leaves the parent null where the call names none', () => {
    for (const name of ['write_source_file', 'delete_source_file', 'set_readme', 'set_model_thumbnail']) {
      expect(touches(name, { slug: 'box' }, { slug: 'box', version: C2 })[0]).toMatchObject({ type: 'revision', before: null })
    }
  })

  it('records a revision tool with no new commit in its answer as a model change, never its slug as a revision', () => {
    expect(touches('update_source', { slug: 'box', base: C1 }, { slug: 'box' })).toEqual([
      { type: 'model', id: 'box', action: 'modified', model: 'box' },
    ])
    expect(touchesOf({ name: 'set_readme', risk: 'write' }, { slug: 'box' }, { content: [{ type: 'text', text: 'not json' }] })).toEqual([
      { type: 'model', id: 'box', action: 'modified', model: 'box' },
    ])
  })

  it("records an upstream merge as a revision, and dismiss or detach as a model change", () => {
    expect(
      touches('update_from_upstream', { slug: 'mine', action: 'merge' }, { model: { slug: 'mine', version: C2 }, taken: [] }),
    ).toEqual([{ type: 'revision', id: C2, action: 'created', model: 'mine', before: null, after: C2 }])
    expect(touches('update_from_upstream', { slug: 'mine', action: 'detach' }, { slug: 'mine', version: C1 })).toEqual([
      { type: 'model', id: 'mine', action: 'modified', model: 'mine', after: C1 },
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

  it('records a fetched asset as an upload does', () => {
    expect(touches('fetch_asset', { slug: 'box', url: 'https://x.test/a.png' }, { id: 'f'.repeat(64), kind: 'image' }, 'outward')).toEqual([
      { type: 'asset', id: 'f'.repeat(64), action: 'created', model: 'box' },
    ])
  })

  it("records a library pin as the model's revision and the library, by name", () => {
    const record = { slug: 'box', version: C2 }
    expect(touches('pin_library', { slug: 'box', name: 'BOSL2' }, record)).toEqual([
      { type: 'revision', id: C2, action: 'created', model: 'box', before: null, after: C2 },
      { type: 'library', id: 'BOSL2', action: 'created', model: 'box' },
    ])
    expect(touches('pin_library_from_url', { slug: 'box', name: 'lib', url: 'https://g.test/x.git', ref: 'v1' }, record, 'outward')[1]).toEqual(
      { type: 'library', id: 'lib', action: 'created', model: 'box' },
    )
    for (const name of ['repin_library', 'repin_library_from_pinned_url']) {
      expect(touches(name, { slug: 'box', name: 'BOSL2' }, record)[1], name).toEqual({ type: 'library', id: 'BOSL2', action: 'modified', model: 'box' })
    }
    expect(touches('unpin_library', { slug: 'box', name: 'BOSL2' }, record)[1]).toEqual({ type: 'library', id: 'BOSL2', action: 'deleted', model: 'box' })
    // The shared checkout, which no model owns.
    expect(touches('remove_library_checkout', { name: 'BOSL2', commit: C1 }, null, 'outward')).toEqual([
      { type: 'library', id: 'BOSL2', action: 'deleted', model: null, before: C1 },
    ])
  })

  it('records a font installed', () => {
    expect(touches('install_font', { family: 'Lobster Two' }, { family: 'Lobster Two', files: [] })).toEqual([
      { type: 'font', id: 'Lobster Two', action: 'created' },
    ])
  })

  it('records settings and remembered choices by what they are for', () => {
    expect(touches('set_print_options', { scope: 'global', options: {} }, {}, 'outward')).toEqual([
      { type: 'setting', id: 'print_options:global', action: 'modified', model: null },
    ])
    expect(touches('set_print_options', { scope: 'printer', key: '3', options: {} }, {}, 'outward')).toEqual([
      { type: 'setting', id: 'print_options:printer:3', action: 'modified', model: null },
    ])
    expect(touches('set_print_options', { scope: 'model', key: 'box', options: {} }, {}, 'outward')).toEqual([
      { type: 'setting', id: 'print_options:model:box', action: 'modified', model: 'box' },
    ])
    expect(touches('remember_model_print_choices', { slug: 'box', tier: 'fast' }, {})).toEqual([
      { type: 'setting', id: 'print_choices:box', action: 'modified', model: 'box' },
    ])
    expect(touches('remember_last_project', { project_id: 4 }, { project_id: 4 })).toEqual([
      { type: 'setting', id: 'last_project', action: 'modified', after: '4' },
    ])
    expect(touches('remember_printer_bed_type', { printer_id: 2, bed_type: 'pei' }, {})).toEqual([
      { type: 'setting', id: 'bed_type:2', action: 'modified' },
    ])
  })

  it("records Bambuddy projects, files and a print's timelapse", () => {
    expect(touches('create_print_project', { name: 'Bins' }, { id: 9, name: 'Bins' }, 'outward')).toEqual([
      { type: 'project', id: '9', action: 'created' },
    ])
    // Linking an existing project changes it; it makes none.
    expect(touches('create_print_project', { project_id: 9 }, { id: 9 }, 'outward')).toEqual([
      { type: 'project', id: '9', action: 'modified' },
    ])
    expect(touches('send_to_bambuddy', { output_id: 'o1' }, { library_file_id: 31, filename: 'a.3mf' }, 'outward')).toEqual([
      { type: 'bambuddy_file', id: '31', action: 'created', before: 'o1' },
    ])
    expect(
      touches('file_output_in_project_folder', { output_id: 'o1', project_id: 9 }, { library_file_id: 31, project_id: 9, created: false }, 'outward'),
    ).toEqual([
      { type: 'bambuddy_file', id: '31', action: 'modified', before: 'o1' },
      { type: 'project', id: '9', action: 'modified' },
    ])
    expect(
      touches('file_output_in_project_folder', { output_id: 'o1', project_id: 9 }, { library_file_id: 32, project_id: 9, created: true }, 'outward')[0],
    ).toEqual({ type: 'bambuddy_file', id: '32', action: 'created', before: 'o1' })
    expect(
      touches('file_output_under_project', { output_id: 'o1' }, { project_id: 9, queue_item_ids: [12], archive_ids: [] }, 'outward'),
    ).toEqual([{ type: 'project', id: '9', action: 'modified', before: 'o1' }])
    expect(touches('pull_print_timelapse', { archive_id: 7, filename: 't.mp4' }, null, 'outward')).toEqual([
      { type: 'print_archive', id: '7', action: 'modified' },
    ])
  })

  it("records the output the user's tab generated, and nothing for what changes only the open page", () => {
    expect(touches('browser_generate', {}, { output: { id: 'o9', name: null } })).toEqual([{ type: 'output', id: 'o9', action: 'created' }])
    expect(touches('browser_generate', {}, null)).toEqual([])
    for (const name of [
      'browser_navigate',
      'browser_open_model',
      'browser_set_param',
      'browser_set_params',
      'browser_reset_param',
      'browser_select_plate',
      'browser_open_print_dialog',
      'browser_replace_range',
      'browser_set_field',
    ]) {
      expect(touches(name, {}, {}), name).toEqual([])
    }
    // A click or a fill can press anything, Save included: what it changed is not known.
    expect(touches('browser_fill', {}, {})).toEqual([{ type: 'unclassified', id: null, action: 'modified' }])
  })

  it('leaves only the browser click and fill unclassified', () => {
    const open = ALL_TOOLS.filter((t) => t.risk !== 'read' && !EXTRACTORS[t.name] && !TOUCHES_NOTHING.has(t.name))
    expect(open.map((t) => t.name).sort()).toEqual(['browser_click', 'browser_fill'])
  })

  it('records a failed render_model (its job was made), and no other failed call', () => {
    const failed = (name: string, input: Record<string, unknown>, data: unknown) =>
      touchesOf({ name, risk: 'write' }, input, { ...result(data, name), isError: true }, false)
    expect(failed('render_model', { slug: 'box' }, { job_id: 'j1', status: 'failed', model_version: C1 })).toEqual([
      { type: 'render_job', id: 'j1', action: 'created', model: 'box', after: C1 },
    ])
    // apply_patch's conflict answer is JSON too, but nothing was written.
    expect(failed('apply_patch', { slug: 'box', base: C1 }, { status: 'conflict', base: C1, current: C2 })).toEqual([])
    expect(failed('set_print_options', { scope: 'global' }, {})).toEqual([])
  })

  it('records nothing it cannot name, rather than a row with no id', () => {
    expect(touches('save_preset', { slug: 'box' }, 'not an object')).toEqual([])
    expect(touches('update_source', {}, {})).toEqual([])
  })

  it('records a write with no extractor as unclassified, and a read with none as nothing', () => {
    expect(touches('browser_click', { ref: 'e1' }, {})).toEqual([{ type: 'unclassified', id: null, action: 'modified' }])
    expect(touches('get_model', { slug: 'box' }, { slug: 'box' }, 'read')).toEqual([])
    // Session control changes no recorded resource: no unclassified noise.
    expect(touches('sessions_send', { session_id: 's', text: 'hi' }, { turn_id: 't' })).toEqual([])
    expect(touches('sessions_approve', { approval_id: 'a' }, {}, 'outward')).toEqual([])
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

  it('reports a failed call as failed, and nothing outside a session', async () => {
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
    // The failed call reaches the sink as failed, which records nothing for it.
    expect(seen.map((c) => [c.sessionId, c.tool.name, c.ok])).toEqual([['sess-1', 'update_source', false]])
    expect(touchesOf(seen[0]!.tool, seen[0]!.input, seen[0]!.result, seen[0]!.ok)).toEqual([])
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

  it('records a render whose save_output failed: the job, with the reason in the result', async () => {
    backend.use(
      http.get(`${BACKEND}/api/v1/models/box/schema`, () => HttpResponse.json({ groups: [], parameters: [] })),
      http.post(`${BACKEND}/api/v1/models/box/render`, () =>
        HttpResponse.json({ job_id: 'j9', status_url: '/api/v1/jobs/j9' }, { status: 202 }),
      ),
      http.get(`${BACKEND}/api/v1/jobs/j9`, () => HttpResponse.json({ id: 'j9', slug: 'box', status: 'done', params: {} })),
      http.post(`${BACKEND}/api/v1/models/box/outputs`, () => HttpResponse.json({ detail: 'disk full' }, { status: 507 })),
    )
    const seen: TouchedCall[] = []
    const mcp = await connect('sess-4', seen)
    const answer = await mcp.callTool({ name: 'render_model', arguments: { slug: 'box', save_output: true } })
    await mcp.close()
    expect(answer.isError).toBe(true)
    const answered = resultJson(answer as never) as { output_error: string }
    expect(answered).toMatchObject({ job_id: 'j9', output: null, output_error: expect.stringContaining('HTTP 507') })
    // The model still sees why, the backend's detail wrapped as untrusted under the error-detail source.
    expect(answered.output_error).toContain('disk full')
    expect(answered.output_error).toContain(ERROR_DETAIL_SOURCE)
    expect(seen).toHaveLength(1)
    expect(seen[0]!.ok).toBe(false)
    expect(touchesOf(seen[0]!.tool, seen[0]!.input, seen[0]!.result, seen[0]!.ok)).toEqual([
      { type: 'render_job', id: 'j9', action: 'created', model: 'box', after: null },
    ])
  })

  it('answers the call unchanged when the sink throws', async () => {
    backend.use(http.put(`${BACKEND}/api/v1/models/box/source`, () => HttpResponse.json({ slug: 'box', version: C2 })))
    const tool = ALL_TOOLS.find((t) => t.name === 'update_source')!
    const run = await runToolWithOutcome(tool, { slug: 'box', source: '' }, {
      ...services({ touched: { record: () => Promise.reject(new Error('sink broke')) } }),
      principal: harnessPrincipal({ kind: 'browser', id: 'browser', label: 'You' }),
      progress: async () => {},
      signal: new AbortController().signal,
      session: 'sess-5',
    })
    expect(run.outcome).toBe('ok')
    expect(resultJson(run.result)).toEqual({ slug: 'box', version: C2 })
  })

  it('records nothing for a failed call outside RECORDED_WHEN_FAILED, or a render that never started', async () => {
    backend.use(
      http.put(`${BACKEND}/api/v1/models/box/source`, () => HttpResponse.json({ detail: 'nope' }, { status: 422 })),
      http.get(`${BACKEND}/api/v1/models/box/schema`, () => HttpResponse.json({ groups: [], parameters: [] })),
      http.post(`${BACKEND}/api/v1/models/box/render`, () => HttpResponse.json({ detail: 'queue full' }, { status: 503 })),
    )
    const rows: unknown[] = []
    const ctx = {
      ...services({
        // As SessionResources does: what touchesOf yields is what is stored.
        touched: { record: async (c: TouchedCall) => void rows.push(...touchesOf(c.tool, c.input, c.result, c.ok ?? true)) },
      }),
      principal: harnessPrincipal({ kind: 'browser', id: 'browser', label: 'You' }),
      progress: async () => {},
      signal: new AbortController().signal,
      session: 'sess-6',
    }
    const find = (name: string) => ALL_TOOLS.find((t) => t.name === name)!
    expect((await runToolWithOutcome(find('update_source'), { slug: 'box', source: '' }, ctx)).outcome).toBe('error')
    expect((await runToolWithOutcome(find('render_model'), { slug: 'box' }, ctx)).outcome).toBe('error')
    expect(rows).toEqual([])
  })

  it("keeps a failed save's reason in render_model's audit detail", async () => {
    backend.use(
      http.get(`${BACKEND}/api/v1/models/box/schema`, () => HttpResponse.json({ groups: [], parameters: [] })),
      http.post(`${BACKEND}/api/v1/models/box/render`, () =>
        HttpResponse.json({ job_id: 'j8', status_url: '/api/v1/jobs/j8' }, { status: 202 }),
      ),
      http.get(`${BACKEND}/api/v1/jobs/j8`, () => HttpResponse.json({ id: 'j8', slug: 'box', status: 'done', params: {} })),
      http.post(`${BACKEND}/api/v1/models/box/outputs`, () => HttpResponse.json({ detail: 'disk full' }, { status: 507 })),
    )
    const run = await runToolWithOutcome(ALL_TOOLS.find((t) => t.name === 'render_model')!, { slug: 'box', save_output: true }, {
      ...services(),
      principal: harnessPrincipal({ kind: 'browser', id: 'browser', label: 'You' }),
      progress: async () => {},
      signal: new AbortController().signal,
    })
    expect(run.outcome).toBe('error')
    expect(run.detail).toContain('HTTP 507')
    expect(run.detail).toContain('disk full')
  })
})
