import { Client } from '@temporalio/client'
import type { TestWorkflowEnvironment } from '@temporalio/testing'
import { Worker } from '@temporalio/worker'
import { randomUUID } from 'node:crypto'
import { mkdtempSync, readdirSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { afterAll, beforeAll, describe, expect, it } from 'vitest'
import { FilePayloadStore, OFFLOAD_BYTES, type PayloadKeys, subjectDataConverter } from '../src/temporal/payloadCodec.js'
import { localTemporal, TEMPORAL_CLI, TEMPORAL_SKIP } from './support/temporal.js'

// A session's large payloads on a real Temporal (#2243, #3739): kept on the data share by
// reference, and read back through the SDK's own External Storage walk, which refuses a
// reference before any codec sees it unless a driver for it is configured. A fork reads a
// long conversation as query pages over OFFLOAD_BYTES, and each one came back refused.

class MemoryKeys implements PayloadKeys {
  readonly #keys = new Map<string, Buffer>()
  async keyFor(subject: string): Promise<Buffer> {
    let key = this.#keys.get(subject)
    if (!key) this.#keys.set(subject, (key = Buffer.alloc(32, this.#keys.size + 1)))
    return key
  }
}

describe.skipIf(!TEMPORAL_CLI)(`a session's payloads kept by reference${TEMPORAL_SKIP}`, () => {
  let env: TestWorkflowEnvironment
  let root: string
  beforeAll(async () => {
    env = await localTemporal()
    root = mkdtempSync(join(tmpdir(), 'payloads-'))
  }, 60_000)
  afterAll(async () => {
    await env?.teardown()
    if (root) rmSync(root, { recursive: true, force: true })
  })

  it('reads a query result, a workflow input, a result and an activity input stored by reference', async () => {
    const subject = `session-${randomUUID()}`
    const dataConverter = subjectDataConverter(new MemoryKeys(), new FilePayloadStore(root))
    const client = new Client({ connection: env.connection, namespace: 'default', dataConverter })
    const taskQueue = `store-${subject}`
    const worker = await Worker.create({
      connection: env.nativeConnection,
      taskQueue,
      workflowsPath: fileURLToPath(new URL('./support/codecWorkflows.ts', import.meta.url)),
      dataConverter,
      activities: { lengthOf: async (text: string) => text.length },
    })
    const words = 'a long conversation '.repeat(Math.ceil((OFFLOAD_BYTES * 2) / 20))
    await worker.runUntil(async () => {
      const handle = await client.workflow.start('holdText', { workflowId: subject, taskQueue, args: [words] })
      expect(await handle.query<string>('held_text')).toBe(words)
      await handle.signal('finish')
      expect(await handle.result()).toBe(`${words} (done)`)
      // An activity's input over OFFLOAD_BYTES: the agent-tools worker's own walk.
      expect(await client.workflow.execute('toolInput', { workflowId: `session-${randomUUID()}`, taskQueue, args: [words] })).toBe(words.length)
    })
    expect(readdirSync(join(root, subject)).length).toBeGreaterThan(0)
  })
})
