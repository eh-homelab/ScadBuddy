import { afterAll } from 'vitest'
import { waitForClaudeCodes } from './claudeProcesses.js'

// A test file may leave no Claude Code process running when it ends (#5030).
// One that outlived its file kept running after vitest's worker was gone and
// sent its Messages API calls to whatever server the OS gave its fake
// Anthropic's port to next (CI run 41033323089: two `POST /v1/messages` at
// another file's MCP trap). A closed query ends its process on its own: on
// stdin EOF Claude Code exits within about 50 ms (sessions/warm.ts DRAIN_MS),
// and the SDK sends SIGTERM 2 s after `close()` to one that has not. So the
// file gets that long and some room, then fails, and the leftovers are killed.
const EXIT_WAIT_MS = 10_000

afterAll(async () => {
  const left = await waitForClaudeCodes(EXIT_WAIT_MS)
  if (left.length === 0) return
  for (const { pid } of left) {
    try {
      process.kill(pid, 'SIGKILL')
    } catch {
      // ended meanwhile
    }
  }
  throw new Error(
    `${left.length} Claude Code process(es) still ran ${EXIT_WAIT_MS} ms after this file's tests ended (killed: ${left
      .map((p) => p.pid)
      .join(', ')}). Close every query, warm process and session manager the tests start (test/support/sessions.ts stopManagers).`,
  )
})
