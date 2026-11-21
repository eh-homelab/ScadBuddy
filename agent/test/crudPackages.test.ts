import { describe, expect, it } from 'vitest'
import { CrudPackageStore, type PreparedPackage } from '../src/plugins/packages/store.js'
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

  readonly fetch: typeof fetch = (input, init) => {
    const url = new URL(String(input))
    const method = init?.method ?? 'GET'
    const raw = init?.body
    const body = typeof raw === 'string' ? (JSON.parse(raw) as Record<string, unknown>) : undefined
    const path = url.pathname.slice(PREFIX.length)
    this.seen.push({ method, path: path + url.search, body })
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
        ...b,
        approved_at: null,
        allow_refused: false,
        enabled: false,
        pending_ref: null,
        pending_fetch_url: null,
        pending_fetch_path: null,
        pending_commit_sha: null,
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
        pending_commit_sha: b.commit_sha,
        pending_content_hash: b.content_hash,
        pending_files: b.files,
        pending_review: b.review,
      })
      return this.#json(row)
    }
    if (m[2] === 'approve') {
      if (b.commit_sha !== row.commit_sha && b.commit_sha !== row.pending_commit_sha) {
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
    const view = await store.setPending('greeter', { ...next, source: { ...next.source, ref: 'main' } })
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
