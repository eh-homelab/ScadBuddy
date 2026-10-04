import type { DataConverter } from '@temporalio/common'
import { NativeConnection, Worker, type WorkerOptions } from '@temporalio/worker'
import { setTimeout as sleep } from 'node:timers/promises'
import { TASK_QUEUE } from './names.js'

// The agent service's Temporal worker (spec 2026-10-01 §4.3, §6.3, #1055): one worker
// on `agent-tools`, serving every tool as an activity (toolActivities.ts) and the
// agent's commands (AgentOperation). Started with the service, it connects in the
// background and keeps trying, as the database and the event listener do, so a
// Temporal that is down at start-up delays the worker, never the pod; /healthz says
// which. Unversioned: it pins nothing that a drain would wait for (plan ruling 9).
// `stop()` is the service's first shutdown step: the worker stops polling and its
// running activities get `shutdownGraceMs` to finish before they are cancelled.

export type TemporalHealth = 'connecting' | 'ok' | 'unavailable'

export type AgentWorkerOptions = {
  address: string
  namespace: string
  taskQueue?: string
  activities: NonNullable<WorkerOptions['activities']>
  /** The AgentOperation workflow: the build's bundle, or its source in tests. */
  workflows?: Pick<WorkerOptions, 'workflowBundle' | 'workflowsPath'>
  dataConverter?: DataConverter
  shutdownGraceMs?: number
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

  async stop(): Promise<void> {
    this.#stopping = true
    this.#wake.abort()
    if (this.#worker?.getState() === 'RUNNING') this.#worker.shutdown()
    await this.#loop
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
          activities: options.activities,
          ...options.workflows,
          ...(options.dataConverter ? { dataConverter: options.dataConverter } : {}),
          shutdownGraceTime: options.shutdownGraceMs ?? 10_000,
        })
        this.#worker = worker
        if (this.#stopping) break
        const run = worker.run()
        this.#state = 'ok'
        await run
      } catch (err) {
        this.#state = 'unavailable'
        log(`agent-tools worker: ${(err as Error).message}; retrying`)
      } finally {
        this.#worker = undefined
        await connection?.close().catch(() => undefined)
      }
      if (this.#stopping) break
      this.#state = this.#state === 'ok' ? 'unavailable' : this.#state
      await sleep(options.retryMs ?? 5_000, undefined, { signal: this.#wake.signal }).catch(() => undefined)
    }
  }
}
