import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import type { Database } from '../src/db.js'
import { canonicalJson, operationKey, OperationStore } from '../src/operations/store.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase } from './support/postgres.js'

// ai_operations, the agent's command record (spec 2026-10-01 §4.2, #1055).

describe('operation keys', () => {
  it("hash the kind, subject, canonical body and client key as the backend's do", () => {
    expect(canonicalJson({ b: 1, a: [true, null, 'x'], c: { z: 1, y: 2 } })).toBe('{"a":[true,null,"x"],"b":1,"c":{"y":2,"z":1}}')
    const key = operationKey('plugin_package_install', 'git.test', { source: { url: 'u', kind: 'git' } }, 'k1')
    expect(key).toMatch(/^[0-9a-f]{64}$/)
    expect(operationKey('plugin_package_install', 'git.test', { source: { kind: 'git', url: 'u' } }, 'k1')).toBe(key)
    expect(operationKey('plugin_package_install', 'git.test', { source: { kind: 'git', url: 'u' } }, 'k2')).not.toBe(key)
  })
})

describe.skipIf(!TEST_DATABASE_URL)(`ai_operations${TEST_DATABASE_URL ? '' : ` (skipped: ${TEST_DATABASE_URL_ENV} is not set)`}`, () => {
  let db: Database
  let drop: () => Promise<void>
  let store: OperationStore

  beforeEach(async () => {
    ;({ db, drop } = await throwawayDatabase())
    expect(await db.ready()).toBe(true)
    store = new OperationStore(db.sql)
  })
  afterEach(async () => {
    await drop()
  })

  const op = (id: string, run = 'r1', key = 'k') => ({
    id,
    kind: 'plugin_package_install',
    subject: 'git.test',
    operationKey: key,
    request: { source: { kind: 'git', url: 'https://git.test/a.git' } },
    workflowId: 'op-plugin_package_install-k',
    workflowRunId: run,
  })

  it('records an execution once, whatever its retried insert says', async () => {
    const first = await store.insert(op('a'))
    const again = await store.insert(op('b'))
    expect(again).toEqual(first)
    expect(first).toMatchObject({ id: 'a', status: 'running', result: null, error: null, finished_at: null })
    expect(await store.get('b')).toBeUndefined()
  })

  it('ends a running operation once', async () => {
    await store.insert(op('a'))
    const done = await store.finish('a', { result: { name: 'greeter' } })
    expect(done).toMatchObject({ status: 'succeeded', result: { name: 'greeter' } })
    expect(done.finished_at).not.toBeNull()
    const late = await store.finish('a', { error: { status: 500, title: 'x', detail: 'y' } })
    expect(late).toEqual(done)
  })

  it("finds a key's newest operation, and prunes finished ones past the retention", async () => {
    await store.insert(op('a', 'r1', 'same'))
    await store.finish('a', { error: { status: 422, title: 'Unprocessable', detail: 'no', extensions: { problems: ['p'] } } })
    await db.sql`UPDATE ai_operations SET created_at = now() - interval '1 minute', finished_at = now() - interval '1 minute'`
    await store.insert(op('b', 'r2', 'same'))
    expect((await store.find('same'))?.id).toBe('b')
    expect(await store.find('other')).toBeUndefined()
    await store.insert(op('c', 'r3', 'third'), 30)
    expect(await store.get('a')).toBeUndefined()
    expect(await store.get('b')).toMatchObject({ status: 'running' })
  })
})
