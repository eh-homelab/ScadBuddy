import { OperationRefusal, type OperationKind } from '../../src/operations/kinds.js'
import type { CommandOutcome, Commands } from '../../src/operations/run.js'
import type { Operation } from '../../src/operations/store.js'

// The agent's commands run in-process, for route tests that exercise a kind's check
// and run against real stores without a Temporal server. AgentCommands
// (src/operations/run.ts) is what the service uses; agentOperation.temporal.test.ts
// covers the workflow, and pluginPackages.temporal.test.ts the routes on it.

export class InlineCommands implements Commands {
  readonly #kinds: Map<string, OperationKind>

  constructor(kinds: readonly OperationKind[]) {
    this.#kinds = new Map(kinds.map((k) => [k.name, k]))
  }

  async run(kind: string, request: Record<string, unknown>): Promise<CommandOutcome> {
    const k = this.#kinds.get(kind)!
    try {
      return { status: 'done', result: await k.run(request, await k.check(request)) }
    } catch (err) {
      if (err instanceof OperationRefusal) return { status: 'problem', problem: err.problem }
      throw err
    }
  }

  async get(): Promise<Operation | undefined> {
    return undefined
  }
}
