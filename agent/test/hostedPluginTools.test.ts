import { http, HttpResponse } from 'msw'
import { setupServer } from 'msw/node'
import { afterAll, afterEach, beforeAll, describe, expect, it } from 'vitest'
import { tiersUpTo } from '../src/auth/principal.js'
import type { PackageView } from '../src/plugins/packages/store.js'
import { ALL_TOOLS } from '../src/tools/index.js'
import type { HostedPackageOffers, Tool, ToolContext } from '../src/tools/registry.js'
import { BACKEND, firstText, services } from './helpers/mcp.js'

// #6938 (spec 2026-11-21 §6.2, §6.3): the assistant's hosted plugin tools. Draft writes are
// `write`; the publish is `outward` and offers the version to the package model in-process.

const server = setupServer()
beforeAll(() => server.listen({ onUnhandledFrame: 'error' }))
afterEach(() => server.resetHandlers())
afterAll(() => server.close())

const BASE = `${BACKEND}/api/v1/hosted-plugins`

function tool(name: string): Tool {
  const t = ALL_TOOLS.find((x) => x.name === name)
  if (!t) throw new Error(`no tool ${name}`)
  return t
}

function ctx(hostedPackages?: HostedPackageOffers): ToolContext {
  return {
    ...services(hostedPackages ? { hostedPackages } : {}),
    principal: { id: 'test', kind: 'browser', tiers: tiersUpTo('outward') },
    progress: async () => {},
    signal: new AbortController().signal,
  }
}

const PLUGIN = {
  name: 'shop-notes',
  description: 'Notes for the shop',
  category: null,
  tags: [],
  latest_version: 1,
  file_count: 1,
  total_bytes: 12,
  created_at: '2026-11-22T10:00:00Z',
  updated_at: '2026-11-22T10:00:00Z',
}

function view(over: Partial<PackageView> = {}): PackageView {
  return {
    name: 'shop-notes',
    source: { kind: 'hosted', name: 'shop-notes' },
    pin_kind: 'version',
    commit_sha: null,
    bytes_sha256: null,
    version: 1,
    not_offered: null,
    content_hash: 'h',
    approved: false,
    approved_at: null,
    allow_refused: false,
    enabled: false,
    pending: null,
    created_at: '',
    updated_at: '',
    ...over,
  } as PackageView
}

/** A fake package model: `get` answers `current`, `offer` records each call. */
function offers(current: PackageView | undefined, offered: PackageView = view()) {
  const calls: string[] = []
  const fake: HostedPackageOffers = {
    get: async () => current,
    offer: async (name) => {
      calls.push(name)
      return offered
    },
  }
  return { fake, calls }
}

describe('hosted plugin tools (#6938)', () => {
  it('has the spec’s names and tiers', () => {
    expect(
      Object.fromEntries(ALL_TOOLS.filter((t) => t.name.startsWith('hosted_plugin_')).map((t) => [t.name, t.risk])),
    ).toEqual({
      hosted_plugin_list: 'read',
      hosted_plugin_create: 'write',
      hosted_plugin_update: 'write',
      hosted_plugin_file_read: 'read',
      hosted_plugin_file_write: 'write',
      hosted_plugin_file_delete: 'write',
      hosted_plugin_publish: 'outward',
    })
    expect(tool('hosted_plugin_publish').gated).toBe(true)
  })

  it('lists, and reads one with its draft and its diff', async () => {
    server.use(
      http.get(BASE, () => HttpResponse.json([PLUGIN])),
      http.get(`${BASE}/shop-notes`, () => HttpResponse.json(PLUGIN)),
      http.get(`${BASE}/shop-notes/files`, () =>
        HttpResponse.json({
          files: [{ path: 'skills/notes/SKILL.md', sha256: 'a'.repeat(64), size: 12, executable: false, updated_at: '' }],
          total_bytes: 12,
          max_files: 500,
          max_file_bytes: 2097152,
          max_total_bytes: 20971520,
        }),
      ),
      http.get(`${BASE}/shop-notes/draft-diff`, () =>
        HttpResponse.json({ base_version: 1, added: [], removed: [], changed: ['skills/notes/SKILL.md'], manifest_changed: false }),
      ),
    )
    const all = firstText(await tool('hosted_plugin_list').execute({}, ctx())) as { plugins: { name: string }[] }
    expect(all.plugins.map((p) => p.name)).toEqual(['shop-notes'])
    const one = firstText(await tool('hosted_plugin_list').execute({ name: 'shop-notes' }, ctx())) as Record<string, unknown>
    expect(one['files']).toEqual([{ path: 'skills/notes/SKILL.md', size: 12, sha256: 'a'.repeat(64), executable: false }])
    expect((one['draft_diff'] as { changed: string[] }).changed).toEqual(['skills/notes/SKILL.md'])
  })

  it('writes a file as raw UTF-8 bytes, with its path whole', async () => {
    let got: { path: string; type: string | null; body: string; executable: string | null } | undefined
    server.use(
      http.put(`${BASE}/shop-notes/files/*`, async ({ request }) => {
        const url = new URL(request.url)
        got = {
          path: decodeURIComponent(url.pathname.slice('/api/v1/hosted-plugins/shop-notes/files/'.length)),
          type: request.headers.get('content-type'),
          body: new TextDecoder().decode(await request.arrayBuffer()),
          executable: url.searchParams.get('executable'),
        }
        return HttpResponse.json({ path: got.path, sha256: 'b'.repeat(64), size: 9, executable: true, updated_at: '' })
      }),
    )
    const result = firstText(
      await tool('hosted_plugin_file_write').execute(
        { name: 'shop-notes', path: 'skills/notes/run.sh', content: 'echo “hi”', executable: true },
        ctx(),
      ),
    )
    expect(got).toEqual({ path: 'skills/notes/run.sh', type: 'application/octet-stream', body: 'echo “hi”', executable: 'true' })
    expect(result).toEqual({ path: 'skills/notes/run.sh', size: 9, sha256: 'b'.repeat(64), executable: true })
  })

  it('reads a text file as text and a binary file as its hash only', async () => {
    server.use(
      http.get(`${BASE}/shop-notes/files/*`, ({ request }) => {
        const binary = request.url.endsWith('.png')
        const body = binary ? new Uint8Array([0x89, 0x50, 0xff, 0xfe]) : new TextEncoder().encode('---\nname: notes\n---\n')
        return new HttpResponse(body, { headers: { 'content-type': 'application/octet-stream', 'x-scadbuddy-executable': 'false' } })
      }),
    )
    expect(firstText(await tool('hosted_plugin_file_read').execute({ name: 'shop-notes', path: 'skills/notes/SKILL.md' }, ctx()))).toEqual({
      path: 'skills/notes/SKILL.md',
      size: 20,
      executable: false,
      content: '---\nname: notes\n---\n',
    })
    const binary = firstText(await tool('hosted_plugin_file_read').execute({ name: 'shop-notes', path: 'skills/notes/a.png' }, ctx())) as Record<string, unknown>
    expect(binary['binary']).toBe(true)
    expect(binary['content']).toBeUndefined()
    expect(binary['sha256']).toMatch(/^[0-9a-f]{64}$/)
  })

  it('refuses an update that changes nothing, and sends only what it names', async () => {
    let body: unknown
    server.use(
      http.patch(`${BASE}/shop-notes`, async ({ request }) => {
        body = await request.json()
        return HttpResponse.json(PLUGIN)
      }),
    )
    await expect(tool('hosted_plugin_update').execute({ name: 'shop-notes' }, ctx())).rejects.toThrow('at least one')
    await tool('hosted_plugin_update').execute({ name: 'shop-notes', category: null }, ctx())
    expect(body).toEqual({ category: null })
  })

  it('words its approval with the next version and the draft’s changes', async () => {
    server.use(
      http.get(`${BASE}/shop-notes`, () => HttpResponse.json(PLUGIN)),
      http.get(`${BASE}/shop-notes/draft-diff`, () =>
        HttpResponse.json({ base_version: 1, added: ['skills/b/SKILL.md'], removed: [], changed: ['skills/notes/SKILL.md'], manifest_changed: false }),
      ),
    )
    const prepared = await tool('hosted_plugin_publish').prepareApproval({ name: 'shop-notes' }, ctx())
    expect(prepared.summary).toBe('Publish hosted plugin shop-notes as version 2 (1 added, 1 changed) and offer it to the assistant')
  })

  it('publishes, then installs when no package has the name', async () => {
    server.use(http.post(`${BASE}/shop-notes/publish`, () => HttpResponse.json({ version: 2, zip_sha256: 'z', content_hash: 'h', files: {}, published_at: '', published_by: 'service:agent' })))
    const { fake, calls } = offers(undefined, view({ version: 2 }))
    const result = firstText(await tool('hosted_plugin_publish').execute({ name: 'shop-notes' }, { ...ctx(fake), gate: 'workflow' })) as Record<string, unknown>
    expect(calls).toEqual(['shop-notes'])
    expect(result['published']).toBe(2)
    expect(result['offered']).toBe('installed')
  })

  it('publishes, then re-pins an installed package', async () => {
    server.use(http.post(`${BASE}/shop-notes/publish`, () => HttpResponse.json({ version: 2, zip_sha256: 'z', content_hash: 'h', files: {}, published_at: '', published_by: 'service:agent' })))
    const { fake, calls } = offers(view({ approved: true }), view({ approved: true, pending: { version: 2 } as PackageView['pending'] }))
    const result = firstText(await tool('hosted_plugin_publish').execute({ name: 'shop-notes' }, { ...ctx(fake), gate: 'workflow' })) as Record<string, unknown>
    expect(calls).toEqual(['shop-notes'])
    expect(result['offered']).toBe('re-pinned')
    expect((result['package'] as { pending_version: number }).pending_version).toBe(2)
  })

  it('offers a version no pin names when there is nothing to publish, and nothing otherwise', async () => {
    server.use(
      http.post(`${BASE}/shop-notes/publish`, () =>
        HttpResponse.json({ type: 'about:blank', title: 'Conflict', status: 409, detail: 'the draft is the same as version 2', instance: '', reason: 'nothing-to-publish' }, { status: 409 }),
      ),
    )
    const stale = offers(view({ not_offered: 2 }))
    const offered = firstText(await tool('hosted_plugin_publish').execute({ name: 'shop-notes' }, { ...ctx(stale.fake), gate: 'workflow' })) as Record<string, unknown>
    expect(stale.calls).toEqual(['shop-notes'])
    expect(offered['published']).toBeNull()
    const current = offers(view({ version: 2 }))
    const nothing = firstText(await tool('hosted_plugin_publish').execute({ name: 'shop-notes' }, { ...ctx(current.fake), gate: 'workflow' })) as Record<string, unknown>
    expect(current.calls).toEqual([])
    expect(nothing['published']).toBeNull()
  })

  it('names every problem of a draft that cannot be published, and offers nothing', async () => {
    server.use(
      http.post(`${BASE}/shop-notes/publish`, () =>
        HttpResponse.json(
          {
            type: 'about:blank',
            title: 'Unprocessable Content',
            status: 422,
            detail: 'the draft cannot be published as it is',
            instance: '',
            reason: 'draft-invalid',
            problems: ['skills/notes/SKILL.md: no description'],
          },
          { status: 422 },
        ),
      ),
    )
    const { fake, calls } = offers(undefined)
    await expect(tool('hosted_plugin_publish').execute({ name: 'shop-notes' }, { ...ctx(fake), gate: 'workflow' })).rejects.toThrow(
      'skills/notes/SKILL.md: no description',
    )
    expect(calls).toEqual([])
  })

  it('says the version stands when the offer fails', async () => {
    server.use(http.post(`${BASE}/shop-notes/publish`, () => HttpResponse.json({ version: 3, zip_sha256: 'z', content_hash: 'h', files: {}, published_at: '', published_by: 'service:agent' })))
    const fake: HostedPackageOffers = {
      get: async () => undefined,
      offer: async () => {
        throw new Error('the package is refused')
      },
    }
    await expect(tool('hosted_plugin_publish').execute({ name: 'shop-notes' }, { ...ctx(fake), gate: 'workflow' })).rejects.toThrow(
      /version 3 is published, but offering it .*failed.*the package is refused/,
    )
  })

  it('refuses to publish where it cannot offer', async () => {
    let called = false
    server.use(
      http.post(`${BASE}/shop-notes/publish`, () => {
        called = true
        return HttpResponse.json({})
      }),
    )
    await expect(tool('hosted_plugin_publish').execute({ name: 'shop-notes' }, { ...ctx(), gate: 'workflow' })).rejects.toThrow('nothing was published')
    expect(called).toBe(false)
  })
})
