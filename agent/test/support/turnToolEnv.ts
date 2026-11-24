import type { Info } from '@temporalio/activity'
import { MockActivityEnvironment } from '@temporalio/testing'
import { TOOL_CALL_TIMEOUT_MS } from '../../src/tools/activityProjection.js'

/**
 * A `MockActivityEnvironment` for a `turn:<server>__<tool>` call, with the start-to-close a
 * real call gets (`TOOL_CALL_TIMEOUT_MS`, unless the request names one). turnToolActivity
 * bounds the MCP call by it; the mock's own default is 1 s, so a call slowed by a loaded runner
 * failed with "MCP error -32001: Request timed out" (#7342). A test of that bound passes
 * `startToCloseTimeoutMs` itself.
 */
export const turnToolEnv = (info: Partial<Info>): MockActivityEnvironment =>
  new MockActivityEnvironment({ startToCloseTimeoutMs: TOOL_CALL_TIMEOUT_MS, ...info })
