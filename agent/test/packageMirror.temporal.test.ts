import type { TestWorkflowEnvironment } from '@temporalio/testing'
import { WorkflowFailedError } from '@temporalio/client'
import { fileURLToPath } from 'node:url'
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it } from 'vitest'
import { MirrorRefusedError } from '../src/plugins/packages/install.js'
import { mirrorWorkflowId, packageMirrorActivities, startMirrorWorkflow } from '../src/plugins/packages/mirrorJob.js'
import type { PackagePin } from '../src/plugins/packages/store.js'
import { AgentWorker } from '../src/temporal/worker.js'
import { localTemporal, TEMPORAL_CLI, TEMPORAL_SKIP } from './support/temporal.js'

// The PackageMirror workflow (#7907) on a Temporal dev server: one run per pin at a time, a
// new run after a finished one (a load that finds the mirror gone or bad must get git again,
// the owner's word on the issue), and a refusal never tried again. The installer is a fake:
// test/pluginPackagesCommitMirror.test.ts covers `makeMirror` itself.

const WORKFLOWS = fileURLToPath(new URL('../src/temporal/workflows.ts', import.meta.url))

const PIN: PackagePin = {
  name: 'greeter',
  fetchUrl: 'https://git.test/greeter.git',
  fetchPath: '',
  commit: 'a'.repeat(40),
  contentHash: `sha256:${'b'.repeat(64)}`,
}
const JOB = { name: PIN.name, commit: PIN.commit, contentHash: PIN.contentHash }

describe.skipIf(!TEMPORAL_CLI)(`the PackageMirror workflow${TEMPORAL_SKIP}`, () => {
  let env: TestWorkflowEnvironment
  let agent: AgentWorker
  let calls: number
  let gate: Promise<void>
  let open: () => void
  let refuse: boolean

  beforeAll(async () => {
    env = await localTemporal()
  }, 60_000)
  afterAll(async () => {
    await env?.teardown()
  })
  beforeEach(async () => {
    calls = 0
    refuse = false
    gate = new Promise((resolve) => (open = resolve))
    agent = AgentWorker.start({
      address: env.address,
      namespace: 'default',
      activities: packageMirrorActivities({
        packages: { filesOf: async (name, pending) => (name === PIN.name && !pending ? { pin: PIN, files: {} } : undefined) },
        installer: {
          makeMirror: async () => {
            calls += 1
            await gate
            if (refuse) throw new MirrorRefusedError('the files at that commit hash to something else')
            return { outcome: 'mirrored' as const, sha256: 'c'.repeat(64) }
          },
        },
      }),
      workflows: { workflowsPath: WORKFLOWS },
    })
    await agent.running(60_000)
  }, 90_000)
  afterEach(async () => {
    await agent.stop()
  })

  it('joins a running run, and starts a new one after it finished', async () => {
    const first = await startMirrorWorkflow(env.client, JOB)
    const joined = await startMirrorWorkflow(env.client, JOB)
    expect(first.workflowId).toBe(mirrorWorkflowId(JOB))
    expect(joined.firstExecutionRunId).toBe(first.firstExecutionRunId)
    open()
    expect(await first.result()).toEqual({ outcome: 'mirrored', sha256: 'c'.repeat(64) })
    expect(calls).toBe(1)
    // The run finished; the mirror is then found gone or bad, so a load starts it again.
    const again = await startMirrorWorkflow(env.client, JOB)
    expect(again.firstExecutionRunId).not.toBe(first.firstExecutionRunId)
    expect(await again.result()).toEqual({ outcome: 'mirrored', sha256: 'c'.repeat(64) })
    expect(calls).toBe(2)
  }, 60_000)

  it('answers not-pinned for a commit the row no longer names', async () => {
    open()
    const handle = await startMirrorWorkflow(env.client, { ...JOB, commit: 'e'.repeat(40) })
    expect(await handle.result()).toEqual({ outcome: 'not-pinned' })
    expect(calls).toBe(0)
  }, 60_000)

  it('never tries a refusal again', async () => {
    refuse = true
    open()
    const handle = await startMirrorWorkflow(env.client, JOB)
    const err = await handle.result().catch((e: unknown) => e)
    expect(err).toBeInstanceOf(WorkflowFailedError)
    expect(calls).toBe(1)
  }, 60_000)
})
