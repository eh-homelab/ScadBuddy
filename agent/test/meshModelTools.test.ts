import { describe, expect, it } from 'vitest'
import { createBackendClient } from '../src/api/backend.js'
import { tiersUpTo } from '../src/auth/principal.js'
import { ALL_TOOLS } from '../src/tools/index.js'
import { runTool, type ToolContext } from '../src/tools/registry.js'
import { BACKEND, firstText, services } from './helpers/mcp.js'

// #2146: mesh models. Every write is a command (an Idempotency-Key, 202 followed).

type Seen = { method: string; path: string; body: unknown; key: string | null }

function backend() {
  const seen: Seen[] = []
  const client = createBackendClient(BACKEND, async (request) => {
    const r = request as Request
    const url = new URL(r.url)
    const type = r.headers.get('content-type') ?? ''
    const body = type.includes('json') ? await r.json() : type.includes('multipart') ? 'multipart' : undefined
    seen.push({ method: r.method, path: url.pathname, body, key: r.headers.get('Idempotency-Key') })
    if (r.method === 'GET' && url.pathname === '/api/v1/models/carrot/scene')
      return Response.json({ scene: { version: 1, objects: [{ id: 'o1' }] }, notes: [], hand_edited: false })
    return Response.json(
      { slug: 'carrot', name: 'Carrot', origin: 'user', version: 'a'.repeat(40), has_thumbnail: false, has_readme: false, updated_at: 'now' },
      { status: r.method === 'POST' && url.pathname.endsWith('from-mesh') ? 201 : 200 },
    )
  })
  return { seen, client }
}

function tool(name: string) {
  const found = ALL_TOOLS.find((t) => t.name === name)
  if (!found) throw new Error(`no tool ${name}`)
  return found
}

async function call(name: string, args: unknown, client: ReturnType<typeof backend>['client']) {
  const ctx: ToolContext = {
    ...services({ backend: client }),
    principal: { id: 'token:t1', kind: 'bearer', tiers: tiersUpTo('write') },
    progress: async () => {},
    signal: new AbortController().signal,
  }
  return runTool(tool(name), args, ctx)
}

describe('mesh model tools', () => {
  it('creates from a library file as a command', async () => {
    const { seen, client } = backend()
    await call('create_model_from_mesh', { name: 'Carrot', library_file_id: 688 }, client)
    expect(seen).toEqual([
      expect.objectContaining({ method: 'POST', path: '/api/v1/models/from-mesh', body: { name: 'Carrot', library_file_id: 688, description: '' } }),
    ])
    expect(seen[0]!.key).toBeTruthy()
  })

  it('creates from an output', async () => {
    const { seen, client } = backend()
    await call('create_model_from_mesh', { name: 'Carrot', output_id: 'a'.repeat(32) }, client)
    expect(seen[0]).toEqual(expect.objectContaining({ path: '/api/v1/models/from-mesh', body: { name: 'Carrot', output_id: 'a'.repeat(32), description: '' } }))
  })

  it('uploads base64 as multipart', async () => {
    const { seen, client } = backend()
    await call('create_model_from_mesh', { name: 'Carrot', filename: 'carrot.stl', content_base64: Buffer.from('solid x').toString('base64') }, client)
    expect(seen[0]).toEqual(expect.objectContaining({ path: '/api/v1/models/from-mesh/upload', body: 'multipart' }))
  })

  it('refuses no source, two sources, a missing filename and a wrong extension, sending nothing', async () => {
    const { seen, client } = backend()
    const b64 = Buffer.from('x').toString('base64')
    for (const args of [
      { name: 'X' },
      { name: 'X', library_file_id: 1, output_id: 'a'.repeat(32) },
      { name: 'X', content_base64: b64 },
      { name: 'X', content_base64: b64, filename: 'x.obj' },
    ]) {
      const result = await call('create_model_from_mesh', args, client)
      expect(JSON.stringify(firstText(result))).toMatch(/exactly one source|filename|\.3mf or \.stl/)
    }
    expect(seen).toEqual([])
  })

  it('takes a workspace file as the upload, and refuses one without a store', async () => {
    expect(Object.keys(tool('create_model_from_mesh').shape)).toEqual(expect.arrayContaining(['workspace_path', 'workspace_sha256']))
    const { seen, client } = backend()
    const result = await call('create_model_from_mesh', { name: 'X', workspace_path: 'downloads/x.stl', workspace_sha256: 'a'.repeat(64) }, client)
    expect(String(firstText(result))).toMatch(/needs the agent database/)
    expect(seen).toEqual([])
  })

  it('reads the scene', async () => {
    const { client } = backend()
    const result = await call('get_scene', { slug: 'carrot' }, client)
    expect(JSON.stringify(firstText(result))).toContain('"hand_edited":false')
  })

  it('sends edits in order as one command', async () => {
    const { seen, client } = backend()
    const edits = [
      { edit: 'split_at_plane', object: 'o1', point: [0, 0, 32], normal: [0, 0, 1], joint: { depth: 5 } },
      { edit: 'lay_flat', object: 'o2', normal: 'largest_flat' },
    ]
    await call('edit_scene', { slug: 'carrot', edits }, client)
    // The tool's schema fills the joint's defaults, as the backend's would.
    const sent = [{ ...edits[0], joint: { depth: 5, clearance: 0.2, key: 'flat', peg_on: 'above' } }, edits[1]]
    expect(seen[0]).toEqual(expect.objectContaining({ method: 'POST', path: '/api/v1/models/carrot/scene/edits', body: { edits: sent, drop_from_presets: false } }))
    expect(seen[0]!.key).toBeTruthy()
  })

  it('refuses what the backend would: a joint over 100, a reserved parameter name, a bad output id', async () => {
    const { seen, client } = backend()
    const split = (joint: object) => [{ edit: 'split_at_plane', object: 'o1', point: [0, 0, 1], normal: [0, 0, 1], joint }]
    const expose = (name: string) => [{ edit: 'expose', object: 'o1', op: null, field: 'translate.0', name }]
    const inline = (name: string) => [{ edit: 'transform', object: 'o1', translate: [{ expose: name, value: 1 }, 0, 0] }]
    const bad = [
      call('edit_scene', { slug: 'carrot', edits: split({ depth: 101 }) }, client),
      call('edit_scene', { slug: 'carrot', edits: split({ diameter: 101 }) }, client),
      ...['o1', 'o2_x', 'sb_x', 'true', 'PI'].flatMap((n) => [
        call('edit_scene', { slug: 'carrot', edits: expose(n) }, client),
        call('edit_scene', { slug: 'carrot', edits: inline(n) }, client),
      ]),
      call('create_model_from_mesh', { name: 'X', output_id: 'o_1' }, client),
    ]
    for (const result of await Promise.all(bad)) expect(JSON.stringify(firstText(result))).toMatch(/invalid|not a usable|expected|too_big|pattern|must/i)
    expect(seen).toEqual([])
    await call('edit_scene', { slug: 'carrot', edits: expose('width') }, client)
    expect(seen).toHaveLength(1)
  })

  it('ejects', async () => {
    const { seen, client } = backend()
    await call('eject_scene', { slug: 'carrot' }, client)
    expect(seen[0]).toEqual(expect.objectContaining({ method: 'POST', path: '/api/v1/models/carrot/scene/eject' }))
  })

  it('no mesh tool is outward: nothing is sent to Bambuddy', () => {
    for (const name of ['create_model_from_mesh', 'get_scene', 'edit_scene', 'eject_scene']) expect(tool(name).risk).not.toBe('outward')
  })
})
