import { randomUUID } from 'node:crypto'
import { describe, expect, it } from 'vitest'
import { CrudAttachmentStore } from '../src/attachments/crudStore.js'
import { AttachmentError } from '../src/attachments/store.js'
import { BLOB_AREAS } from '../src/api/crudAreas.js'
import { CrudReadError } from '../src/api/crudRead.js'
import { type SessionImage, sessionImage } from '../src/sessions/blobs.js'
import { CrudHttpBodies, CrudSessionBlobs, IMAGES_PER_WRITE } from '../src/sessions/crudBlobs.js'
import type { Owner } from '../src/sessions/protocol.js'

// A session's images, the staged attachments and the saved http_request bodies through the API's
// CRUD routes (#5299 step 7, areas `session-blobs`, `attachments`, `http-bodies`), against a fake
// API that keeps them in memory as backend api/crud_agent_blobs.py does (the backend's
// tests/crud/test_crud_agent_blobs.py covers the routes and the SQL).

const BASE = 'http://api:8080'
const PREFIX = '/api/v1/internal/crud/agent'
const SESSION = '0b6d8a52-3a4e-4d36-9a3c-2f8c1d1e0f11'
const PNG = Buffer.concat([Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]), Buffer.alloc(16)])
const WEBP = Buffer.concat([Buffer.from('RIFF'), Buffer.alloc(4), Buffer.from('WEBPVP8 '), Buffer.alloc(8)])
const owner: Owner = { kind: 'browser', id: 'browser', label: 'You' }

type Seen = { method: string; path: string; query: URLSearchParams; meta?: Record<string, unknown>; parts: string[] }

class FakeApi {
  readonly blobs = new Map<string, { mediaType: string; bytes: Buffer }>()
  readonly attachments = new Map<string, { owner: string; name: string; mediaType: string; image: Buffer; previewType: string; preview: Buffer }>()
  readonly bodies = new Map<string, { meta: Record<string, unknown>; bytes: Buffer }>()
  readonly seen: Seen[] = []
  fenced = true
  quotaFull = false

  readonly fetch: typeof fetch = async (input, init) => {
    const request = new Request(input, init)
    const url = new URL(request.url)
    const path = url.pathname.slice(PREFIX.length)
    const seen: Seen = { method: request.method, path, query: url.searchParams, parts: [] }
    const type = request.headers.get('content-type') ?? ''
    let json: Record<string, unknown> | undefined
    const files = new Map<string, Buffer>()
    if (type.startsWith('multipart/form-data')) {
      const form = await request.formData()
      for (const [key, value] of form) {
        if (key === 'meta') seen.meta = JSON.parse(String(value)) as Record<string, unknown>
        else {
          seen.parts.push(key)
          files.set(key, Buffer.from(await (value as Blob).arrayBuffer()))
        }
      }
    } else if (type.startsWith('application/json')) json = (await request.json()) as Record<string, unknown>
    this.seen.push(seen)
    const crud = { 'x-scadbuddy-crud': '1' }
    const ok = (body: unknown) => new Response(JSON.stringify(body), { headers: crud })
    const missing = () => new Response(JSON.stringify({ detail: 'none' }), { status: 404, headers: crud })
    const q = url.searchParams
    if (path === '/session-blobs' && request.method === 'POST') {
      const meta = seen.meta as { session_id: string; images: { name: string; media_type: string }[]; fence?: unknown }
      if (meta.fence !== undefined && !this.fenced) return ok({ stored: false })
      meta.images.forEach((image, i) => this.blobs.set(`${meta.session_id}|${image.name}`, { mediaType: image.media_type, bytes: files.get(`image${i}`) ?? Buffer.alloc(0) }))
      return ok({ stored: true })
    }
    if (path === '/session-blobs') {
      const blob = this.blobs.get(`${q.get('session_id')}|${q.get('name')}`)
      return blob ? new Response(new Uint8Array(blob.bytes), { headers: { ...crud, 'content-type': blob.mediaType } }) : missing()
    }
    if (path === '/attachments' && request.method === 'POST') {
      if (this.quotaFull) return new Response(JSON.stringify({ detail: 'too many images', reason: 'quota' }), { status: 409, headers: crud })
      const meta = seen.meta as { owner: { kind: string; id: string }; media_type: 'image/png'; preview_media_type: 'image/webp' }
      const image = files.get('image') ?? Buffer.alloc(0)
      const preview = files.get('preview') ?? Buffer.alloc(0)
      const id = randomUUID()
      const name = sessionImage(meta.media_type, image).name
      this.attachments.set(id, { owner: `${meta.owner.kind}:${meta.owner.id}`, name, mediaType: meta.media_type, image, previewType: meta.preview_media_type, preview })
      return new Response(
        JSON.stringify({ id, name, media_type: meta.media_type, preview: { name: sessionImage(meta.preview_media_type, preview).name, media_type: meta.preview_media_type } }),
        { status: 201, headers: crud },
      )
    }
    const body = json as { owner: { kind: string; id: string }; ids: string[] } | undefined
    const mine = (id: string) => this.attachments.get(id)?.owner === `${body?.owner.kind}:${body?.owner.id}`
    if (path === '/attachments/resolve') {
      const rows = (body?.ids ?? []).filter(mine).map((id) => {
        const a = this.attachments.get(id)!
        return { id, name: a.name, media_type: a.mediaType, preview_media_type: a.previewType }
      })
      return ok({ attachments: rows })
    }
    if (path === '/attachments/deletes') {
      const gone = (body?.ids ?? []).filter(mine)
      for (const id of gone) this.attachments.delete(id)
      return ok({ deleted: gone.length })
    }
    if (path === '/attachments/sweeps') return ok({ deleted: 0 })
    const part = /^\/attachments\/([^/]+)\/(image|preview)$/.exec(path)
    if (part) {
      const a = this.attachments.get(decodeURIComponent(part[1] ?? ''))
      if (!a || a.owner !== `${q.get('owner_kind')}:${q.get('owner_id')}`) return missing()
      return new Response(new Uint8Array(part[2] === 'image' ? a.image : a.preview), { headers: crud })
    }
    if (path === '/http-bodies' && request.method === 'POST') {
      const meta = seen.meta as Record<string, unknown>
      this.bodies.set(`${String(meta.session_id)}|${String(meta.id)}`, { meta, bytes: files.get('body') ?? Buffer.alloc(0) })
      return ok({ stored: true })
    }
    const saved = this.bodies.get(`${q.get('session_id')}|${q.get('id')}`)
    if (path === '/http-bodies/meta') return saved ? ok({ ...saved.meta, created_at: new Date(0).toISOString() }) : missing()
    if (path === '/http-bodies/bytes') {
      if (!saved) return missing()
      const offset = Number(q.get('offset') ?? 0)
      const length = q.has('length') ? Number(q.get('length')) : saved.bytes.length
      return new Response(new Uint8Array(saved.bytes.subarray(offset, offset + length)), { headers: crud })
    }
    return new Response('no route', { status: 404 })
  }
}

const image = (n: number): SessionImage => sessionImage('image/png', Buffer.concat([PNG, Buffer.from([n])]))

describe('the blob areas', () => {
  it('are the three the backend lists', () => {
    expect([...BLOB_AREAS]).toEqual(['session-blobs', 'attachments', 'http-bodies'])
  })
})

describe('CrudSessionBlobs', () => {
  it('sends the bytes as parts, names them in meta, and reads them back as bytes', async () => {
    const api = new FakeApi()
    const blobs = new CrudSessionBlobs(BASE, { fetch: api.fetch })
    await blobs.put(SESSION, [image(1), image(2)], 'toolu_1')
    expect(api.seen[0]?.meta).toMatchObject({ session_id: SESSION, tool_use_id: 'toolu_1', images: [{ name: image(1).name, media_type: 'image/png' }, { name: image(2).name }] })
    expect(api.seen[0]?.parts).toEqual(['image0', 'image1'])
    expect(JSON.stringify(api.seen[0]?.meta)).not.toContain(image(1).bytes.toString('base64'))
    const read = await blobs.get(SESSION, image(2).name)
    expect(read).toEqual({ mediaType: 'image/png', bytes: image(2).bytes })
    expect(api.seen.at(-1)?.query.get('session_id')).toBe(SESSION)
    expect(await blobs.get(SESSION, image(9).name)).toBeUndefined()
  })

  it('never sends a name that is not a blob name', async () => {
    const api = new FakeApi()
    const blobs = new CrudSessionBlobs(BASE, { fetch: api.fetch })
    for (const name of ['../x.png', `${'a'.repeat(64)}.png/../y`, 'x.png']) expect(await blobs.get(SESSION, name)).toBeUndefined()
    expect(api.seen).toEqual([])
  })

  it('splits a plain write into batches, but never one tool call, and sends the fence', async () => {
    const api = new FakeApi()
    const blobs = new CrudSessionBlobs(BASE, { fetch: api.fetch })
    const many = Array.from({ length: IMAGES_PER_WRITE + 2 }, (_, i) => image(i))
    await blobs.put(SESSION, many)
    expect(api.seen.map((s) => s.parts.length)).toEqual([IMAGES_PER_WRITE, 2])
    await expect(blobs.put(SESSION, many, 'toolu_2')).rejects.toThrow(/at most/)
    expect(await blobs.putIf(SESSION, [image(1)], { attempt: 'run:1', turnId: 't' })).toBe(true)
    expect(api.seen.at(-1)?.meta?.fence).toEqual({ attempt: 'run:1', turn_id: 't' })
    api.fenced = false
    expect(await blobs.putIf(SESSION, [image(1)], { attempt: 'run:2', turnId: 't' })).toBe(false)
  })
})

describe('CrudAttachmentStore', () => {
  const userImage = { mediaType: 'image/png' as const, bytes: PNG, preview: { mediaType: 'image/webp' as const, bytes: WEBP } }

  it('stages, resolves, claims into the session and removes', async () => {
    const api = new FakeApi()
    const blobs = new CrudSessionBlobs(BASE, { fetch: api.fetch })
    const store = new CrudAttachmentStore(BASE, blobs, { fetch: api.fetch })
    const view = await store.put(owner, userImage)
    expect(view).toMatchObject({ name: sessionImage('image/png', PNG).name, mediaType: 'image/png', preview: { mediaType: 'image/webp' } })
    expect(api.seen[0]?.parts).toEqual(['image', 'preview'])
    const [resolved] = await store.resolve(owner, [{ kind: 'attachment', id: view.id }])
    expect(resolved?.image.bytes).toEqual(PNG)
    expect(resolved?.image.preview.bytes).toEqual(WEBP)
    await store.claim(SESSION, owner, resolved ? [resolved] : [])
    expect((await blobs.get(SESSION, view.name))?.bytes).toEqual(PNG)
    expect(api.attachments.size).toBe(0)
    const again = await store.put(owner, userImage)
    expect(await store.remove(owner, again.id)).toBe(true)
    expect(await store.remove(owner, again.id)).toBe(false)
  })

  it("reads another owner's attachment as unknown, and refuses past the caps", async () => {
    const api = new FakeApi()
    const store = new CrudAttachmentStore(BASE, new CrudSessionBlobs(BASE, { fetch: api.fetch }), { fetch: api.fetch })
    const view = await store.put(owner, userImage)
    const other: Owner = { kind: 'bearer', id: 'token:other', label: 'x' }
    await expect(store.resolve(other, [{ kind: 'attachment', id: view.id }])).rejects.toMatchObject({ code: 'not_found' })
    api.quotaFull = true
    await expect(store.put(owner, userImage)).rejects.toBeInstanceOf(AttachmentError)
    await expect(store.put(owner, userImage)).rejects.toMatchObject({ code: 'quota' })
  })

  it('never puts an id that is not a UUID into a path', async () => {
    const api = new FakeApi()
    const store = new CrudAttachmentStore(BASE, new CrudSessionBlobs(BASE, { fetch: api.fetch }), { fetch: api.fetch })
    await expect(store.resolve(owner, [{ kind: 'attachment', id: '../x' }])).rejects.toMatchObject({ code: 'not_found' })
    expect(await store.remove(owner, '../../sessions')).toBe(false)
    expect(api.seen).toEqual([])
  })
})

describe('CrudHttpBodies', () => {
  it('saves the body as a part and reads it by range', async () => {
    const api = new FakeApi()
    const bodies = new CrudHttpBodies(BASE, SESSION, { fetch: api.fetch })
    const id = randomUUID()
    const meta = { url: 'https://example.com', content_type: 'text/plain', size_bytes: 10, complete: true }
    await bodies.save(id, Buffer.from('0123456789'), meta)
    expect(api.seen[0]?.parts).toEqual(['body'])
    expect(await bodies.meta(id)).toEqual(meta)
    expect((await bodies.read(id, 2, 3)).toString()).toBe('234')
    expect(api.seen.at(-1)?.query.get('offset')).toBe('2')
    expect((await bodies.whole(id))?.bytes.toString()).toBe('0123456789')
    const other = randomUUID()
    expect(await bodies.meta(other)).toBeUndefined()
    expect((await bodies.read(other, 0, 4)).length).toBe(0)
    expect(await bodies.whole(other)).toBeUndefined()
  })

  it('never sends an id that is not a UUID', async () => {
    const api = new FakeApi()
    const bodies = new CrudHttpBodies(BASE, SESSION, { fetch: api.fetch })
    expect(await bodies.meta('../x')).toBeUndefined()
    expect((await bodies.read('../x', 0, 1)).length).toBe(0)
    await expect(bodies.save('../x', Buffer.from('x'), { url: 'u', content_type: null, size_bytes: 1, complete: true })).rejects.toThrow(/UUID/)
    expect(api.seen).toEqual([])
  })

  it('takes a non-CRUD answer as a failure, not as no body', async () => {
    const bodies = new CrudHttpBodies(BASE, SESSION, { fetch: async () => new Response('bad gateway', { status: 502 }) })
    await expect(bodies.meta(randomUUID())).rejects.toBeInstanceOf(CrudReadError)
  })
})
