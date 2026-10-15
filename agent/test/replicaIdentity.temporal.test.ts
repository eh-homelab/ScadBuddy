import { afterAll, beforeAll, describe, expect, it } from 'vitest'
import type { TestWorkflowEnvironment } from '@temporalio/testing'
import { fileURLToPath } from 'node:url'
import { pollingReplicas, replicaIdentity } from '../src/plugins/pluginSessions.js'
import { AgentWorker } from '../src/temporal/worker.js'
import { localTemporal, TEMPORAL_SKIP } from './support/temporal.js'

// The identity the sweep counts live (pluginSessions.ts) is the one a real agent-tools worker polls with.

describe.skipIf(TEMPORAL_SKIP !== '')(`replica identity against a real worker${TEMPORAL_SKIP}`, () => {
  let env: TestWorkflowEnvironment
  let agent: AgentWorker
  beforeAll(async () => {
    env = await localTemporal()
    agent = AgentWorker.start({
      address: env.address,
      namespace: 'default',
      activities: {},
      workflows: { workflowsPath: fileURLToPath(new URL('../src/temporal/workflows.ts', import.meta.url)) },
    })
    await agent.running(60_000)
  }, 90_000)
  afterAll(async () => {
    await agent?.stop()
    await env?.teardown()
  }, 30_000)

  it("DescribeTaskQueue's poller, less its <pid>@, is replicaIdentity()", async () => {
    await expect.poll(async () => [...(await pollingReplicas(env.client))], { timeout: 20_000 }).toContain(replicaIdentity())
  }, 60_000)
})
