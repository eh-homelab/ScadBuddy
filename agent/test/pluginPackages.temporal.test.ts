import type { TestWorkflowEnvironment } from '@temporalio/testing'
import { mkdtempSync, rmSync } from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it } from 'vitest'
import { createApp } from '../src/app.js'
import type { Database } from '../src/db.js'
import { originPolicy } from '../src/http/origins.js'
import { AgentCommands } from '../src/operations/run.js'
import { OperationStore } from '../src/operations/store.js'
import { PackageInstaller } from '../src/plugins/packages/install.js'
import { packageKinds } from '../src/plugins/packages/operations.js'
import { PackageStore, type PackageView } from '../src/plugins/packages/store.js'
import { operationActivities } from '../src/temporal/operationActivities.js'
import { AgentWorker } from '../src/temporal/worker.js'
import { gitMissing, gitRepo, GREETER, localFetcher, resolver, type TestRepo } from './support/gitRepo.js'
import { MemoryCredentials } from './support/memoryCredentials.js'
import { TEST_DATABASE_URL, throwawayDatabase } from './support/postgres.js'
import { localTemporal, TEMPORAL_CLI, TEMPORAL_SKIP } from './support/temporal.js'

// Installing a plugin package as a command, end to end (spec 2026-10-01 §4.2, §10 phase
// 4; #1055): the route, AgentCommands, AgentOperation on a Temporal dev server, the
// package kinds, Postgres and local git repositories.

const UI = { host: 'scadbuddy.example', origin: 'https://scadbuddy.example', 'x-forwarded-proto': 'https' }
const WORKFLOWS = fileURLToPath(new URL('../src/temporal/workflows.ts', import.meta.url))

const skip = !TEMPORAL_CLI || !TEST_DATABASE_URL || gitMissing !== undefined

describe.skipIf(skip)(`plugin package install on Temporal${TEMPORAL_SKIP}`, () => {
  let env: TestWorkflowEnvironment
  let db: Database
  let drop: () => Promise<void>
  let repos: Record<string, TestRepo>
  let cacheRoot: string
  let fetcher: ReturnType<typeof localFetcher>
  let agent: AgentWorker
  let app: ReturnType<typeof createApp>

  beforeAll(async () => {
    env = await localTemporal()
  }, 60_000)
  afterAll(async () => {
    await env?.teardown()
  })
  beforeEach(async () => {
    ;({ db, drop } = await throwawayDatabase())
    expect(await db.ready()).toBe(true)
    repos = { greeter: gitRepo(GREETER) }
    cacheRoot = mkdtempSync(path.join(os.tmpdir(), 'pkg-temporal-cache-'))
    fetcher = localFetcher(repos)
    const packages = new PackageStore(db.sql)
    const installer = new PackageInstaller({ fetcher, cacheRoot, resolve: resolver() })
    const kinds = packageKinds({ packages, installer })
    const store = new OperationStore(db.sql)
    agent = AgentWorker.start({
      address: env.address,
      namespace: 'default',
      activities: operationActivities(kinds, store),
      workflows: { workflowsPath: WORKFLOWS },
    })
    await agent.running(60_000)
    app = createApp({
      database: { ping: () => Promise.resolve(true), ready: () => db.ready() },
      backend: () => Promise.resolve(true),
      kek: { ok: false, reason: 'not configured' },
      credentials: new MemoryCredentials(),
      pluginPackages: packages,
      packageInstaller: installer,
      commands: new AgentCommands({ client: env.client, store, kinds, searchAttributes: false, deadlineMs: 30_000 }),
      testConnection: () => Promise.resolve({ ok: true, detail: '', duration_ms: 0, model: null }),
      remoteAddress: () => '10.0.0.7',
      origins: originPolicy('https://scadbuddy.example', '10.0.0.0/8'),
    })
  }, 90_000)
  afterEach(async () => {
    await agent.stop()
    await drop()
    for (const repo of Object.values(repos)) repo.remove()
    rmSync(cacheRoot, { recursive: true, force: true })
  })

  const install = (key: string, url = 'https://git.test/greeter.git') =>
    app.request('/api/v1/ai/plugin-packages', {
      method: 'POST',
      headers: { ...UI, 'content-type': 'application/json', 'Idempotency-Key': key },
      body: JSON.stringify({ source: { kind: 'git', url } }),
    })

  it('installs once per key: a re-send answers the first outcome and fetches nothing', async () => {
    const first = await install('k1')
    expect(first.status).toBe(201)
    const pkg = (await first.json()) as PackageView
    expect(pkg).toMatchObject({ name: 'greeter', approved: false })
    const again = await install('k1')
    expect(again.status).toBe(201)
    expect(await again.json()).toEqual(pkg)
    expect(fetcher.checkouts).toHaveLength(1)
    // A new press is a new command, refused by the store as before.
    const second = await install('k2')
    expect(second.status).toBe(409)
    expect(((await second.json()) as { detail: string }).detail).toMatch(/already installed/)
  }, 90_000)

  it('answers a refusal in the route’s words, and records none', async () => {
    const bad = await install('k3', 'ssh://git@github.com/o/r.git')
    expect(bad.status).toBe(400)
    const missing = await install('k4', 'https://git.test/nothing.git')
    expect(missing.status).toBe(502)
    expect(await db.sql`SELECT kind, status FROM ai_operations`).toEqual([{ kind: 'plugin_package_install', status: 'failed' }])
  }, 90_000)
})
