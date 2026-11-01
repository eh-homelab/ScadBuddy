import type { DataConverter } from '@temporalio/common'
import type { Context as ActivityContext } from '@temporalio/activity'
import { type ActivityInterceptors, NativeConnection, Worker, type WorkerOptions } from '@temporalio/worker'
import { setTimeout as sleep } from 'node:timers/promises'
import { TASK_QUEUE } from './names.js'
import { SESSION_TURN_ACTIVITY } from './session/names.js'

// The agent service's Temporal worker (spec 2026-10-01 §4.3, §6.3, #1055): one worker
// on `agent-tools`, serving every tool as an activity (toolActivities.ts) and the
// agent's commands (AgentOperation). Started with the service, it connects in the
// background and keeps trying, as the database and the event listener do, so a
// Temporal that is down at start-up delays the worker, never the pod; /healthz says
// which. Versioned when it is given a deployment (plan 2026-10-13 Phase 11): every workflow
// AUTO_UPGRADE, so it pins nothing that a drain would wait for (plan ruling 9).
// `stop()` is the service's first shutdown step: the worker stops polling and its
// running activities get `shutdownGraceMs` to finish before they are cancelled; one
// that ignores the cancel is abandoned at `shutdownForceMs`, so stop() ends within the
// pod's 30 s whatever an activity does (the SDK's `shutdownForceTime`).

export type TemporalHealth = 'connecting' | 'ok' | 'unavailable'

/** How often every activity on this worker heartbeats, from its first moment. */
export const ACTIVITY_HEARTBEAT_MS = 10_000

/**
 * Every activity beats while it runs, from its start (#4199): a caller's heartbeat
 * timeout then tells an attempt Temporal recorded as started but never delivered (a
 * poll that gave up mid-handoff, or a replica a deploy took) within that timeout, not
 * at the end of a start-to-close sized for the slowest real call. `turn_tool_tier`
 * spends ~35 s connecting a turn's servers before it answers, with no beat of its own.
 * A tool's own beats (progress) go on beside these. Not `session_turn`'s: its beat is how
 * a stalled placement is noticed (its 20 s heartbeat timeout, affinity I3) and carries the
 * turn's `TurnBeat`, which a bare beat would overwrite (session/turnActivity.ts).
 */
export function heartbeatingActivities(everyMs = ACTIVITY_HEARTBEAT_MS): (ctx: ActivityContext) => ActivityInterceptors {
  return (ctx) => ({
    inbound: {
      async execute(input, next) {
        if (ctx.info.activityType === SESSION_TURN_ACTIVITY) return next(input)
        const beat = setInterval(() => ctx.heartbeat(), everyMs)
        try {
          return await next(input)
        } finally {
          clearInterval(beat)
        }
      },
    },
  })
}

/**
 * The identity a replica's workers poll under: `<pid>@<replica>`, the SDK's own default shape
 * (`<pid>@<hostname>`), which plugins/pluginSessions.ts `pollingReplicas` and
 * temporal/session/affinity.ts `liveHostQueues` read the replica back out of. Set explicitly so
 * tests may run several replicas in one process.
 */
export const workerIdentity = (replica: string): string => `${process.pid}@${replica}`
/** The replica behind a poller's identity (`workerIdentity`). */
export const replicaOfIdentity = (identity: string): string => identity.replace(/^\d+@/, '')

export type AgentWorkerOptions = {
  address: string
  namespace: string
  taskQueue?: string
  /** What the worker polls as (`workerIdentity`); the SDK's `<pid>@<hostname>` when omitted. */
  identity?: string
  activities: NonNullable<WorkerOptions['activities']>
  /** The AgentOperation workflow: the build's bundle, or its source in tests. */
  workflows?: Pick<WorkerOptions, 'workflowBundle' | 'workflowsPath'>
  dataConverter?: DataConverter
  /**
   * A Worker Deployment version (spec 2026-10-13 §5.1, D4; plan Phase 11): the worker is
   * versioned, its workflows `AUTO_UPGRADE`, and once it polls its build is made the
   * deployment's current version, which a versioned worker needs before it takes any task.
   * Made current once per process, never at a reconnect: a replica of an old build whose
   * connection blips late in a rollout must not take the deployment back. (An old-build
   * process that starts during a rollout still does; the clusters PostSync promote hook,
   * `temporal-promote-workers.yaml`, puts the deployed build back once the rollout ends.)
   * `queues` names the queues every worker of this build in the process polls
   * (`agent-tools`, `agent-sessions`), asked when the build is made current: it is made
   * current only once Temporal lists the version on each, since a queue the current build
   * does not poll routes to unversioned pollers, which a versioned deployment has none of.
   * Temporal's own check (`ignoreMissingTaskQueues` off) skips a queue with no recent tasks,
   * so it cannot be relied on (docs/superpowers/plans/2026-10-13-unified-agent-runtime-phase-11.md).
   * That check still refuses a build that stops polling a queue the current build polls with
   * recent tasks: a release that retires one of these queues must pass
   * `ignoreMissingTaskQueues` for it, or its rollout waits at /readyz.
   */
  deployment?: { name: string; buildId: string; queues?: () => readonly string[] }
  /** The most activities it runs at once; the SDK's default (100) when omitted. */
  maxConcurrentActivityTaskExecutions?: number
  /** How often each activity heartbeats (`heartbeatingActivities`); `ACTIVITY_HEARTBEAT_MS` when omitted. */
  activityHeartbeatMs?: number
  shutdownGraceMs?: number
  /** When the SDK gives up on running activities and the worker stops regardless. */
  shutdownForceMs?: number
  /** The wait between connection attempts. */
  retryMs?: number
  /**
   * How long a connection attempt may take before /healthz says `unavailable`. The SDK's
   * connect has no timeout of its own and keeps trying; the attempt goes on.
   */
  connectTimeoutMs?: number
  log?: (message: string) => void
}

export class AgentWorker {
  #state: TemporalHealth = 'connecting'
  #stopping = false
  /** Its build's make-current: started once per process, at the first poll. */
  #makingCurrent: Promise<void> | undefined
  #current = false
  /** The connection the worker polls on now; undefined between connections. */
  #connection: NativeConnection | undefined
  #worker: Worker | undefined
  readonly #loop: Promise<void>
  readonly #wake = new AbortController()
  readonly #stopped = new Promise<'stopped'>((resolve) =>
    this.#wake.signal.addEventListener('abort', () => resolve('stopped'), { once: true }),
  )

  private constructor(options: AgentWorkerOptions) {
    this.#loop = this.#run(options)
  }

  static start(options: AgentWorkerOptions): AgentWorker {
    return new AgentWorker(options)
  }

  state(): TemporalHealth {
    return this.#state
  }

  /** Resolves once the worker polls; for tests and the start-up log. */
  async running(timeoutMs = 30_000): Promise<void> {
    const until = Date.now() + timeoutMs
    while (this.#state !== 'ok') {
      if (Date.now() > until) throw new Error(`the agent-tools worker is still ${this.#state}`)
      await sleep(50)
    }
  }

  /**
   * Whether the worker's build has been made its deployment's current version, which
   * /readyz waits for (runtime/health.ts): true at once for an unversioned worker.
   */
  current(): boolean {
    return this.#current
  }

  async stop(): Promise<void> {
    this.#stopping = true
    this.#wake.abort()
    if (this.#worker?.getState() === 'RUNNING') this.#worker.shutdown()
    await this.#loop
    await this.#makingCurrent
  }

  /**
   * Retried until it takes or the worker stops, on whichever connection the worker has then: the
   * deployment may not know this version until its first poll, nor take it until this build
   * polls the current build's every queue.
   */
  async #makeCurrent(
    options: AgentWorkerOptions,
    deployment: NonNullable<AgentWorkerOptions['deployment']>,
    log: (message: string) => void,
  ): Promise<void> {
    while (!this.#stopping) {
      try {
        const connection = this.#connection
        if (!connection) throw new Error('not connected')
        const queues = deployment.queues?.() ?? []
        if (queues.length) {
          const { workerDeploymentVersionInfo: info } = await connection.workflowService.describeWorkerDeploymentVersion({
            namespace: options.namespace,
            deploymentVersion: { deploymentName: deployment.name, buildId: deployment.buildId },
          })
          const listed = new Set((info?.taskQueueInfos ?? []).map((q) => q.name))
          const missing = queues.filter((q) => !listed.has(q))
          if (missing.length) throw new Error(`this build does not poll ${missing.join(', ')} yet`)
        }
        await connection.workflowService.setWorkerDeploymentCurrentVersion({
          namespace: options.namespace,
          deploymentName: deployment.name,
          buildId: deployment.buildId,
          allowNoPollers: true,
        })
        this.#current = true
        log(`${options.taskQueue ?? TASK_QUEUE} worker: made build ${deployment.buildId} current for ${deployment.name}`)
        return
      } catch (err) {
        // Temporal's refusal names the queue: one this build should poll and does not (a worker
        // this process never started is not asked for, main.ts `versionedQueues`).
        log(`${options.taskQueue ?? TASK_QUEUE} worker: could not make build ${deployment.buildId} current for ${deployment.name} yet (${(err as Error).message}); retrying`)
        await sleep(options.retryMs ?? 5_000, undefined, { signal: this.#wake.signal }).catch(() => undefined)
      }
    }
  }

  async #run(options: AgentWorkerOptions): Promise<void> {
    const log = options.log ?? ((message: string) => console.error(message))
    while (!this.#stopping) {
      let connection: NativeConnection | undefined
      try {
        const connecting = NativeConnection.connect({ address: options.address })
        const slow = setTimeout(() => {
          if (this.#state === 'connecting') this.#state = 'unavailable'
        }, options.connectTimeoutMs ?? 10_000)
        const connected = await Promise.race([connecting, this.#stopped]).finally(() => clearTimeout(slow))
        if (connected === 'stopped') {
          // Stopped mid-connect: close the connection if it is made after all.
          connecting.then((c) => c.close(), () => undefined).catch(() => undefined)
          break
        }
        connection = connected
        const worker = await Worker.create({
          connection,
          namespace: options.namespace,
          taskQueue: options.taskQueue ?? TASK_QUEUE,
          ...(options.identity !== undefined ? { identity: options.identity } : {}),
          activities: options.activities,
          interceptors: { activity: [heartbeatingActivities(options.activityHeartbeatMs)] },
          ...options.workflows,
          ...(options.dataConverter ? { dataConverter: options.dataConverter } : {}),
          ...(options.deployment
            ? {
                workerDeploymentOptions: {
                  version: { deploymentName: options.deployment.name, buildId: options.deployment.buildId },
                  useWorkerVersioning: true as const,
                  defaultVersioningBehavior: 'AUTO_UPGRADE' as const,
                },
              }
            : {}),
          ...(options.maxConcurrentActivityTaskExecutions !== undefined
            ? { maxConcurrentActivityTaskExecutions: options.maxConcurrentActivityTaskExecutions }
            : {}),
          shutdownGraceTime: options.shutdownGraceMs ?? 10_000,
          shutdownForceTime: options.shutdownForceMs ?? 15_000,
        })
        this.#worker = worker
        this.#connection = connection
        if (this.#stopping) break
        const run = worker.run()
        this.#state = 'ok'
        if (!options.deployment) this.#current = true
        else this.#makingCurrent ??= this.#makeCurrent(options, options.deployment, log)
        await run
      } catch (err) {
        // Past shutdownForceTime the SDK gives up on the activities still running
        // (GracefulShutdownPeriodExpiredError, then the native worker refusing to close
        // under them): stopped all the same.
        if (this.#stopping) {
          log(`agent-tools worker: stopped with activities still running past their cancel (${(err as Error).message})`)
          break
        }
        this.#state = 'unavailable'
        log(`agent-tools worker: ${(err as Error).message}; retrying`)
      } finally {
        this.#worker = undefined
        this.#connection = undefined
        await connection?.close().catch(() => undefined)
      }
      if (this.#stopping) break
      this.#state = this.#state === 'ok' ? 'unavailable' : this.#state
      await sleep(options.retryMs ?? 5_000, undefined, { signal: this.#wake.signal }).catch(() => undefined)
    }
  }
}
