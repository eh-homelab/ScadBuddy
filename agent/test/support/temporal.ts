import { TestWorkflowEnvironment } from '@temporalio/testing'
import { execFileSync } from 'node:child_process'

// A Temporal dev server for the agent's `agent-tools` tests (spec 2026-10-01 §8, #1055),
// as the backend's `requires_temporal` tests get one: SCADBUDDY_TEST_TEMPORAL_DEV_SERVER
// names a Temporal CLI, else `temporal` on PATH; without either the tests skip. Never
// downloaded: CI installs the pinned CLI (ci.yml, the agent job).

export const TEMPORAL_CLI_ENV = 'SCADBUDDY_TEST_TEMPORAL_DEV_SERVER'

function onPath(): string | undefined {
  try {
    return execFileSync('which', ['temporal'], { encoding: 'utf8' }).trim() || undefined
  } catch {
    return undefined
  }
}

export const TEMPORAL_CLI = process.env[TEMPORAL_CLI_ENV]?.trim() || onPath()
export const TEMPORAL_SKIP = TEMPORAL_CLI ? '' : ` (skipped: ${TEMPORAL_CLI_ENV} is not set and no temporal on PATH)`

export function localTemporal(): Promise<TestWorkflowEnvironment> {
  return TestWorkflowEnvironment.createLocal({
    server: { executable: { type: 'existing-path', path: TEMPORAL_CLI! } },
  })
}
