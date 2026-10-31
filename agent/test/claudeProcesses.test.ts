import { spawn } from 'node:child_process'
import { existsSync } from 'node:fs'
import { describe, expect, it } from 'vitest'
import { bundledCliPath } from '../src/harness/cliVersion.js'
import { liveClaudeCodes, waitForClaudeCodes } from './support/claudeProcesses.js'

let cliMissing: string | undefined
try {
  bundledCliPath()
} catch (err) {
  cliMissing = (err as Error).message
}
const skip = cliMissing ?? (existsSync('/proc/self/stat') ? undefined : 'no /proc')

// The leak guard (test/support/leakGuard.ts, #5030) sees only what this finds:
// were the bundled binary's path to change, it would pass every file.
describe.skipIf(skip !== undefined)(`the Claude Code processes below this one${skip ? ` (skipped: ${skip})` : ''}`, () => {
  it('include a running bundled Claude Code, and not once it has exited', async () => {
    // stdin left open: a streaming-input Claude Code waits on it, as a warm one does.
    const child = spawn(bundledCliPath(), ['-p', '--input-format', 'stream-json', '--output-format', 'stream-json', '--verbose'], {
      stdio: ['pipe', 'ignore', 'ignore'],
      env: { PATH: process.env.PATH, HOME: process.env.HOME, ANTHROPIC_API_KEY: 'unused', ANTHROPIC_BASE_URL: 'http://127.0.0.1:9' },
    })
    const exited = new Promise((resolve) => child.once('exit', resolve))
    try {
      expect(liveClaudeCodes().map((p) => p.pid)).toContain(child.pid)
    } finally {
      child.kill('SIGKILL')
    }
    await exited
    expect(await waitForClaudeCodes(0)).toEqual([])
  })
})
