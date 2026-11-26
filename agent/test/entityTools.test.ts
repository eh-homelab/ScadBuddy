import { createHash } from 'node:crypto'
import { http, HttpResponse } from 'msw'
import { setupServer } from 'msw/node'
import { afterAll, afterEach, beforeAll, describe, expect, it } from 'vitest'
import { tiersUpTo } from '../src/auth/principal.js'
import { ALL_TOOLS } from '../src/tools/index.js'
import { keptName } from '../src/tools/entities.js'
import { runTool, ToolError, type Tool, type ToolContext } from '../src/tools/registry.js'
import { BACKEND, firstText, services } from './helpers/mcp.js'
import { memoryWorkspace } from './helpers/workspace.js'
import { IMAGE_BYTES_MAX } from '../src/sessions/images.js'
import { WorkspaceError } from '../src/workspace/store.js'

// Entity attachments and links (#6950, phase 1 Task 7): the generic `/entities/{ref}` routes.

const server = setupServer()
beforeAll(() => server.listen({ onUnhandledFrame: 'error' }))
afterEach(() => server.resetHandlers())
afterAll(() => server.close())

const SPOOL = 'spool:11111111-2222-4333-8444-555555555555'
const PRINTER = 'printer:7'
const ID = 'a77ac4ed-1111-4222-8333-944455556666'
const base = (entity: string) => `${BACKEND}/api/v1/entities/${encodeURIComponent(entity)}`

function tool(name: string): Tool {
  const t = ALL_TOOLS.find((x) => x.name === name)
  if (!t) throw new Error(`no tool ${name}`)
  return t
}

/** A workspace that keeps what is written, for a file the tool stores. */
function memoryWorkspaceWithWrites() {
  const written = new Map<string, Buffer>()
  const workspace = {
    async write(_ws: unknown, path: string, bytes: Uint8Array, o?: { mediaType?: string }) {
      written.set(path, Buffer.from(bytes))
      return { path, mediaType: o?.mediaType ?? 'application/octet-stream', size: bytes.byteLength, sha256: createHash('sha256').update(bytes).digest('hex') }
    },
  } as unknown as NonNullable<ToolContext['workspace']>
  return { written, workspace }
}

/** The backend's inline types (orders/models.py INLINE): any other file is served as application/octet-stream. */
const INLINE = new Set(['image/png', 'image/jpeg', 'image/webp', 'image/gif', 'image/avif', 'image/heic', 'video/mp4', 'video/quicktime', 'video/webm', 'application/pdf'])

/** An entity's one file as the backend serves it: its record in the list, and its bytes under its type only when inline. */
function serveFile(entity: string, mediaType: string, body: Buffer | null, headers: Record<string, string> = {}, filename = 'file.bin') {
  return [
    http.get(`${base(entity)}/attachments`, () =>
      HttpResponse.json({ attachments: [{ id: ID, entity, filename, media_type: mediaType, kind: 'file', role: 'other' }] }),
    ),
    http.get(`${base(entity)}/attachments/${ID}/content`, () =>
      new HttpResponse(body, {
        headers: {
          'Content-Type': INLINE.has(mediaType) ? mediaType : 'application/octet-stream',
          ...(INLINE.has(mediaType) ? {} : { 'Content-Disposition': `attachment; filename="${filename}"` }),
          ...headers,
        },
      }),
    ),
  ]
}

function ctx(): ToolContext {
  return {
    ...services(),
    principal: { id: 'test', kind: 'browser', tiers: tiersUpTo('outward') },
    progress: async () => {},
    signal: new AbortController().signal,
    pollIntervalMs: 1,
  }
}

describe('entity attachment and link tools', () => {
  it('tiers lists as read and every change as outward, and maps each to its route', () => {
    const expected: Record<string, [string, string]> = {
      entity_attachment_list: ['read', 'GET /api/v1/entities/{ref}/attachments'],
      entity_attachment_add: ['outward', 'POST /api/v1/entities/{ref}/attachments'],
      entity_attachment_update: ['outward', 'PATCH /api/v1/entities/{ref}/attachments/{attachment_id}'],
      entity_attachment_delete: ['outward', 'DELETE /api/v1/entities/{ref}/attachments/{attachment_id}'],
      entity_link_list: ['read', 'GET /api/v1/entities/{ref}/links'],
      entity_link_add: ['outward', 'POST /api/v1/entities/{ref}/links'],
      entity_link_update: ['outward', 'PATCH /api/v1/entities/{ref}/links/{link_id}'],
      entity_link_delete: ['outward', 'DELETE /api/v1/entities/{ref}/links/{link_id}'],
    }
    for (const [name, [risk, route]] of Object.entries(expected)) {
      expect(tool(name).risk, name).toBe(risk)
      expect(tool(name).routes, name).toEqual([route])
    }
  })

  it("answers a photo as an image, its thumbnail on request, and any other file as its bytes", async () => {
    const jpeg = Buffer.from([0xff, 0xd8, 0xff, 0xe0, 1, 2, 3])
    const webp = Buffer.from('RIFF\0\0\0\0WEBPVP8 ')
    server.use(
      ...serveFile(SPOOL, 'image/jpeg', jpeg),
      http.get(`${base(SPOOL)}/attachments/${ID}/thumbnail`, () => new HttpResponse(webp, { headers: { 'Content-Type': 'image/webp' } })),
      ...serveFile(PRINTER, 'application/pdf', Buffer.from('%PDF-1.7'), {}, 'manual.pdf'),
    )
    const get = tool('entity_attachment_get')
    expect(get.risk).toBe('read')
    expect(get.routes).toEqual([
      'GET /api/v1/entities/{ref}/attachments',
      'GET /api/v1/entities/{ref}/attachments/{attachment_id}/content',
      'GET /api/v1/entities/{ref}/attachments/{attachment_id}/thumbnail',
    ])
    const photo = await get.execute({ entity: SPOOL, attachment_id: ID }, ctx())
    expect(photo.content).toEqual([{ type: 'image', mimeType: 'image/jpeg', data: jpeg.toString('base64') }])
    const thumb = await get.execute({ entity: SPOOL, attachment_id: ID, thumbnail: true }, ctx())
    expect(thumb.content).toEqual([{ type: 'image', mimeType: 'image/webp', data: webp.toString('base64') }])
    const ws = memoryWorkspaceWithWrites()
    const pdf = await get.execute({ entity: PRINTER, attachment_id: ID }, { ...ctx(), workspace: ws.workspace } as ToolContext)
    expect(pdf.content.some((c) => c.type === 'resource' || c.type === 'resource_link')).toBe(false)
    expect(firstText(pdf)).toMatchObject({
      workspace_file: { name: `attachments/${ID}-manual.pdf`, mediaType: 'application/pdf', size: 8 },
    })
    expect(ws.written.get(`attachments/${ID}-manual.pdf`)?.toString()).toBe('%PDF-1.7')
  })

  it('keeps an SVG or STL by its own name and type, though the content route serves it as octet-stream', async () => {
    for (const [type, filename, body] of [
      ['image/svg+xml', 'label.svg', '<svg/>'],
      ['model/stl', 'bracket.stl', 'solid x'],
    ] as const) {
      server.use(...serveFile(PRINTER, type, Buffer.from(body), {}, filename))
      const ws = memoryWorkspaceWithWrites()
      const result = await tool('entity_attachment_get').execute({ entity: PRINTER, attachment_id: ID }, { ...ctx(), workspace: ws.workspace } as ToolContext)
      expect(firstText(result)).toMatchObject({ workspace_file: { name: `attachments/${ID}-${filename}`, mediaType: type } })
    }
  })

  it('answers a TIFF as a note by its record, though the content route serves it as octet-stream', async () => {
    server.use(...serveFile(SPOOL, 'image/tiff', Buffer.from('II*\0')))
    expect(firstText(await tool('entity_attachment_get').execute({ entity: SPOOL, attachment_id: ID }, ctx()))).toMatchObject({
      viewable: false,
      media_type: 'image/tiff',
    })
  })

  it('keeps a file with a long name, cut to fit a workspace path segment with its extension', async () => {
    const long = `${'a'.repeat(120)}.pdf`
    expect(keptName(ID, long)).toHaveLength(128)
    expect(keptName(ID, long)).toMatch(/^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$/)
    expect(keptName(ID, long).endsWith('.pdf')).toBe(true)
    expect(keptName(ID, 'manual.pdf')).toBe(`${ID}-manual.pdf`)
    expect(keptName(ID, '..')).toBe(`${ID}-file`)
    server.use(...serveFile(PRINTER, 'application/pdf', Buffer.from('%PDF-1.7'), {}, long))
    const ws = memoryWorkspaceWithWrites()
    const result = await tool('entity_attachment_get').execute({ entity: PRINTER, attachment_id: ID }, { ...ctx(), workspace: ws.workspace } as ToolContext)
    expect(firstText(result)).toMatchObject({ workspace_file: { name: `attachments/${keptName(ID, long)}` } })
  })

  it('refuses a file the entity does not hold', async () => {
    server.use(http.get(`${base(SPOOL)}/attachments`, () => HttpResponse.json({ attachments: [] })))
    await expect(tool('entity_attachment_get').execute({ entity: SPOOL, attachment_id: ID }, ctx())).rejects.toThrow(/has no file/)
  })

  it('answers a HEIC or AVIF photo as a note, never as an image block the model would refuse', async () => {
    for (const type of ['image/heic', 'image/avif']) {
      server.use(...serveFile(SPOOL, type, Buffer.from('ftyp')))
      const result = await tool('entity_attachment_get').execute({ entity: SPOOL, attachment_id: ID }, ctx())
      expect(result.content.some((c) => c.type === 'image')).toBe(false)
      expect(firstText(result)).toMatchObject({ viewable: false, media_type: type, size_bytes: 4 })
    }
  })

  it('answers a file too large to read as a note, never a backend path', async () => {
    server.use(
      ...serveFile(PRINTER, 'video/mp4', null, { 'Content-Length': String(26 * 1024 * 1024) }),
    )
    const result = await tool('entity_attachment_get').execute({ entity: PRINTER, attachment_id: ID }, ctx())
    expect(firstText(result)).toMatchObject({ viewable: false, media_type: 'video/mp4', size_bytes: 26 * 1024 * 1024 })
    expect(JSON.stringify(result.content)).not.toContain('/api/v1/entities')
  })

  it('answers a non-image file as a note with no workspace, or when the workspace refuses it', async () => {
    server.use(...serveFile(PRINTER, 'application/pdf', Buffer.from('%PDF-1.7')))
    const get = tool('entity_attachment_get')
    expect(firstText(await get.execute({ entity: PRINTER, attachment_id: ID }, ctx()))).toMatchObject({
      media_type: 'application/pdf',
      size_bytes: 8,
      note: 'There is no workspace to keep it in here.',
    })
    const full = {
      async write() {
        throw new WorkspaceError('too_large', 'the workspace is full')
      },
    } as unknown as NonNullable<ToolContext['workspace']>
    expect(firstText(await get.execute({ entity: PRINTER, attachment_id: ID }, { ...ctx(), workspace: full } as ToolContext))).toMatchObject({
      note: 'Not kept: the workspace is full',
    })
  })

  it('refuses an attachment id that is not a plain id, before anything is fetched', async () => {
    expect((await runTool(tool('entity_attachment_get'), { entity: PRINTER, attachment_id: '..' }, ctx())).isError).toBe(true)
  })

  it('answers a photo over the read cap with the thumbnail hint', async () => {
    server.use(
      ...serveFile(SPOOL, 'image/jpeg', null, { 'Content-Length': String(26 * 1024 * 1024) }),
    )
    const answer = firstText(await tool('entity_attachment_get').execute({ entity: SPOOL, attachment_id: ID }, ctx())) as { note: string }
    expect(answer.note).toContain('thumbnail: true')
  })

  it('answers a GIF too large to view without the thumbnail hint, which has no GIF', async () => {
    const big = Buffer.alloc(IMAGE_BYTES_MAX + 1)
    big.write('GIF89a')
    server.use(...serveFile(SPOOL, 'image/gif', big))
    const answer = firstText(await tool('entity_attachment_get').execute({ entity: SPOOL, attachment_id: ID }, ctx())) as { note: string }
    expect(answer.note).not.toContain('thumbnail')
  })

  it('answers a photo too large to view as a note, never as an image block the model would refuse', async () => {
    const big = Buffer.alloc(IMAGE_BYTES_MAX + 1)
    big.set([0xff, 0xd8, 0xff])
    server.use(...serveFile(SPOOL, 'image/jpeg', big))
    const result = await tool('entity_attachment_get').execute({ entity: SPOOL, attachment_id: ID }, ctx())
    expect(result.content.some((c) => c.type === 'image')).toBe(false)
    expect(firstText(result)).toMatchObject({ inline: false, media_type: 'image/jpeg', size_bytes: IMAGE_BYTES_MAX + 1 })
  })

  it("refuses an order's entity with the backend's use_order_route", async () => {
    server.use(
      http.post(`${base(`order:${ID}`)}/attachments`, () =>
        HttpResponse.json({ type: 'https://scadbuddy.dev/problems/use_order_route', detail: "an order's files go through the order routes" }, { status: 409 }),
      ),
    )
    const ws = memoryWorkspace({ 'a.jpg': Buffer.from('jpg') }, 'image/jpeg')
    const c = { ...ctx(), workspace: ws.workspace } as ToolContext
    await expect(tool('entity_attachment_add').execute({ entity: `order:${ID}`, ...ws.ref('a.jpg') }, c)).rejects.toMatchObject({
      status: 409,
      problem: 'use_order_route',
    })
  })

  it('attaches a workspace file to an entity as a command, with its role and caption', async () => {
    const seen: { key: string | null; fields: Record<string, string>; file: { name: string; size: number } }[] = []
    server.use(
      http.post(`${base(SPOOL)}/attachments`, async ({ request }) => {
        const form = await request.formData()
        const file = form.get('file') as File
        const fields = Object.fromEntries([...form.entries()].filter((e): e is [string, string] => typeof e[1] === 'string'))
        seen.push({ key: request.headers.get('Idempotency-Key'), fields, file: { name: file.name, size: file.size } })
        return HttpResponse.json({ id: ID, entity: SPOOL, filename: file.name })
      }),
    )
    const ws = memoryWorkspace({ 'photos/label.jpg': Buffer.from('jpg bytes') }, 'image/jpeg')
    const c = { ...ctx(), workspace: ws.workspace } as ToolContext
    const add = tool('entity_attachment_add')
    const result = await add.execute({ entity: SPOOL, ...ws.ref('photos/label.jpg'), role: 'label', caption: 'Lot 7' }, c)
    expect(seen).toHaveLength(1)
    expect(seen[0]!.key).toBeTruthy()
    expect(seen[0]!.fields).toEqual({ role: 'label', caption: 'Lot 7' })
    expect(seen[0]!.file).toEqual({ name: 'label.jpg', size: 9 })
    expect(firstText(result)).toMatchObject({
      id: ID,
      workspace_file: { path: 'photos/label.jpg', size_bytes: 9, media_type: 'image/jpeg' },
    })
  })

  it('lists attachments and links with the entity page where the kind has one', async () => {
    server.use(
      http.get(`${base(SPOOL)}/attachments`, () => HttpResponse.json({ attachments: [{ id: ID, caption: 'x' }] })),
      http.get(`${base(PRINTER)}/links`, () => HttpResponse.json({ links: [{ id: ID, url: 'https://e.test/' }] })),
    )
    const attachments = firstText(await tool('entity_attachment_list').execute({ entity: SPOOL }, ctx()))
    expect(attachments).toEqual({
      attachments: [{ id: ID, caption: 'x' }],
      entity_ui_path: '/filament/spools/11111111-2222-4333-8444-555555555555',
    })
    const links = firstText(await tool('entity_link_list').execute({ entity: PRINTER }, ctx()))
    expect(links).toEqual({ links: [{ id: ID, url: 'https://e.test/' }] })
  })

  it.each([
    ['insert_type', '/filament/catalog?kind=insert-types'],
    ['insert_material', '/filament/catalog?kind=insert-materials'],
    ['material', '/filament/catalog'],
    ['subtype', '/filament/catalog?kind=subtypes'],
    ['brand', '/filament/catalog?kind=brands'],
    ['category', '/filament/catalog?kind=categories'],
    ['effect', '/filament/catalog?kind=effects'],
    ['colour', '/filament/catalog?kind=colours'],
    ['location_kind', '/filament/catalog?kind=location-kinds'],
  ])("answers a %s's catalog as its page", async (kind, page) => {
    const ref = `${kind}:${ID}`
    server.use(http.get(`${base(ref)}/attachments`, () => HttpResponse.json({ attachments: [] })))
    expect(firstText(await tool('entity_attachment_list').execute({ entity: ref }, ctx()))).toEqual({ attachments: [], entity_ui_path: page })
  })

  it('sends each change to its route with only the fields given', async () => {
    const calls: string[] = []
    server.use(
      http.patch(`${base(SPOOL)}/attachments/${ID}`, async ({ request }) => {
        calls.push(`PATCH att ${JSON.stringify(await request.json())}`)
        return HttpResponse.json({ id: ID })
      }),
      http.delete(`${base(SPOOL)}/attachments/${ID}`, () => {
        calls.push('DELETE att')
        return new HttpResponse(null, { status: 204 })
      }),
      http.post(`${base(SPOOL)}/links`, async ({ request }) => {
        calls.push(`POST link ${JSON.stringify(await request.json())}`)
        return HttpResponse.json({ id: ID })
      }),
      http.patch(`${base(SPOOL)}/links/${ID}`, async ({ request }) => {
        calls.push(`PATCH link ${JSON.stringify(await request.json())}`)
        return HttpResponse.json({ id: ID })
      }),
      http.delete(`${base(SPOOL)}/links/${ID}`, () => {
        calls.push('DELETE link')
        return new HttpResponse(null, { status: 204 })
      }),
    )
    await tool('entity_attachment_update').execute({ entity: SPOOL, attachment_id: ID, caption: 'Label' }, ctx())
    expect(firstText(await tool('entity_attachment_delete').execute({ entity: SPOOL, attachment_id: ID }, ctx()))).toEqual({ deleted: ID })
    await tool('entity_link_add').execute({ entity: SPOOL, url: 'https://e.test/p', kind: 'support' }, ctx())
    await tool('entity_link_update').execute({ entity: SPOOL, link_id: ID, note: 'n' }, ctx())
    expect(firstText(await tool('entity_link_delete').execute({ entity: SPOOL, link_id: ID }, ctx()))).toEqual({ deleted: ID })
    expect(calls).toEqual([
      'PATCH att {"caption":"Label"}',
      'DELETE att',
      'POST link {"url":"https://e.test/p","kind":"support"}',
      'PATCH link {"note":"n"}',
      'DELETE link',
    ])
    await expect(tool('entity_link_update').execute({ entity: SPOOL, link_id: ID }, ctx())).rejects.toThrow('nothing to change')
  })

  it('names the entity when the backend says entity_not_found', async () => {
    server.use(
      http.get(`${base(SPOOL)}/links`, () =>
        HttpResponse.json(
          { type: 'https://scadbuddy.dev/problems/entities/entity_not_found', title: 'Not found', status: 404, detail: 'gone' },
          { status: 404 },
        ),
      ),
    )
    const err = await tool('entity_link_list').execute({ entity: SPOOL }, ctx()).catch((e: unknown) => e)
    expect(err).toBeInstanceOf(ToolError)
    expect((err as ToolError).message).toContain(`no entity ${SPOOL}`)
    expect((err as ToolError).status).toBe(404)
    expect((err as ToolError).problem).toBe('entity_not_found')
  })

  it('parks entity_link_add as a pending action and sends nothing', async () => {
    const result = await runTool(tool('entity_link_add'), { entity: SPOOL, url: 'https://e.test/p' }, ctx())
    expect(firstText(result)).toMatchObject({ status: 'pending_approval' })
  })
})

// Entity properties (#7026, Task 4): an entity's key-value properties and a kind's schema.
describe('entity property tools', () => {
  const TYPE = 'spool_type:a1b2c3d4-0000-4000-8000-000000000001'
  const KIND = `${BACKEND}/api/v1/entity-kinds/spool_type/property-schema`

  it('tiers the reads as read and the two writes as write, and maps each to its route', () => {
    const expected: Record<string, [string, string]> = {
      entity_property_list: ['read', 'GET /api/v1/entities/{ref}/properties'],
      entity_property_set: ['write', 'PATCH /api/v1/entities/{ref}/properties'],
      entity_property_schema_set: ['write', 'PUT /api/v1/entity-kinds/{kind}/property-schema'],
    }
    for (const [name, [risk, route]] of Object.entries(expected)) {
      expect(tool(name).risk, name).toBe(risk)
      expect(tool(name).routes, name).toEqual([route])
    }
    expect(tool('entity_property_schema_get').risk).toBe('read')
    expect(tool('entity_property_schema_get').routes).toEqual([
      'GET /api/v1/entity-kinds/{kind}/property-schema',
      'GET /api/v1/entities/{ref}/property-schema',
    ])
  })

  it("reads an entity's own schema: an item's is its kind's (#7067)", async () => {
    const ITEM = 'item:ab700000-0000-4000-8000-000000000001'
    const schema = { type: 'object', properties: { thread: { type: 'string' } } }
    server.use(
      http.get(`${base(ITEM)}/property-schema`, () => HttpResponse.json({ kind: 'item', schema, keys_in_use: [] })),
    )
    expect(firstText(await tool('entity_property_schema_get').execute({ entity: ITEM }, ctx()))).toEqual({
      kind: 'item',
      schema,
      keys_in_use: [],
      entity_ui_path: '/inventory/items/ab700000-0000-4000-8000-000000000001',
    })
    await expect(tool('entity_property_schema_get').execute({}, ctx())).rejects.toThrow('give one of `kind` or `entity`')
    await expect(tool('entity_property_schema_get').execute({ kind: 'spool', entity: ITEM }, ctx())).rejects.toThrow('`kind` or `entity`')
  })

  it('lists the properties with the entity page, and renames a key in one call', async () => {
    const bodies: unknown[] = []
    const row = { key: 'gtin', value: ['0123'], source: 'manual' }
    server.use(
      http.get(`${base(TYPE)}/properties`, () => HttpResponse.json({ properties: [row] })),
      http.patch(`${base(TYPE)}/properties`, async ({ request }) => {
        bodies.push(await request.json())
        return HttpResponse.json({ properties: [{ ...row, key: 'ean' }] })
      }),
    )
    expect(firstText(await tool('entity_property_list').execute({ entity: TYPE }, ctx()))).toEqual({
      properties: [row],
      entity_ui_path: '/filament/types/a1b2c3d4-0000-4000-8000-000000000001',
    })
    const renamed = firstText(await runTool(tool('entity_property_set'), { entity: TYPE, set: { gtin: null, ean: ['0123'] } }, ctx()))
    expect(renamed).toMatchObject({ properties: [{ key: 'ean' }] })
    expect(bodies).toEqual([{ set: { gtin: null, ean: ['0123'] } }])
  })

  it('refuses a bad key or an empty set before it sends anything', () => {
    expect(() => tool('entity_property_set').parse({ entity: TYPE, set: { 'Bad-Key': 'x' } })).toThrow()
    expect(() => tool('entity_property_set').parse({ entity: TYPE, set: {} })).toThrow()
    expect(() => tool('entity_property_set').parse({ entity: TYPE, set: { td: 1.8, ok: true, lot: 'L1', gtins: ['a'], gone: null } })).not.toThrow()
  })

  it('lists every keyword the backend allows in the schema tool description', () => {
    // A copy of backend/scadbuddy/entities/properties.py TOP_KEYWORDS and KEY_KEYWORDS (minus the ones named in prose).
    const keywords = [
      'type', 'properties', 'additionalProperties', '$schema', '$comment', 'title', 'description', 'examples',
      'default', 'deprecated', 'readOnly', 'writeOnly', 'enum', 'const', 'minimum', 'maximum', 'exclusiveMinimum',
      'exclusiveMaximum', 'multipleOf', 'minLength', 'maxLength', 'format', 'items', 'minItems', 'maxItems', 'uniqueItems',
    ]
    const text = tool('entity_property_schema_set').description
    for (const k of keywords) expect(text, k).toContain(`\`${k}`)
  })

  it('summarises a set with its removals, and counts code points like the backend', () => {
    const t = tool('entity_property_set')
    expect(t.summarize?.({ entity: TYPE, set: { mpn: 'x', gtin: null } } as never)).toContain('set mpn; remove gtin')
    expect(() => t.parse({ entity: TYPE, set: { note: '😀'.repeat(1500) } })).not.toThrow()
    expect(() => t.parse({ entity: TYPE, set: { note: '😀'.repeat(2001) } })).toThrow()
    expect(() => t.parse({ entity: TYPE, set: { note: ['😀'.repeat(400)] } })).not.toThrow()
    expect(() => t.parse({ entity: TYPE, set: { note: 'a\u0000b' } })).toThrow()
  })

  it("reads and replaces a kind's schema", async () => {
    const bodies: unknown[] = []
    const schema = { type: 'object', properties: { td: { type: 'number' } } }
    server.use(
      http.get(KIND, () => HttpResponse.json({ kind: 'spool_type', schema, keys_in_use: [{ key: 'td', count: 2 }] })),
      http.put(KIND, async ({ request }) => {
        bodies.push(await request.json())
        return HttpResponse.json({ kind: 'spool_type', schema, keys_in_use: [] })
      }),
    )
    expect(firstText(await tool('entity_property_schema_get').execute({ kind: 'spool_type' }, ctx()))).toMatchObject({
      keys_in_use: [{ key: 'td', count: 2 }],
    })
    await runTool(tool('entity_property_schema_set'), { kind: 'spool_type', schema }, ctx())
    expect(bodies).toEqual([{ schema }])
  })
})
