import { WorkflowFailedError } from '@temporalio/client'
import { ActivityFailure, TimeoutFailure, TimeoutType } from '@temporalio/common'
import type { Payload, PayloadCodec, SerializationContext } from '@temporalio/common'
import type { TestWorkflowEnvironment } from '@temporalio/testing'
import { Worker } from '@temporalio/worker'
import { randomUUID } from 'node:crypto'
import { setTimeout as sleep } from 'node:timers/promises'
import { fileURLToPath } from 'node:url'
import { afterAll, beforeAll, describe, expect, it } from 'vitest'
import { z } from 'zod'
import { unwrapUntrusted } from '../src/safety/untrusted.js'
import { SESSION_TURN_ACTIVITY } from '../src/temporal/session/names.js'
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
  async encode(payloads: Payload[], context?: SerializationContext): Promise<Payload[]> {
    this.contexts.push(context)
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
      expect(JSON.parse(unwrapUntrusted(content as string))).toEqual({ said: 'hi', session: SESSION })
      // §6.5's gate for phase 4: a codec on this worker learns which workflow a
      // tool's arguments belong to, so it can pick that session's key.
      expect(codec.contexts).toContainEqual(expect.objectContaining({ type: 'activity', workflowId, activityId: 'tool-toolu_9' }))
    } finally {
      await agent.stop()
    }
    expect(agent.state()).not.toBe('connecting')
  }, 120_000)

  it("runs a flow run's tool call as its starter (#1057)", async () => {
    const run = randomUUID()
    const agent = AgentWorker.start({
      address: env.address,
      namespace: 'default',
      activities: toolActivities([echo], {
        services: services(),
        sessions: { ownerOf: async () => undefined },
        flows: {
          startedBy: async (id) => (id === run ? { kind: 'browser', id: 'browser', label: 'You' } : undefined),
          approved: async () => false,
        },
      }),
    })
    const flowWorker = await Worker.create({
      connection: env.nativeConnection,
      taskQueue: 'project-workflow-standin',
      workflowsPath: fileURLToPath(new URL('./support/toolWorkflows.ts', import.meta.url)),
    })
    try {
      await agent.running()
      const content = await flowWorker.runUntil(
        env.client.workflow.execute('callTool', {
          workflowId: `flow-${run}`,
          taskQueue: 'project-workflow-standin',
          args: ['echo', { say: 'hi' }, 'call_1'],
        }),
      )
      expect(JSON.parse(unwrapUntrusted(content as string))).toEqual({ said: 'hi' })
    } finally {
      await agent.stop()
    }
  }, 120_000)

  it("keeps a flow call's image out of the run's history: a pointer crosses, never the bytes (#2477)", async () => {
    const run = randomUUID()
    const PNG = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII='
    const bytes = Buffer.from(PNG, 'base64')
    const pic = defineTool({
      name: 'pic',
      description: 'an image',
      input: z.object({}),
      risk: 'read',
      routes: [],
      handler: async () => ({ content: [{ type: 'image' as const, data: PNG, mimeType: 'image/png' }] }),
    })
    const kept = new Map<string, Buffer>()
    const agent = AgentWorker.start({
      address: env.address,
      namespace: 'default',
      activities: toolActivities([pic], {
        services: services(),
        sessions: { ownerOf: async () => undefined },
        flows: {
          startedBy: async (id) => (id === run ? { kind: 'browser', id: 'browser', label: 'You' } : undefined),
          approved: async () => false,
        },
        flowBlobs: {
          put: async (subject, images) => {
            images.forEach((i) => kept.set(`${subject}/${i.name}`, i.bytes))
            return images.map((i) => i.name)
          },
          get: async () => undefined,
        },
      }),
    })
    const flowWorker = await Worker.create({
      connection: env.nativeConnection,
      taskQueue: 'project-workflow-standin',
      workflowsPath: fileURLToPath(new URL('./support/toolWorkflows.ts', import.meta.url)),
    })
    try {
      await agent.running()
      const workflowId = `flow-${run}`
      const answer = (await flowWorker.runUntil(
        env.client.workflow.execute('callTool', { workflowId, taskQueue: 'project-workflow-standin', args: ['pic', {}, 'call_1'] }),
      )) as { images: { name: string; bytes: number }[] }
      const [image] = answer.images
      expect(image).toMatchObject({ mediaType: 'image/png', bytes: bytes.length })
      expect(kept.get(`${workflowId}/${image!.name}`)).toEqual(bytes)
      // No payload of the run's history holds the image: not its bytes, not their base64.
      const history = await env.client.workflow.getHandle(workflowId).fetchHistory()
      // Every byte string anywhere in the history, as text: payload data and metadata alike.
      const found: Buffer[] = []
      const walk = (value: unknown): void => {
        if (value instanceof Uint8Array) found.push(Buffer.from(value))
        else if (Array.isArray(value)) value.forEach(walk)
        else if (value && typeof value === 'object') Object.values(value).forEach(walk)
      }
      walk(history.events)
      const all = Buffer.concat(found)
      expect(all.toString('latin1')).toContain(image!.name)
      expect(all.includes(bytes.subarray(0, 8))).toBe(false)
      expect(all.toString('latin1')).not.toContain(PNG.slice(0, 24))
    } finally {
      await agent.stop()
    }
  }, 120_000)

  it('beats for every activity, so one that outlives its heartbeat timeout completes (#4261)', async () => {
    // `slow` never beats: only the worker's interceptor (heartbeatingActivities) keeps it alive.
    // Without it in Worker.create's interceptors, the call fails on its heartbeat timeout.
    // Timing (#4352): the interceptor beats every 200 ms, but the SDK sends at most one beat
    // per 0.8 × the 2 s timeout, so the server hears one every ~1.6 s; the 4 s sleep spans
    // two timeouts. `session_turn`, which the interceptor leaves alone, is the control: the
    // same call fails on that timeout, so the test cannot pass with no timeout enforced.
    // Its own queue, so no other `agent-tools` poller on this dev server is offered either.
    const taskQueue = `heartbeat-${randomUUID()}`
    const release = new AbortController()
    const agent = AgentWorker.start({
      address: env.address,
      namespace: 'default',
      taskQueue,
      activities: {
        slow: async () => (await sleep(4_000), 'done'),
        // Cut short once the control has failed, so stop() does not wait out its sleep:
        // without beats the worker is never told of the timeout.
        [SESSION_TURN_ACTIVITY]: () => sleep(60_000, 'done', { signal: release.signal }).catch(() => 'released'),
      },
      activityHeartbeatMs: 200,
    })
    const caller = await Worker.create({
      connection: env.nativeConnection,
      taskQueue: 'heartbeat-standin',
      workflowsPath: fileURLToPath(new URL('./support/toolWorkflows.ts', import.meta.url)),
    })
    const call = (name: string) =>
      env.client.workflow.execute('callBeating', {
        workflowId: `beating-${randomUUID()}`,
        taskQueue: 'heartbeat-standin',
        args: [name, 2_000, taskQueue],
      })
    try {
      await agent.running()
      await caller.runUntil(async () => {
        expect(await call('slow')).toBe('done')
        const failed = await call(SESSION_TURN_ACTIVITY).then(
          () => undefined,
          (error: unknown) => error,
        )
        expect(failed).toBeInstanceOf(WorkflowFailedError)
        const activity = failed instanceof WorkflowFailedError ? failed.cause : undefined
        expect(activity).toBeInstanceOf(ActivityFailure)
        const timeout = activity?.cause
        expect(timeout).toBeInstanceOf(TimeoutFailure)
        expect(timeout instanceof TimeoutFailure ? timeout.timeoutType : undefined).toBe(TimeoutType.HEARTBEAT)
      })
    } finally {
      // Before stop(): stop() waits for the control's sleep, which only this ends.
      release.abort()
      await agent.stop()
    }
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
