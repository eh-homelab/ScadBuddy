import { createHash } from 'node:crypto'
import { describe, expect, it } from 'vitest'
import { CrudPackageMirror } from '../src/plugins/packages/mirror.js'
import { CrudPackageStore, type PreparedPackage } from '../src/plugins/packages/store.js'
import { withRepinRef } from '../src/plugins/packages/source.js'
import type { PackageReview } from '../src/plugins/packages/vet.js'
import { PluginError } from '../src/plugins/registry.js'

// The plugin packages through the API's CRUD routes (#5299 step 5, area `plugin-packages`),
// against a fake API that keeps the rows in memory as backend api/crud_agent_packages.py
// answers them (the backend's tests/api/test_crud_agent_packages.py covers the routes, the
// row lock, the prune and the eviction on Postgres). The store takes no SQL and no KEK.

const BASE = 'http://api:8080'
const PREFIX = '/api/v1/internal/crud/agent'
const C1 = 'a'.repeat(40)
const C2 = 'b'.repeat(40)
const H1 = `sha256:${'1'.repeat(64)}`
const H2 = `sha256:${'2'.repeat(64)}`
const AT = '2026-11-15T10:20:00.123456Z'

type Seen = { method: string; path: string; body: Record<string, unknown> | undefined }
type Wire = Record<string, unknown>

/** The API's side, in memory: what each route answers, with no SQL. */
class FakeApi {
  seen: Seen[] = []
  rows = new Map<string, Wire>()
  mirrors = new Map<string, Buffer>()
  /** Which package each kept file was sent for: the only one the GET serves it to. */
  mirrorNames = new Map<string, string>()

  readonly fetch: typeof fetch = (input, init) => {
    const url = new URL(String(input))
    const method = init?.method ?? 'GET'
    const raw = init?.body
    const body = typeof raw === 'string' ? (JSON.parse(raw) as Record<string, unknown>) : undefined
    const path = url.pathname.slice(PREFIX.length)
    this.seen.push({ method, path: path + url.search, body })
    const upload = /^\/package-mirrors\/([^/]+)$/.exec(path)
    if (upload && method === 'POST') {
      const bytes = Buffer.from(raw as Uint8Array)
      const sha = createHash('sha256').update(bytes).digest('hex')
      this.mirrors.set(sha, bytes)
      this.mirrorNames.set(sha, decodeURIComponent(upload[1]!))
      return Promise.resolve(this.#json({ bytes_sha256: sha }))
    }
    const mirror = /^\/package-mirrors\/([^/]+)\/([0-9a-f]{64})$/.exec(path)
    if (mirror && method === 'GET') {
      const kept = this.mirrors.get(mirror[2]!)
      if (!kept || this.mirrorNames.get(mirror[2]!) !== decodeURIComponent(mirror[1]!)) {
        return Promise.resolve(this.#problem(404, 'no mirror of the bytes is kept', 'mirror-not-found'))
      }
      return Promise.resolve(new Response(new Uint8Array(kept), { status: 200, headers: { 'x-scadbuddy-crud': '1' } }))
    }
    return Promise.resolve(this.#route(method, path, url.searchParams, body ?? {}))
  }

  #json(value: unknown, status = 200): Response {
    return new Response(JSON.stringify(value), { status, headers: { 'content-type': 'application/json', 'x-scadbuddy-crud': '1' } })
  }

  #problem(status: number, detail: string, reason: string): Response {
    return this.#json({ detail, reason }, status)
  }

  #route(method: string, path: string, q: URLSearchParams, b: Record<string, unknown>): Response {
    if (path === '/plugin-packages' && method === 'GET') {
      const rows = [...this.rows.values()].filter((r) => q.get('enabled') !== 'true' || r.enabled)
      return this.#json({ packages: rows })
    }
    if (path === '/plugin-packages' && method === 'POST') {
      const name = b.name as string
      if (this.rows.has(name)) return this.#problem(409, `a plugin package named "${name}" is already installed; re-pin it instead`, 'package-exists')
      const row: Wire = {
        pin_kind: 'commit',
        commit_sha: null,
        bytes_sha256: null,
        version: null,
        ...b,
        approved_at: null,
        allow_refused: false,
        enabled: false,
        pending_ref: null,
        pending_fetch_url: null,
        pending_fetch_path: null,
        pending_pin_kind: null,
        pending_commit_sha: null,
        pending_bytes_sha256: null,
        pending_version: null,
        pending_content_hash: null,
        pending_files: null,
        pending_review: null,
        created_at: AT,
        updated_at: AT,
      }
      this.rows.set(name, row)
      return this.#json(row, 201)
    }
    const m = /^\/plugin-packages\/([^/]+)(?:\/([a-z-]+))?$/.exec(path)
    if (!m) return this.#json({ detail: 'Not Found' }, 404)
    const name = decodeURIComponent(m[1]!)
    const row = this.rows.get(name)
    if (!m[2]) return this.#json({ package: row ?? null })
    if (m[2] === 'delete') return this.#json({ done: this.rows.delete(name) })
    if (!row) return this.#problem(404, `no plugin package named "${name}"`, 'package-not-found')
    if (m[2] === 'pending') {
      Object.assign(row, {
        pending_ref: b.ref,
        pending_fetch_url: b.fetch_url,
        pending_fetch_path: b.fetch_path,
        pending_pin_kind: b.pin_kind ?? 'commit',
        pending_commit_sha: b.commit_sha ?? null,
        pending_bytes_sha256: b.bytes_sha256 ?? null,
        pending_version: b.version ?? null,
        pending_content_hash: b.content_hash,
        pending_files: b.files,
        pending_review: b.review,
      })
      return this.#json(row)
    }
    if (m[2] === 'approve') {
      const id = b.commit_sha ?? b.bytes_sha256 ?? b.version
      const field = b.version !== undefined ? 'version' : b.bytes_sha256 !== undefined ? 'bytes_sha256' : 'commit_sha'
      const matches = (prefix: string) => id === row[`${prefix}${field}`]
      if (!matches('') && !matches('pending_')) {
        return this.#problem(409, 'the commit and content hash do not match the pin under review; reload the review and approve what it shows', 'pin-mismatch')
      }
      row.approved_at = AT
      return this.#json(row)
    }
    if (m[2] === 'enabled') {
      if (b.enabled && row.approved_at === null) return this.#problem(409, 'approve the pin before enabling the package', 'package-unapproved')
      row.enabled = b.enabled
      return this.#json(row)
    }
    return this.#json({ detail: 'Not Found' }, 404)
  }
}

function review(name = 'greeter'): PackageReview {
  return {
    name,
    description: null,
    version: null,
    skills: [`${name}:hello`],
    commands: [],
    agents: [],
    hooks: [],
    mcp_servers: [],
    local_servers: [],
    files: [],
    refused: [],
  }
}

function prepared(commit = C1, contentHash = H1, name = 'greeter'): PreparedPackage {
  return {
    source: { kind: 'git', url: `https://git.test/${name}.git`, ref: 'HEAD', path: '' },
    fetchUrl: `https://git.test/${name}.git`,
    fetchPath: '',
    commit,
    contentHash,
    files: { 'README.md': { sha256: '0'.repeat(64), size: 1, executable: false } },
    review: review(name),
  }
}

describe('the plugin packages through CRUD routes (#5299 step 5)', () => {
  it('installs, approves, enables and lists the pins, the times read back as dates', async () => {
    const api = new FakeApi()
    const store = new CrudPackageStore({ baseUrl: BASE, fetch: api.fetch })
    const made = await store.create(prepared())
    expect(made).toMatchObject({ name: 'greeter', approved: false, enabled: false, created_at: AT.replace('123456Z', '123Z') })
    expect(api.seen.at(-1)).toMatchObject({
      method: 'POST',
      path: '/plugin-packages',
      body: { name: 'greeter', source_kind: 'git', marketplace_entry: null, commit_sha: C1, content_hash: H1 },
    })
    await store.approve('greeter', C1, H1)
    expect(api.seen.at(-1)?.body).toEqual({ commit_sha: C1, content_hash: H1, allow_refused: false })
    expect((await store.setEnabled('greeter', true)).enabled).toBe(true)
    expect(await store.enabledPins()).toEqual([
      { name: 'greeter', fetchUrl: 'https://git.test/greeter.git', fetchPath: '', commit: C1, contentHash: H1, allowRefused: false },
    ])
    expect(api.seen.at(-1)?.path).toBe('/plugin-packages?enabled=true')
    expect((await store.list()).map((p) => p.name)).toEqual(['greeter'])
    expect(await store.pinOf('greeter')).toMatchObject({ source: { kind: 'git', url: 'https://git.test/greeter.git', ref: 'HEAD', path: '' } })
  })

  it('shows a re-pin with its file diff and reads its files', async () => {
    const api = new FakeApi()
    const store = new CrudPackageStore({ baseUrl: BASE, fetch: api.fetch })
    await store.create(prepared())
    const next = prepared(C2, H2)
    next.files = { 'README.md': { sha256: '9'.repeat(64), size: 2, executable: false }, 'NEW.md': { sha256: '8'.repeat(64), size: 1, executable: false } }
    const view = await store.setPending('greeter', { ...next, source: withRepinRef(next.source, 'main') })
    expect(view.pending).toMatchObject({ ref: 'main', commit_sha: C2, diff: { added: ['NEW.md'], changed: ['README.md'] } })
    expect((await store.filesOf('greeter', true))?.pin.commit).toBe(C2)
    expect((await store.filesOf('greeter', false))?.pin.commit).toBe(C1)
  })

  it("turns the route's 404 and 409 into the PluginError the SQL store throws", async () => {
    const api = new FakeApi()
    const store = new CrudPackageStore({ baseUrl: BASE, fetch: api.fetch })
    await store.create(prepared())
    await expect(store.create(prepared())).rejects.toMatchObject({ name: 'PluginError', status: 409 })
    await expect(store.setEnabled('greeter', true)).rejects.toBeInstanceOf(PluginError)
    await expect(store.approve('greeter', C2, H2)).rejects.toMatchObject({ status: 409, message: expect.stringContaining('do not match') })
    await expect(store.discardPending('nope')).rejects.toMatchObject({ status: 404, message: 'no plugin package named "nope"' })
    // A name the table cannot hold never reaches the API.
    const before = api.seen.length
    expect(await store.get('../x')).toBeUndefined()
    expect(await store.delete('../x')).toBe(false)
    await expect(store.setEnabled('../x', true)).rejects.toMatchObject({ status: 404 })
    expect(api.seen.length).toBe(before)
    // The name check before the call, as the SQL store's.
    await expect(store.setPending('greeter', prepared(C2, H2, 'other'))).rejects.toMatchObject({ status: 409 })
  })

  it('removes a package once', async () => {
    const api = new FakeApi()
    const store = new CrudPackageStore({ baseUrl: BASE, fetch: api.fetch })
    await store.create(prepared())
    expect(await store.delete('greeter')).toBe(true)
    expect(await store.delete('greeter')).toBe(false)
    expect(await store.get('greeter')).toBeUndefined()
  })
})

describe('bytes pins and their mirrors through CRUD routes (#6935)', () => {
  const B1 = 'c'.repeat(64)

  function bytesPrepared(): PreparedPackage {
    return {
      ...prepared('', H1),
      source: { kind: 'archive', url: 'https://files.test/greeter.zip', ref: '' },
      fetchUrl: 'https://files.test/greeter.zip',
      fetchPath: 'greeter-1.0',
      bytesSha256: B1,
    }
  }

  it('sends a bytes pin by its sha256, and a commit pin as before', async () => {
    const api = new FakeApi()
    const store = new CrudPackageStore({ baseUrl: BASE, fetch: api.fetch })
    await store.create(prepared(C1, H1, 'other'))
    // An API pod from before #6935 takes a commit pin's body unchanged.
    expect(api.seen.at(-1)?.body).not.toHaveProperty('pin_kind')
    const made = await store.create(bytesPrepared())
    expect(api.seen.at(-1)?.body).toMatchObject({ source_kind: 'archive', source_ref: '', pin_kind: 'bytes', bytes_sha256: B1, fetch_path: 'greeter-1.0' })
    expect(api.seen.at(-1)?.body).not.toHaveProperty('commit_sha')
    expect(made).toMatchObject({ pin_kind: 'bytes', bytes_sha256: B1, commit_sha: null, source: { kind: 'archive', plugin_path: 'greeter-1.0' } })
    await store.approve('greeter', B1, H1, false, 'bytes')
    expect(api.seen.at(-1)?.body).toEqual({ bytes_sha256: B1, content_hash: H1, allow_refused: false })
    await store.setEnabled('greeter', true)
    expect((await store.enabledPins()).find((p) => p.name === 'greeter')).toMatchObject({ commit: '', bytesSha256: B1, fetchPath: 'greeter-1.0' })
    expect(await store.pinOf('greeter')).toMatchObject({ source: { kind: 'archive', url: 'https://files.test/greeter.zip', ref: '' } })
  })

  it('keeps a mirror for a package, reads it back by name and sha256, and refuses one that does not hash to it', async () => {
    const api = new FakeApi()
    const mirror = new CrudPackageMirror({ baseUrl: BASE, fetch: api.fetch })
    const bytes = Buffer.from('an archive')
    const sha = createHash('sha256').update(bytes).digest('hex')
    await mirror.put('greeter', sha, bytes)
    expect(api.seen.at(-1)).toMatchObject({ method: 'POST', path: '/package-mirrors/greeter' })
    expect(await mirror.get('greeter', sha)).toEqual(bytes)
    expect(api.seen.at(-1)).toMatchObject({ method: 'GET', path: `/package-mirrors/greeter/${sha}` })
    await expect(mirror.get('other', sha)).rejects.toThrow(/no mirrored copy/)
    await expect(mirror.get('greeter', B1)).rejects.toThrow(/no mirrored copy/)
    api.mirrors.set(B1, Buffer.from('other bytes'))
    api.mirrorNames.set(B1, 'greeter')
    await expect(mirror.get('greeter', B1)).rejects.toThrow(/does not hash/)
    // The API names the sha256 it kept; one other than the agent's is refused.
    await expect(mirror.put('greeter', B1, bytes)).rejects.toThrow(/kept the bytes .* not /)
    await expect(mirror.put('../x', sha, bytes)).rejects.toThrow()
  })
})

describe('hosted packages through CRUD routes (#6934)', () => {
  function hostedPrepared(version: number, contentHash: string): PreparedPackage {
    return { ...prepared('', contentHash), source: { kind: 'hosted', name: 'greeter' }, fetchUrl: 'hosted:greeter', version }
  }

  it('sends a version pin, approves it by its version, and shows a version no pin names', async () => {
    const api = new FakeApi()
    const store = new CrudPackageStore({ baseUrl: BASE, fetch: api.fetch })
    const made = await store.create(hostedPrepared(1, H1))
    expect(api.seen.at(-1)?.body).toMatchObject({
      name: 'greeter',
      source_kind: 'hosted',
      source_url: 'hosted:greeter',
      source_ref: '',
      fetch_url: 'hosted:greeter',
      pin_kind: 'version',
      version: 1,
    })
    expect(api.seen.at(-1)?.body).not.toHaveProperty('commit_sha')
    expect(made).toMatchObject({ source: { kind: 'hosted', name: 'greeter' }, pin_kind: 'version', version: 1, approved: false, not_offered: null })
    await store.approve('greeter', '1', H1, false, 'version')
    expect(api.seen.at(-1)?.body).toEqual({ version: 1, content_hash: H1, allow_refused: false })
    await store.setEnabled('greeter', true)
    expect((await store.enabledPins())[0]).toMatchObject({ name: 'greeter', commit: '', version: 1, contentHash: H1 })
    expect(await store.pinOf('greeter')).toMatchObject({ source: { kind: 'hosted', name: 'greeter' }, version: 1 })

    // Version 2 is published: the API answers the latest version, and no pin names it.
    api.rows.get('greeter')!.hosted_latest_version = 2
    expect((await store.get('greeter'))?.not_offered).toBe(2)
    const repinned = await store.setPending('greeter', hostedPrepared(2, H2))
    expect(api.seen.at(-1)?.body).toMatchObject({ ref: '', pin_kind: 'version', version: 2 })
    expect(repinned).toMatchObject({ version: 1, not_offered: null, pending: { pin_kind: 'version', version: 2, content_hash: H2 } })
    // The current pin keeps loading until the re-pin is approved.
    expect((await store.enabledPins())[0]).toMatchObject({ version: 1, contentHash: H1 })
    expect(await store.filesOf('greeter', true)).toMatchObject({ pin: { version: 2, contentHash: H2 } })
  })

  it('reads an API from before #6934 as nothing not offered', async () => {
    const api = new FakeApi()
    const store = new CrudPackageStore({ baseUrl: BASE, fetch: api.fetch })
    await store.create(hostedPrepared(1, H1))
    expect((await store.get('greeter'))?.not_offered).toBeNull()
  })
})
