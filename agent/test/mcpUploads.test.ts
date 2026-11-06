import { createHash } from 'node:crypto'
import { describe, expect, it } from 'vitest'
import { readCapped, uploadMediaType } from '../src/mcp/uploads.js'
import { type SessionFiles, type WorkspaceFileInfo, WorkspaceError, type WorkspaceRef, type WriteOptions } from '../src/workspace/store.js'
import { appFetch, INGRESS, services, testApp } from './helpers/mcp.js'

// PUT /mcp/workspace/<path> (#4550, src/mcp/uploads.ts): an /mcp client's binary
// file into its own principal workspace, answered with the pointer the tools take.
// The workspace here is a fake that records each write; mcpUploads.pg.test.ts
// writes to Postgres and reads the file back over /mcp.

const UPLOAD_URL = 'http://scadbuddy.test/mcp/workspace'
const HTTPS = { 'x-forwarded-proto': 'https' }
/** A PNG's 8-byte signature, then filler: sniffImage reads only the signature. */
const PNG = Buffer.concat([Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]), Buffer.alloc(24, 1)])
const STL = Buffer.concat([Buffer.alloc(80, 0x20), Buffer.from([0, 0, 0, 0])])

type Written = { ws: WorkspaceRef; path: string; bytes: Uint8Array; options: WriteOptions }

function fakeWorkspace(fileMaxBytes = 1000) {
  const written: Written[] = []
  const workspace = {
    async limits() {
      return { fileMaxBytes, workspaceMaxBytes: 10 * fileMaxBytes }
    },
    async write(ws: WorkspaceRef, path: string, bytes: Uint8Array, options: WriteOptions = {}): Promise<WorkspaceFileInfo> {
      if (options.expectedSha256 === null && written.some((w) => w.path === path)) {
        throw new WorkspaceError('conflict', `${path} already exists; nothing was written`)
      }
      written.push({ ws, path, bytes, options })
      const sha256 = createHash('sha256').update(bytes).digest('hex')
      return { path, mediaType: options.mediaType ?? 'application/octet-stream', size: bytes.byteLength, sha256, source: options.source ?? null, createdAt: new Date(0), updatedAt: new Date(0) }
    },
  } as unknown as SessionFiles
  return { workspace, written }
}

async function setup(options: { tier?: 'read' | 'write' | 'outward'; mode?: 'bearer' | 'disabled'; fileMaxBytes?: number } = {}) {
  const { workspace, written } = fakeWorkspace(options.fileMaxBytes)
  const t = testApp({ services: services({ workspace }), settings: { mode: options.mode ?? 'bearer' } })
  const { token, record } = await t.tokens.mint({ name: 'plugin', tier: options.tier ?? 'write' })
  const headers = { ...HTTPS, authorization: `Bearer ${token}` }
  const put = (path: string, body: RequestInit['body'], extra: Record<string, string> = {}, method = 'PUT') =>
    appFetch(t.app, { address: INGRESS, headers: { ...headers, ...extra } })(`${UPLOAD_URL}/${path}`, { method, body, duplex: 'half' } as RequestInit)
  return { ...t, written, put, record }
}

describe('PUT /mcp/workspace/<path>', () => {
  it("stores the bytes in the caller's principal workspace and answers {name, mediaType, size, sha256}", async () => {
    const { put, written, record } = await setup()
    const res = await put('thumbs/keychain.png', PNG, { 'content-type': 'image/png' })
    expect(res.status).toBe(201)
    expect(res.headers.get('cache-control')).toBe('no-store')
    const body = await res.json()
    expect(body).toEqual({
      name: 'thumbs/keychain.png',
      mediaType: 'image/png',
      size: PNG.length,
      sha256: createHash('sha256').update(PNG).digest('hex'),
    })
    expect(written).toHaveLength(1)
    expect(written[0]!.ws).toEqual({ kind: 'principal', principalKind: 'bearer', principalId: `token:${record.id}` })
    expect(Buffer.from(written[0]!.bytes).equals(PNG)).toBe(true)
    // A new path must not exist yet; the stored type is ScadBuddy's own.
    expect(written[0]!.options).toMatchObject({ mediaType: 'image/png', expectedSha256: null, source: { kind: 'upload', by: 'mcp' } })
  })

  it('stores an STL by its extension, with no Content-Type', async () => {
    const { put } = await setup()
    const res = await put('part.stl', STL)
    expect(res.status).toBe(201)
    expect(await res.json()).toMatchObject({ name: 'part.stl', mediaType: 'model/stl', size: STL.length })
  })

  it('replaces a file only with expected_sha256, and answers 200 then', async () => {
    const { put } = await setup()
    expect((await put('part.stl', STL)).status).toBe(201)
    const again = await put('part.stl', STL)
    expect(again.status).toBe(409)
    const sha = createHash('sha256').update(STL).digest('hex')
    expect((await put(`part.stl?expected_sha256=${sha}`, STL)).status).toBe(200)
    expect((await put('part.stl?expected_sha256=nothex', STL)).status).toBe(400)
  })

  describe('auth', () => {
    it('refuses a request with no token, as /mcp does', async () => {
      const { app, written } = await setup()
      const res = await appFetch(app, { address: INGRESS, headers: HTTPS })(`${UPLOAD_URL}/a.png`, { method: 'PUT', body: PNG })
      expect(res.status).toBe(401)
      expect(res.headers.get('www-authenticate')).toMatch(/^Bearer/)
      expect(written).toHaveLength(0)
    })

    it('refuses a token that is not valid', async () => {
      const { app, written } = await setup()
      const res = await appFetch(app, { address: INGRESS, headers: { ...HTTPS, authorization: 'Bearer sbmcp_nope' } })(`${UPLOAD_URL}/a.png`, {
        method: 'PUT',
        body: PNG,
      })
      expect(res.status).toBe(401)
      expect(written).toHaveLength(0)
    })

    it('refuses a read-tier token', async () => {
      const { put, written } = await setup({ tier: 'read' })
      const res = await put('a.png', PNG)
      expect(res.status).toBe(403)
      expect(((await res.json()) as { error: string }).error).toContain('write tier')
      expect(written).toHaveLength(0)
    })

    it('refuses disabled auth mode: no credential names a workspace', async () => {
      const { put, written } = await setup({ mode: 'disabled' })
      const res = await put('a.png', PNG)
      expect(res.status).toBe(403)
      expect(((await res.json()) as { error: string }).error).toContain('disabled auth mode')
      expect(written).toHaveLength(0)
    })

    it('refuses plain HTTP and a foreign Origin before auth', async () => {
      const { app, put } = await setup()
      const plain = await appFetch(app, { address: INGRESS, headers: { 'x-forwarded-proto': 'http' } })(`${UPLOAD_URL}/a.png`, { method: 'PUT', body: PNG })
      expect(plain.status).toBe(403)
      expect(((await plain.json()) as { error: string }).error).toContain('HTTPS only')
      const foreign = await put('a.png', PNG, { origin: 'https://evil.test' })
      expect(foreign.status).toBe(403)
    })

    it('answers 405 to any method but PUT', async () => {
      const { put } = await setup()
      const res = await put('a.png', null, {}, 'GET')
      expect(res.status).toBe(405)
      expect(res.headers.get('allow')).toBe('PUT')
    })
  })

  describe('size cap', () => {
    it('refuses a Content-Length over the per-file cap unread', async () => {
      const { put, written } = await setup({ fileMaxBytes: 16 })
      const res = await put('big.stl', STL, { 'content-length': String(STL.length) })
      expect(res.status).toBe(413)
      expect(((await res.json()) as { error: string }).error).toContain('16 bytes')
      expect(written).toHaveLength(0)
    })

    it('stops a streamed body with no length once it passes the cap', async () => {
      const { put, written } = await setup({ fileMaxBytes: 16 })
      let pulled = 0
      const stream = new ReadableStream<Uint8Array>({
        pull(controller) {
          pulled += 1
          if (pulled > 100) controller.close()
          else controller.enqueue(new Uint8Array(10))
        },
      })
      const capped = await put('big.stl', stream)
      expect(capped.status).toBe(413)
      expect(written).toHaveLength(0)
      // Read no further than the chunk that passed the cap (plus what the stream queued ahead).
      expect(pulled).toBeLessThan(10)
    })

    it('refuses an empty body', async () => {
      const { put } = await setup()
      expect((await put('a.stl', new Uint8Array())).status).toBe(400)
    })
  })

  describe('names', () => {
    it.each([
      ['..%2F..%2Fetc%2Fpasswd'],
      ['%2Fetc%2Fpasswd'],
      ['.hidden'],
      ['a%00b.png'],
      ['a/b/c/d/e.png'],
      ['a%5Cb.png'],
      ['%E0%A4%A'],
      [''],
    ])('refuses %s', async (path) => {
      const { put, written } = await setup()
      const res = await put(path, PNG)
      expect(res.status).toBe(400)
      expect(written).toHaveLength(0)
    })

    it('decodes each segment and stores the checked path', async () => {
      const { put, written } = await setup()
      const res = await put('uploads/part%2D1.stl', STL)
      expect(res.status).toBe(201)
      expect(written[0]!.path).toBe('uploads/part-1.stl')
    })
  })

  describe('media type', () => {
    it('refuses a .png that is not a PNG', async () => {
      const { put, written } = await setup()
      const res = await put('a.png', STL)
      expect(res.status).toBe(415)
      expect(written).toHaveLength(0)
    })

    it('refuses a PNG named .stl, and a declared type the bytes contradict', async () => {
      const { put } = await setup()
      expect((await put('a.stl', PNG)).status).toBe(415)
      expect((await put('a', STL, { 'content-type': 'image/png' })).status).toBe(415)
      expect((await put('a', PNG, { 'content-type': 'image/jpeg' })).status).toBe(415)
    })

    it('refuses a form body and a Content-Encoding', async () => {
      const { put } = await setup()
      expect((await put('a.png', PNG, { 'content-type': 'multipart/form-data; boundary=x' })).status).toBe(415)
      expect((await put('a.png', PNG, { 'content-encoding': 'gzip' })).status).toBe(415)
    })

    it('keeps its own guess over a declared type that is not an image', async () => {
      expect(uploadMediaType('part.stl', STL, 'application/sla')).toBe('model/stl')
      expect(uploadMediaType('part.stl', STL, 'application/octet-stream')).toBe('model/stl')
      expect(uploadMediaType('thumb', PNG, undefined)).toBe('image/png')
      expect(uploadMediaType('notes.txt', Buffer.from('hi'), 'text/html')).toBe('text/plain')
    })
  })
})

describe('readCapped', () => {
  it('answers the bytes under the cap and too_large past it', async () => {
    const body = () => new Blob([new Uint8Array(10), new Uint8Array(10)]).stream()
    expect((await readCapped(body(), 20)) as Uint8Array).toHaveLength(20)
    expect(await readCapped(body(), 19)).toBe('too_large')
    expect(await readCapped(null, 19)).toHaveLength(0)
  })
})
