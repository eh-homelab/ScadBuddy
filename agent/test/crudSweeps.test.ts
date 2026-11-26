import { mkdir, mkdtemp, readdir } from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'
import { describe, expect, it } from 'vitest'
import { CrudForgetRows, CrudSubjectKeys, forgetRoutes, forgetSubject } from '../src/sessions/forget.js'
import { CrudSessionReads } from '../src/sessions/sessionReads.js'
import { CrudSessionPresence, LIVE_IDS_MAX } from '../src/sessions/presence.js'
import { ForksNeedKeys } from '../src/sessions/store.js'
import { sweepTurnDirs } from '../src/temporal/turnServers.js'

// The pod's directory sweeps and a forget's last rows through the API's CRUD routes (#6501),
// against a fake API that answers as the backend's tools/go/core/internal/sweeps does (the backend's
// the Go tests in tools/go/core covers the routes). A session id goes into a path only as
// a lowercase UUID, encoded; an id that is no UUID sends nothing.

const BASE = 'http://api:8080'
const PREFIX = '/api/v1/internal/crud/agent'
const SESSION = '0b0e7c55-6a3e-4a37-9d3c-4b1a1f0e2a10'
const GONE = '1b6c1e4e-7d3a-4f5e-9a51-3f1c2d4e5f61'

type Seen = { method: string; path: string; body: unknown }

class FakeApi {
  seen: Seen[] = []
  live = new Set<string>()
  status = 200
  crud = true

  readonly fetch: typeof fetch = async (input, init) => {
    const request = new Request(input, init)
    const url = new URL(request.url)
    const path = url.pathname.slice(PREFIX.length)
    const text = request.method === 'POST' ? await request.text() : ''
    const body = text ? (JSON.parse(text) as unknown) : undefined
    this.seen.push({ method: request.method, path, body })
    const headers = this.crud ? { 'x-scadbuddy-crud': '1' } : {}
    if (this.status !== 200) return new Response(JSON.stringify({ detail: 'refused' }), { status: this.status, headers })
    if (path === '/sessions/live') {
      const ids = (body as { ids: string[] }).ids
      return new Response(JSON.stringify({ ids: ids.filter((id) => this.live.has(id)) }), { headers })
    }
    if (path.endsWith('/forget-rows')) return new Response(JSON.stringify({ rows: 3 }), { headers })
    if (path === '/background-tasks') return new Response(JSON.stringify({ tasks: [] }), { headers })
    return new Response(JSON.stringify({ detail: 'no route' }), { status: 404, headers })
  }
}

const target = (api: FakeApi) => ({ baseUrl: BASE, fetch: api.fetch })

describe('which sessions still have a row, over the CRUD route', () => {
  it('asks in pages of the most the API takes, each id lowercased once', async () => {
    const api = new FakeApi()
    const ids = Array.from({ length: LIVE_IDS_MAX + 5 }, (_, i) => `00000000-0000-4000-8000-${String(i).padStart(12, '0')}`)
    api.live = new Set([ids[0]!, ids[LIVE_IDS_MAX + 1]!])
    const live = await new CrudSessionPresence(target(api)).live([...ids, SESSION.toUpperCase(), SESSION])
    expect(live).toEqual(new Set([ids[0], ids[LIVE_IDS_MAX + 1]]))
    expect(api.seen.map((s) => (s.body as { ids: string[] }).ids.length)).toEqual([LIVE_IDS_MAX, 6])
    expect(api.seen.every((s) => s.method === 'POST' && s.path === '/sessions/live')).toBe(true)
  })

  it('sends nothing for no ids, and throws for an id that is no UUID', async () => {
    const api = new FakeApi()
    const presence = new CrudSessionPresence(target(api))
    expect(await presence.live([])).toEqual(new Set())
    await expect(presence.live([SESSION, '../areas'])).rejects.toThrow(/not a session id/)
    expect(api.seen).toEqual([])
  })

  it('throws on a refusal or an answer not from the route, so a sweep removes nothing', async () => {
    const root = await mkdtemp(path.join(os.tmpdir(), 'crud-sweeps-'))
    for (const name of [SESSION, GONE]) await mkdir(path.join(root, name), { recursive: true })
    const refused = new FakeApi()
    refused.status = 503
    await expect(sweepTurnDirs(root, new CrudSessionPresence(target(refused)))).rejects.toThrow()
    const proxy = new FakeApi()
    proxy.crud = false
    await expect(sweepTurnDirs(root, new CrudSessionPresence(target(proxy)))).rejects.toThrow()
    expect((await readdir(root)).sort()).toEqual([SESSION, GONE].sort())
  })

  it("sweeps a gone session's directory through the route", async () => {
    const root = await mkdtemp(path.join(os.tmpdir(), 'crud-sweeps-'))
    for (const name of [SESSION, GONE]) await mkdir(path.join(root, name), { recursive: true })
    const api = new FakeApi()
    api.live = new Set([SESSION])
    const presence = new CrudSessionPresence(target(api))
    expect(await sweepTurnDirs(root, presence)).toEqual([GONE])
    expect(await readdir(root)).toEqual([SESSION])
  })
})

describe("a forget's last rows over the CRUD route", () => {
  it('posts to the session path as a lowercase UUID', async () => {
    const api = new FakeApi()
    expect(await new CrudForgetRows(target(api)).forget(SESSION.toUpperCase())).toBe(3)
    expect(api.seen).toEqual([{ method: 'POST', path: `/sessions/${SESSION}/forget-rows`, body: undefined }])
  })

  it('sends nothing for an id that is no UUID', async () => {
    const api = new FakeApi()
    await expect(new CrudForgetRows(target(api)).forget('../areas')).rejects.toThrow(/not a session id/)
    expect(api.seen).toEqual([])
  })

  it("forgets a session through every step's route", async () => {
    const api = new FakeApi()
    const finished: string[] = []
    const done = await forgetSubject(
      {
        crud: { forget: () => Promise.resolve(true) },
        tasks: {
          list: () => Promise.resolve([{ id: 'task-1' }] as never),
          finish: (id) => {
            finished.push(id)
            return Promise.resolve(true)
          },
        },
        reads: { openCalls: () => Promise.resolve([]) },
        rows: new CrudForgetRows(target(api)),
      },
      `session-${SESSION}`,
    )
    expect(done).toEqual({ key: true, workflow: 'not_reached', rows: 3 })
    expect(finished).toEqual(['task-1'])
  })

  it("refuses, and deletes no rows, when the API cannot copy a fork's ranges", async () => {
    const api = new FakeApi()
    await expect(
      forgetSubject(
        {
          crud: { forget: () => Promise.resolve('forks') },
          tasks: { list: () => Promise.resolve([]), finish: () => Promise.resolve(false) },
          reads: { openCalls: () => Promise.resolve([]) },
          rows: new CrudForgetRows(target(api)),
        },
        `session-${SESSION}`,
      ),
    ).rejects.toSatisfy((err: unknown) => err instanceof ForksNeedKeys && /cancelled \(no Temporal client, so no workflow or tool call was terminated\), but its key, transcript and rows remain/.test(err.message))
    expect(api.seen).toEqual([])
  })

  it("the operator's forget takes every step from the API", async () => {
    const api = new FakeApi()
    const routes = forgetRoutes(target(api))
    expect(routes.crud).toBeInstanceOf(CrudSubjectKeys)
    expect(routes.reads).toBeInstanceOf(CrudSessionReads)
    expect(routes.rows).toBeInstanceOf(CrudForgetRows)
    expect(await routes.tasks.list(SESSION)).toEqual([])
    expect(api.seen.map((r) => `${r.method} ${r.path}`)).toEqual(['GET /background-tasks'])
  })
})
