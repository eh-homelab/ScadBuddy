import { Client, WithStartWorkflowOperation } from '@temporalio/client'
import type { Payload, PayloadCodec, SerializationContext } from '@temporalio/common'
import type { TestWorkflowEnvironment } from '@temporalio/testing'
import { Worker } from '@temporalio/worker'
import { randomUUID } from 'node:crypto'
import { fileURLToPath } from 'node:url'
import { afterAll, beforeAll, describe, expect, it } from 'vitest'
import { z } from 'zod'
import { unwrapUntrusted } from '../src/safety/untrusted.js'
import { toolActivities } from '../src/temporal/toolActivities.js'
import { AgentWorker } from '../src/temporal/worker.js'
import { defineTool, json } from '../src/tools/registry.js'
import { services } from './helpers/mcp.js'
import { localTemporal, TEMPORAL_CLI, TEMPORAL_SKIP } from './support/temporal.js'

// The agent-tools worker against a Temporal dev server (spec 2026-10-01 §6.3, §8;
// #1055): a workflow on another queue, as the durable session's will be, calls a tool
// by name on `agent-tools`, and the agent service's worker runs it.

const SESSION = randomUUID()
const echo = defineTool({
  name: 'echo',
  description: 'echoes',
  input: z.object({ say: z.string() }),
  risk: 'read',
  routes: [],
  handler: async (args, ctx) => json({ said: args.say, session: ctx.session }),
})

/** A codec that changes nothing and records the context of every call (spec §6.5). */
class SpyCodec implements PayloadCodec {
  readonly contexts: (SerializationContext | undefined)[] = []
  /** What each encode carried, by its payloads' JSON data. */
  readonly encoded: { data: string[]; context: SerializationContext | undefined }[] = []
  async encode(payloads: Payload[], context?: SerializationContext): Promise<Payload[]> {
    this.contexts.push(context)
    this.encoded.push({ data: payloads.map((p) => Buffer.from(p.data ?? []).toString()), context })
    return payloads
  }
  async decode(payloads: Payload[], context?: SerializationContext): Promise<Payload[]> {
    this.contexts.push(context)
    return payloads
  }
}

describe.skipIf(!TEMPORAL_CLI)(`the agent-tools worker${TEMPORAL_SKIP}`, () => {
  let env: TestWorkflowEnvironment
  beforeAll(async () => {
    env = await localTemporal()
  }, 60_000)
  afterAll(async () => {
    await env?.teardown()
  })

  it("runs a session's tool call, and its codec sees the session's workflow id", async () => {
    const codec = new SpyCodec()
    const agent = AgentWorker.start({
      address: env.address,
      namespace: 'default',
      activities: toolActivities([echo], {
        services: services(),
        sessions: { ownerOf: async (id) => (id === SESSION ? { kind: 'browser', id: 'browser', label: 'You' } : undefined) },
      }),
      dataConverter: { payloadCodecs: [codec] },
    })
    const sessionWorker = await Worker.create({
      connection: env.nativeConnection,
      taskQueue: 'durable-session-standin',
      workflowsPath: fileURLToPath(new URL('./support/toolWorkflows.ts', import.meta.url)),
    })
    try {
      await agent.running()
      expect(agent.state()).toBe('ok')
      const workflowId = `session-${SESSION}`
      const content = await sessionWorker.runUntil(
        env.client.workflow.execute('callTool', {
          workflowId,
          taskQueue: 'durable-session-standin',
          args: ['echo', { say: 'hi' }, 'toolu_9'],
        }),
      )
      const [block] = content as { type: string; text: string }[]
      expect(JSON.parse(unwrapUntrusted(block!.text))).toEqual({ said: 'hi', session: SESSION })
      // §6.5's gate for phase 4: a codec on this worker learns which workflow a
      // tool's arguments belong to, so it can pick that session's key.
      expect(codec.contexts).toContainEqual(expect.objectContaining({ type: 'activity', workflowId, activityId: 'tool-toolu_9' }))
    } finally {
      await agent.stop()
    }
    expect(agent.state()).not.toBe('connecting')
  }, 120_000)

  // §6.5's gate for phase 5: a client's codec learns which session a durable turn's
  // start arguments and Update arguments belong to, so it can seal them under its key.
  it("gives a client's codec the session's workflow id for Update-with-Start", async () => {
    const codec = new SpyCodec()
    const client = new Client({ connection: env.connection, namespace: 'default', dataConverter: { payloadCodecs: [codec] } })
    const worker = await Worker.create({
      connection: env.nativeConnection,
      taskQueue: 'durable-session-standin',
      workflowsPath: fileURLToPath(new URL('./support/toolWorkflows.ts', import.meta.url)),
    })
    const workflowId = `session-${randomUUID()}`
    const result = await worker.runUntil(
      client.workflow.executeUpdateWithStart('send', {
        args: ['update-arg'],
        startWorkflowOperation: new WithStartWorkflowOperation('turns', {
          workflowId,
          taskQueue: 'durable-session-standin',
          args: ['start-arg'],
          workflowIdConflictPolicy: 'USE_EXISTING',
        }),
      }),
    )
    expect(result).toBe('got update-arg')
    await client.workflow.getHandle(workflowId).terminate()
    const context = { type: 'workflow', namespace: 'default', workflowId }
    expect(codec.encoded).toContainEqual({ data: ['"start-arg"'], context })
    expect(codec.encoded).toContainEqual({ data: ['"update-arg"'], context })
  }, 120_000)

  it('keeps trying while Temporal cannot be reached, and stops cleanly', async () => {
    const agent = AgentWorker.start({
      address: '127.0.0.1:1',
      namespace: 'default',
      activities: {},
      retryMs: 20,
      connectTimeoutMs: 50,
      log: () => {},
    })
    await expect.poll(() => agent.state(), { timeout: 30_000 }).toBe('unavailable')
    await agent.stop()
  }, 60_000)
})
