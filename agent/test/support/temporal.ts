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

/**
 * The client connection's first-contact deadline. The SDK's `ensureConnected` takes it from
 * `Date.now()`, the wall clock, which on some hosts steps by about ±11 s: with the default
 * 10 s, one forward step fails `createLocal` with "Failed to connect before the deadline"
 * however fast the server answers. Long enough that no step reaches it.
 */
const CONNECT_TIMEOUT = '5 minutes'

type CreateOptions = {
  server: { type: 'dev-server'; executable: { type: 'existing-path'; path: string } }
  supportsTimeSkipping: false
  connectionOptions: { connectTimeout: string }
}

export function localTemporal(): Promise<TestWorkflowEnvironment> {
  // What `createLocal` does, plus the connection options it does not pass on (SDK 1.24:
  // `create` is private in the types only; it is the static `createLocal` calls).
  const create = (TestWorkflowEnvironment as unknown as { create(o: CreateOptions): Promise<TestWorkflowEnvironment> })
    .create
  return create.call(TestWorkflowEnvironment, {
    server: { type: 'dev-server', executable: { type: 'existing-path', path: TEMPORAL_CLI! } },
    supportsTimeSkipping: false,
    connectionOptions: { connectTimeout: CONNECT_TIMEOUT },
  })
}
