import { randomUUID } from 'node:crypto'
import { setTimeout as sleep } from 'node:timers/promises'
import type { TestWorkflowEnvironment } from '@temporalio/testing'
import { bundleWorkflowCode, NativeConnection, Worker, type WorkerOptions, type WorkflowBundleWithSourceMap } from '@temporalio/worker'
import { afterAll, afterEach, beforeAll, describe, expect, it } from 'vitest'
import { AgentWorker } from '../src/temporal/worker.js'
import { localTemporal, TEMPORAL_CLI, TEMPORAL_SKIP } from './support/temporal.js'
import { until } from './support/until.js'
import { goSignal, type ProbeCall, type ProbeResult } from './support/versioningWorkflows.js'

// Plan 2026-10-13 Phase 11 (Tasks 11.1 and 11.2): how Worker Deployment versioning routes a
// shared queue's activity tasks while an old build and a new one both poll it, for each kind of
// caller that schedules onto `agent-tools`: a versioned AUTO_UPGRADE workflow of the same
// deployment (AgentSession), an unversioned workflow (an in-process ProjectWorkflow) and an
// AUTO_UPGRADE workflow of another deployment (prod's ProjectWorkflow, `scadbuddy-projects`).
// The first block records what Temporal does (the gate's measurement,
// docs/superpowers/plans/2026-10-13-unified-agent-runtime-phase-11.md); the second checks that
// AgentWorker, as main.ts runs it, makes a build current only once it serves both queues.

/** As the backend's UNREGISTERED_WAITS (workflows/flow_tools.py), in seconds. */
const UNREGISTERED_WAITS_S = [2, 4, 8]
const WORKFLOWS = new URL('./support/versioningWorkflows.ts', import.meta.url).pathname
type Counts = Record<string, number>

describe.skipIf(!TEMPORAL_CLI)(`worker versioning on a shared queue${TEMPORAL_SKIP}`, () => {
  let env: TestWorkflowEnvironment
  let bundle: WorkflowBundleWithSourceMap
  let connection: NativeConnection
  const workers: Worker[] = []
  const runs: Promise<void>[] = []
  const agentWorkers: AgentWorker[] = []

  beforeAll(async () => {
    env = await localTemporal()
    bundle = await bundleWorkflowCode({ workflowsPath: WORKFLOWS })
    connection = await NativeConnection.connect({ address: env.address })
  }, 120_000)
  afterEach(async () => {
    for (const w of workers.splice(0)) w.shutdown()
    await Promise.allSettled(runs.splice(0))
    await Promise.all(agentWorkers.splice(0).map((w) => w.stop()))
  })
  afterAll(async () => {
    await connection?.close()
    await env?.teardown()
  })

  const ns = () => env.namespace ?? 'default'
  const ran = (name: string) => () => Promise.resolve(name)
  const versioned = (deploymentName: string, buildId: string): Partial<WorkerOptions> => ({
    workerDeploymentOptions: { version: { deploymentName, buildId }, useWorkerVersioning: true, defaultVersioningBehavior: 'AUTO_UPGRADE' },
  })
  const start = async (options: Omit<WorkerOptions, 'connection' | 'namespace'>) => {
    const worker = await Worker.create({ connection, namespace: ns(), ...options })
    workers.push(worker)
    runs.push(worker.run())
    await until(() => worker.getState() === 'RUNNING', 10_000)
  }
  const setCurrent = async (deploymentName: string, buildId: string) => {
    // Retried: a version is known to the server only once its first poll arrives.
    await until(async () => {
      try {
        await env.client.workflowService.setWorkerDeploymentCurrentVersion({ namespace: ns(), deploymentName, buildId, ignoreMissingTaskQueues: true, allowNoPollers: true })
        return true
      } catch {
        await sleep(200)
        return false
      }
    }, 30_000)
  }
  const currentBuild = async (deploymentName: string) =>
    (await env.client.workflowService.describeWorkerDeployment({ namespace: ns(), deploymentName })).workerDeploymentInfo?.routingConfig
      ?.currentDeploymentVersion?.buildId
  /** `n` callers on `callerQueue` each run `call` once: how many ran on each build, or failed how. */
  const probe = async (callerQueue: string, call: ProbeCall, n = 8): Promise<Counts> => {
    const results = await Promise.all(
      Array.from({ length: n }, () =>
        env.client.workflow.execute('probeCaller', { taskQueue: callerQueue, workflowId: `probe-${randomUUID()}`, args: [call], workflowExecutionTimeout: '60s' }) as Promise<ProbeResult>,
      ),
    )
    const counts: Counts = {}
    for (const r of results) {
      const key = 'ran' in r ? r.ran : r.failed.startsWith('NotFoundError') ? 'NotFoundError' : r.failed
      counts[key] = (counts[key] ?? 0) + 1
    }
    return counts
  }

  /**
   * `n` callers each run `call` as a flow's call is run, asked again while the build that took it
   * does not register it: where each ended up, and how many needed more than one try.
   */
  const probeRetried = async (callerQueue: string, call: ProbeCall, n = 8): Promise<{ ran: Counts; retried: number }> => {
    const results = await Promise.all(
      Array.from({ length: n }, () =>
        env.client.workflow.execute('probeRetrying', {
          taskQueue: callerQueue,
          workflowId: `retried-${randomUUID()}`,
          args: [call, UNREGISTERED_WAITS_S],
          workflowExecutionTimeout: '2m',
        }) as Promise<ProbeResult & { tries: number }>,
      ),
    )
    const ran: Counts = {}
    for (const r of results) {
      const key = 'ran' in r ? r.ran : r.failed
      ran[key] = (ran[key] ?? 0) + 1
    }
    return { ran, retried: results.filter((r) => r.tries > 1).length }
  }

  it('routes every kind of caller to the current build, some after a retry, and a queue outside it to unversioned pollers only', async () => {
    const id = randomUUID().slice(0, 8)
    const [agent, projects] = [`agent-${id}`, `projects-${id}`]
    const q = {
      tools: `tools-${id}`,
      sessions: `sessions-${id}`,
      unversioned: `unversioned-${id}`,
      other: `other-${id}`,
      oldHost: `tools-${id}.old`,
      oldHostUnversioned: `tools-${id}.old-unversioned`,
      newHost: `tools-${id}.new`,
    }
    const callers = [q.sessions, q.unversioned, q.other] as const
    const fromEveryCaller = async (call: ProbeCall, n?: number) => Promise.all(callers.map((caller) => probe(caller, call, n)))

    await start({ taskQueue: q.unversioned, workflowBundle: bundle })
    await start({ taskQueue: q.other, workflowBundle: bundle, ...versioned(projects, 'p1') })
    await setCurrent(projects, 'p1')
    // The old build: its callers, the shared queue (no `a_new`) and its replica's own queue,
    // versioned and not.
    await start({ taskQueue: q.sessions, workflowBundle: bundle, ...versioned(agent, 'v1') })
    await start({ taskQueue: q.tools, activities: { whoami: ran('v1') }, ...versioned(agent, 'v1') })
    await start({ taskQueue: q.oldHost, activities: { whoami: ran('v1-host') }, ...versioned(agent, 'v1') })
    await start({ taskQueue: q.oldHostUnversioned, activities: { whoami: ran('v1-host-unversioned') } })
    await setCurrent(agent, 'v1')
    // Open across the rollout, as an AgentSession is: AUTO_UPGRADE, it moves to the new build.
    const waiter = await env.client.workflow.start('probeWaiter', {
      taskQueue: q.sessions,
      workflowId: `waiter-${id}`,
      args: [{ queue: q.tools, activity: 'a_new' }],
      workflowExecutionTimeout: '5m',
    })
    expect(await fromEveryCaller({ queue: q.tools, activity: 'whoami' })).toEqual([{ v1: 8 }, { v1: 8 }, { v1: 8 }])

    // The new build polls too; the old one is still current, so every caller still reaches it.
    await start({ taskQueue: q.sessions, workflowBundle: bundle, ...versioned(agent, 'v2') })
    await start({ taskQueue: q.tools, activities: { whoami: ran('v2'), a_new: ran('v2') }, ...versioned(agent, 'v2') })
    await start({ taskQueue: q.newHost, activities: { whoami: ran('v2-host') }, ...versioned(agent, 'v2') })
    await sleep(2_000)
    expect(await fromEveryCaller({ queue: q.tools, activity: 'a_new' })).toEqual([{ NotFoundError: 8 }, { NotFoundError: 8 }, { NotFoundError: 8 }])
    expect(await probe(q.sessions, { queue: q.oldHost, activity: 'whoami' }, 2)).toEqual({ 'v1-host': 2 })

    // Once the new build is current, every caller ends up on it, though the old one still polls.
    // Not at once for every caller: an unversioned or another deployment's caller is routed by
    // the activity queue's own copy of the routing, which can lag the switch. On a loaded CI
    // runner 7 of 8 unversioned and 4 of 8 other-deployment calls went to the old build first
    // (a versioned caller's 8 went to the new one); on an idle dev server none did (the check's
    // doc, §3). So a call is asked again after a wait, as a flow's is, and lands on the new build.
    await setCurrent(agent, 'v2')
    const switched = await Promise.all(callers.map((caller) => probeRetried(caller, { queue: q.tools, activity: 'a_new' })))
    expect(switched.map((r) => r.ran)).toEqual([{ v2: 8 }, { v2: 8 }, { v2: 8 }])
    console.info(`retried after the switch (AgentSession-like, unversioned, other deployment): ${switched.map((r) => r.retried).join(', ')} of 8`)
    await waiter.signal(goSignal)
    expect(await waiter.result()).toEqual({ ran: 'v2' })
    // The old replica's own queue is no member of the new build: versioned, its tasks reach no
    // one; unversioned, its own poller still takes them.
    expect(await probe(q.sessions, { queue: q.oldHost, activity: 'whoami', scheduleToStartSeconds: 5 }, 2)).toEqual({
      'activity ScheduleToStart timeout': 2,
    })
    expect(await fromEveryCaller({ queue: q.oldHostUnversioned, activity: 'whoami' }, 2)).toEqual([
      { 'v1-host-unversioned': 2 },
      { 'v1-host-unversioned': 2 },
      { 'v1-host-unversioned': 2 },
    ])
    expect(await probe(q.sessions, { queue: q.newHost, activity: 'whoami' }, 2)).toEqual({ 'v2-host': 2 })

    // A rollback to a build whose shared-queue worker is unversioned (before Phase 11): the queue
    // is no member of the current build, so its unversioned poller takes the tasks: every call
    // runs, and once the routing has settled (it lags the switch, as above) all of them there.
    await start({ taskQueue: q.sessions, workflowBundle: bundle, ...versioned(agent, 'v0') })
    await start({ taskQueue: q.tools, activities: { whoami: ran('v0-unversioned') } })
    await setCurrent(agent, 'v0')
    const allOnV0 = [{ 'v0-unversioned': 4 }, { 'v0-unversioned': 4 }, { 'v0-unversioned': 4 }]
    await until(async () => JSON.stringify(await fromEveryCaller({ queue: q.tools, activity: 'whoami' }, 4)) === JSON.stringify(allOnV0), 60_000)
  }, 300_000)

  it('makes a build current only once it polls both queues, and then runs every caller\'s new activity', async () => {
    const id = randomUUID().slice(0, 8)
    const deployment = `agent-${id}`
    const q = { tools: `tools-${id}`, sessions: `sessions-${id}`, unversioned: `unversioned-${id}` }
    const agentWorker = (taskQueue: string, buildId: string, activities: Record<string, () => Promise<string>>, workflows = false) => {
      const worker = AgentWorker.start({
        address: env.address,
        namespace: ns(),
        taskQueue,
        activities,
        ...(workflows ? { workflows: { workflowBundle: bundle } } : {}),
        // As main.ts gives both of a build's versioned workers (VERSIONED_QUEUES).
        deployment: { name: deployment, buildId, queues: [q.tools, q.sessions] },
        retryMs: 300,
        log: () => undefined,
      })
      agentWorkers.push(worker)
      return worker
    }
    await start({ taskQueue: q.unversioned, workflowBundle: bundle })
    const old = [agentWorker(q.sessions, 'v1', {}, true), agentWorker(q.tools, 'v1', { whoami: ran('v1') })]
    await until(() => old.every((w) => w.current()), 30_000)
    expect(await currentBuild(deployment)).toBe('v1')

    // The new build's agent-sessions worker alone is not made current while the build does not
    // poll agent-tools: Temporal itself would take it (the queue has had no tasks yet), and then
    // hand agent-tools' tasks to no one. Readiness (`current()`) waits too.
    const newSessions = agentWorker(q.sessions, 'v2', {}, true)
    await newSessions.running()
    await sleep(3_000)
    expect(newSessions.current()).toBe(false)
    expect(await currentBuild(deployment)).toBe('v1')

    const newTools = agentWorker(q.tools, 'v2', { whoami: ran('v2'), a_new: ran('v2') })
    await until(() => newSessions.current() && newTools.current(), 30_000)
    expect(await currentBuild(deployment)).toBe('v2')
    expect(old.map((w) => w.state())).toEqual(['ok', 'ok'])
    // Ready (current) means every caller's new activity runs on the new build: an unversioned
    // caller's after a retry at worst (the first test).
    expect((await probeRetried(q.sessions, { queue: q.tools, activity: 'a_new' })).ran).toEqual({ v2: 8 })
    expect((await probeRetried(q.unversioned, { queue: q.tools, activity: 'a_new' })).ran).toEqual({ v2: 8 })
  }, 120_000)

  it('is current at once when unversioned', async () => {
    const worker = AgentWorker.start({ address: env.address, namespace: ns(), taskQueue: `plain-${randomUUID().slice(0, 8)}`, activities: { whoami: ran('x') }, log: () => undefined })
    agentWorkers.push(worker)
    await worker.running()
    expect(worker.current()).toBe(true)
  })
})
