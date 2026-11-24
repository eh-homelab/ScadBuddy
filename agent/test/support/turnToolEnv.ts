import type { Info } from '@temporalio/activity'
import { MockActivityEnvironment } from '@temporalio/testing'

/**
 * A mock `turn:` call's start-to-close: under vitest's `testTimeout` (20 s, vitest.config.ts),
 * so a hung call fails with the MCP timeout error that names it, not a generic test timeout
 * (#7441), and far above what a loaded runner takes (the #7342 call took 1.2 s there).
 * The bound is per call and `testTimeout` per test body, so the 5 s between them is shared: a
 * hung call that starts more than 5 s into its test (after slow earlier calls, say) reaches the
 * test timeout before its own MCP error can name it.
 */
export const TURN_TOOL_TEST_TIMEOUT_MS = 15_000

/**
 * A `MockActivityEnvironment` for a `turn:<server>__<tool>` call. turnToolActivity bounds the
 * MCP call by its start-to-close; the mock's own default is 1 s, so a call slowed by a loaded
 * runner failed with "MCP error -32001: Request timed out" (#7342). This one gets
 * `TURN_TOOL_TEST_TIMEOUT_MS`. A test of that bound passes `startToCloseTimeoutMs` itself.
 */
export const turnToolEnv = (info: Partial<Info>): MockActivityEnvironment =>
  new MockActivityEnvironment({ startToCloseTimeoutMs: TURN_TOOL_TEST_TIMEOUT_MS, ...info })
