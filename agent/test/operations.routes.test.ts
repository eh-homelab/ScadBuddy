import { describe, expect, it } from 'vitest'
import { type AppDeps, createApp } from '../src/app.js'
import { originPolicy } from '../src/http/origins.js'
import type { CommandOutcome, Commands } from '../src/operations/run.js'
import type { Operation } from '../src/operations/store.js'
import type { PackageRepo } from '../src/plugins/packages/store.js'
import { MemoryCredentials } from './support/memoryCredentials.js'

// Plugin package install and re-pin answer as commands (spec 2026-10-01 §4.2, #1055),
// and GET /api/v1/ai/operations/{id} follows one; `Commands` is stood in for here
// (agentOperation.temporal.test.ts and pluginPackages.pg.test.ts run the real ones).

const UI = { host: 'scadbuddy.example', origin: 'https://scadbuddy.example', 'x-forwarded-proto': 'https' }
const READ = { host: 'scadbuddy.example', 'x-forwarded-proto': 'https', 'sec-fetch-site': 'same-origin' }
const KEY = '0123456789abcdef0123456789abcdef'

const RUNNING: Operation = {
  id: 'op1',
  kind: 'plugin_package_install',
  subject: 'git.test',
  status: 'running',
  result: null,
  error: null,
  created_at: '2026-10-04T00:00:00.000Z',
  finished_at: null,
}

class FakeCommands implements Commands {
  calls: [string, Record<string, unknown>, string | undefined][] = []
  private readonly outcome: CommandOutcome
  constructor(outcome: CommandOutcome) {
    this.outcome = outcome
  }
  async run(kind: string, request: Record<string, unknown>, key: string | undefined): Promise<CommandOutcome> {
    this.calls.push([kind, request, key])
    return this.outcome
  }
  async get(id: string): Promise<Operation | undefined> {
    return id === RUNNING.id ? RUNNING : undefined
  }
}

function app(commands: Commands | undefined) {
  const deps: AppDeps = {
    database: { ping: async () => true, ready: async () => true },
    backend: async () => true,
    kek: { ok: false, reason: 'not configured' },
    credentials: new MemoryCredentials(),
    pluginPackages: {} as PackageRepo,
    packageInstaller: { prepare: async () => undefined as never, evict: async () => undefined },
    commands,
    testConnection: async () => ({ ok: true, detail: '', duration_ms: 0, model: null }),
    remoteAddress: () => '10.0.0.7',
    origins: originPolicy('https://scadbuddy.example', '10.0.0.0/8'),
  }
  return createApp(deps)
}

function post(body: unknown, key?: string) {
  return {
    method: 'POST',
    headers: { ...UI, 'content-type': 'application/json', ...(key ? { 'Idempotency-Key': key } : {}) },
    body: JSON.stringify(body),
  }
}

describe('plugin package commands', () => {
  const source = { kind: 'git', url: 'https://git.test/greeter.git' }

  it("installs through the command, with the client's key, and answers 201 with the package", async () => {
    const commands = new FakeCommands({ status: 'done', result: { name: 'greeter' } })
    const res = await app(commands).request('/api/v1/ai/plugin-packages', post({ source }, KEY))
    expect(res.status).toBe(201)
    expect(await res.json()).toEqual({ name: 'greeter' })
    // The validated source crosses, defaults filled in.
    expect(commands.calls).toEqual([['plugin_package_install', { source: { ...source, ref: 'HEAD', path: '' } }, KEY]])
  })

  it('refuses a source with credentials, or a bad ref, with 400 before any command', async () => {
    // A command's request is its workflow's input, in Temporal history: a token in the URL
    // must never get that far.
    const commands = new FakeCommands({ status: 'done', result: {} })
    const credentialed = await app(commands).request(
      '/api/v1/ai/plugin-packages',
      post({ source: { kind: 'git', url: 'https://user:s3cret@git.test/greeter.git' } }, KEY),
    )
    expect(credentialed.status).toBe(400)
    const body = await credentialed.text()
    expect(body).toContain('must not carry credentials')
    expect(body).not.toContain('s3cret')
    const query = await app(commands).request(
      '/api/v1/ai/plugin-packages',
      post({ source: { kind: 'git', url: 'https://git.test/greeter.git?token=s3cret' } }),
    )
    expect(query.status).toBe(400)
    const ref = await app(commands).request('/api/v1/ai/plugin-packages/greeter/repin', post({ ref: '--upload-pack=x' }))
    expect(ref.status).toBe(400)
    expect(commands.calls).toEqual([])
  })

  it('re-pins through the command and answers 200', async () => {
    const commands = new FakeCommands({ status: 'done', result: { name: 'greeter', pending: {} } })
    const res = await app(commands).request('/api/v1/ai/plugin-packages/greeter/repin', post({ ref: 'main' }))
    expect(res.status).toBe(200)
    expect(commands.calls).toEqual([['plugin_package_repin', { name: 'greeter', ref: 'main' }, undefined]])
  })

  it('answers 202 with the operation while it runs', async () => {
    const res = await app(new FakeCommands({ status: 'running', operation: { ...RUNNING, repeated: true } })).request(
      '/api/v1/ai/plugin-packages',
      post({ source }, KEY),
    )
    expect(res.status).toBe(202)
    expect(await res.json()).toMatchObject({ id: 'op1', status: 'running', repeated: true })
  })

  it("answers a refusal as the route always has, every problem included, and a 503's Retry-After", async () => {
    const refused = await app(
      new FakeCommands({
        status: 'problem',
        problem: { status: 422, title: 'Unprocessable Content', detail: 'the plugin package is refused', extensions: { problems: ['p'] } },
      }),
    ).request('/api/v1/ai/plugin-packages', post({ source }))
    expect(refused.status).toBe(422)
    expect(await refused.json()).toEqual({ detail: 'the plugin package is refused', problems: ['p'] })
    const accepting = await app(
      new FakeCommands({
        status: 'problem',
        problem: { status: 503, title: 'Service Unavailable', type: 'https://scadbuddy.dev/problems/command-still-accepting', detail: 'd' },
        retryAfter: 2,
      }),
    ).request('/api/v1/ai/plugin-packages', post({ source }))
    expect(accepting.status).toBe(503)
    expect(accepting.headers.get('retry-after')).toBe('2')
    expect(await accepting.json()).toEqual({ detail: 'd', type: 'https://scadbuddy.dev/problems/command-still-accepting' })
  })

  it('answers 503 without Temporal, and 400 for a body that does not parse, before any command', async () => {
    const none = await app(undefined).request('/api/v1/ai/plugin-packages', post({ source }))
    expect(none.status).toBe(503)
    expect(((await none.json()) as { detail: string }).detail).toMatch(/SCADBUDDY_TEMPORAL_ADDRESS/)
    const commands = new FakeCommands({ status: 'done', result: {} })
    expect((await app(commands).request('/api/v1/ai/plugin-packages', post({ nope: 1 }))).status).toBe(400)
    expect(commands.calls).toEqual([])
  })
})

describe('GET /api/v1/ai/operations/{id}', () => {
  it('answers one operation, 404 for none, and only to the UI', async () => {
    const a = app(new FakeCommands({ status: 'done', result: {} }))
    const one = await a.request('/api/v1/ai/operations/op1', { headers: READ })
    expect(one.status).toBe(200)
    expect(await one.json()).toEqual(RUNNING)
    expect((await a.request('/api/v1/ai/operations/nope', { headers: READ })).status).toBe(404)
    expect((await a.request('/api/v1/ai/operations/op1', { headers: { ...READ, 'sec-fetch-site': 'cross-site' } })).status).toBe(403)
    expect((await app(undefined).request('/api/v1/ai/operations/op1', { headers: READ })).status).toBe(503)
  })
})
