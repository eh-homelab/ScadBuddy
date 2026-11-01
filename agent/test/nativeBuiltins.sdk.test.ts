import { existsSync } from 'node:fs'
import { mkdir, mkdtemp, readFile, writeFile } from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'
import type { SDKMessage } from '@anthropic-ai/claude-agent-sdk'
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it } from 'vitest'
import { NATIVE_BUILTINS } from '../src/access/nativeBuiltins.js'
import type { Credential } from '../src/credentials.js'
import type { Database } from '../src/db.js'
import { startCredentialGateway, stopCredentialGateway } from '../src/gateway/server.js'
import { bundledCliPath } from '../src/harness/cliVersion.js'
import type { ApprovalRequest, RiskTier, ToolDecision } from '../src/harness/permissions.js'
import { type HarnessRun, runHarness } from '../src/harness/run.js'
import { ensureStateDirs, linkTaskList } from '../src/harness/stateDirs.js'
import { SessionDirectory } from '../src/workspace/directory.js'
import { SessionFiles } from '../src/workspace/store.js'
import { type FakeAnthropic, type RecordedRequest, type Reply, startFakeAnthropic } from './support/fakeAnthropic.js'
import { TEST_DATABASE_URL, throwawayDatabase } from './support/postgres.js'
import { browser, manager } from './support/sessions.js'

// Plan 2026-10-13 Step 9.6 against the bundled Claude Code: its own Read, Write, Edit, Glob and
// Grep work in the session's working directory and are refused outside it; its task list
// survives a move to another pod; Bash, when a package switches it on, parks as outward.

const TOKEN = 'gw-native-builtins-0000111122223333'

let cliMissing: string | undefined
try {
  bundledCliPath()
} catch (err) {
  cliMissing = (err as Error).message
}

const DEFAULTS = NATIVE_BUILTINS.filter((b) => b.access !== 'off').map((b) => ({ name: b.name, tier: b.access as RiskTier }))

describe.skipIf(cliMissing !== undefined)("Claude Code's own built-ins in a session", () => {
  let fake: FakeAnthropic
  let steps: Reply[]
  let requests: RecordedRequest[]
  beforeAll(async () => {
    await startCredentialGateway()
  })
  afterAll(async () => {
    await stopCredentialGateway()
  })
  beforeEach(async () => {
    requests = []
    fake = await startFakeAnthropic((r) => {
      requests.push(r)
      if (!(r.body?.tools ?? []).length) return { text: 'ok' }
      return steps.shift() ?? { text: 'done' }
    })
  })
  afterEach(async () => {
    await fake.close()
  })

  const gateway = (): Credential => ({ kind: 'gateway', baseUrl: fake.url, secret: TOKEN })
  /** Every tool result of the turn: the last model request carries the whole conversation. */
  const toolResults = () =>
    (requests.filter((r) => (r.body?.tools ?? []).length).at(-1)?.body?.messages ?? [])
      .flatMap((m) => (Array.isArray(m.content) ? m.content : []))
      .filter((b) => b.type === 'tool_result')

  async function turn(run: Partial<HarnessRun> & Pick<HarnessRun, 'paths' | 'cwd'>): Promise<{ decisions: [string, ToolDecision][]; offered: string[] }> {
    const decisions: [string, ToolDecision][] = []
    const messages: SDKMessage[] = []
    try {
      for await (const m of runHarness({
        credential: gateway(),
        prompt: 'go',
        maxTurns: 12,
        nativeTools: DEFAULTS,
        onDecision: (name, decision) => decisions.push([name, decision]),
        ...run,
      }))
        messages.push(m)
    } catch (err) {
      if (!messages.some((m) => m.type === 'result')) throw err
    }
    const offered = (requests.find((r) => (r.body?.tools ?? []).length)?.body?.tools ?? []).map((t) => t.name)
    return { decisions, offered }
  }

  const session = async () => {
    const stateDir = await mkdtemp(path.join(os.tmpdir(), 'native-state-'))
    const paths = { stateDir }
    await ensureStateDirs(paths)
    const cwd = await mkdtemp(path.join(os.tmpdir(), 'native-cwd-'))
    return { paths, cwd }
  }

  it('reads, writes, edits and finds files in the session directory', async () => {
    const { paths, cwd } = await session()
    const file = path.join(cwd, 'notes', 'a.txt')
    steps = [
      { toolUse: { name: 'Write', input: { file_path: file, content: 'hello world\n' } } },
      { toolUse: { name: 'Read', input: { file_path: file } } },
      { toolUse: { name: 'Edit', input: { file_path: file, old_string: 'world', new_string: 'there' } } },
      { toolUse: { name: 'Glob', input: { pattern: '**/*.txt' } } },
      { toolUse: { name: 'Grep', input: { pattern: 'there', output_mode: 'files_with_matches' } } },
    ]
    const { decisions, offered } = await turn({ paths, cwd })
    expect(offered).toEqual(expect.arrayContaining(['Read', 'Write', 'Edit', 'Glob', 'Grep', 'NotebookEdit', 'TaskCreate', 'TaskList']))
    expect(offered).not.toContain('Bash')
    expect(await readFile(file, 'utf8')).toBe('hello there\n')
    // Write and Edit ask canUseTool; Claude Code runs the read-only ones after the PreToolUse hook alone.
    expect(decisions.map(([n, d]) => `${n}:${d.decision}`)).toEqual(['Write:allow', 'Edit:allow'])
    const results = toolResults().map((b) => JSON.stringify(b.content))
    expect(toolResults().filter((b) => b.is_error)).toEqual([])
    expect(results).toHaveLength(5)
    expect(results[1]).toContain('hello world')
    expect(results[3]).toContain('a.txt')
    expect(results[4]).toContain('a.txt')
  }, 60_000)

  it('refuses a path outside the session directory', async () => {
    const { paths, cwd } = await session()
    const outside = await mkdtemp(path.join(os.tmpdir(), 'native-out-'))
    await writeFile(path.join(outside, 'secret'), 'the key')
    steps = [
      { toolUse: { name: 'Read', input: { file_path: path.join(outside, 'secret') } } },
      { toolUse: { name: 'Write', input: { file_path: path.join(outside, 'new.txt'), content: 'x' } } },
      { toolUse: { name: 'Grep', input: { pattern: 'key', path: outside } } },
      { toolUse: { name: 'Glob', input: { pattern: `${outside}/*` } } },
    ]
    const { decisions } = await turn({ paths, cwd })
    expect(decisions.map(([n, d]) => `${n}:${d.decision}`)).toEqual(['Read:deny', 'Write:deny', 'Grep:deny', 'Glob:deny'])
    expect(existsSync(path.join(outside, 'new.txt'))).toBe(false)
    expect(JSON.stringify(toolResults())).not.toContain('the key')
  }, 60_000)

  // Measured on 2.1.310: Read and Write expand `~` before the seam, but Glob's and Grep's
  // `path` reach it as `~` and the tool then searches HOME (the state dir); `$HOME`,
  // `${HOME}`, `~user` and a `~` in a pattern are taken as names in the directory.
  it('refuses a Glob pattern that climbs out through a brace alternative', async () => {
    const parent = await mkdtemp(path.join(os.tmpdir(), 'native-brace-'))
    const { paths } = await session()
    const cwd = path.join(parent, 'cwd')
    await mkdir(cwd)
    await writeFile(path.join(parent, 'sibling-secret.txt'), 'the key')
    steps = [{ toolUse: { name: 'Glob', input: { pattern: '{..,x}/*.txt' } } }]
    const { decisions } = await turn({ paths, cwd })
    expect(decisions.map(([n, d]) => `${n}:${d.decision}`)).toEqual(['Glob:deny'])
    expect(JSON.stringify(toolResults())).not.toContain('sibling-secret')
  }, 60_000)

  it('refuses a path that starts at a home directory', async () => {
    const { paths, cwd } = await session()
    await writeFile(path.join(paths.stateDir, 'marker'), 'HOME-MARKER')
    steps = [
      { toolUse: { name: 'Read', input: { file_path: '~/marker' } } },
      { toolUse: { name: 'Glob', input: { pattern: 'marker', path: '~' } } },
      // Read takes `~root/x` as a name in the directory and stops at "File does not exist"
      // before the seam; Glob's `path` reaches it.
      { toolUse: { name: 'Glob', input: { pattern: '*', path: '~root' } } },
      { toolUse: { name: 'Grep', input: { pattern: 'MARKER', path: '~' } } },
      { toolUse: { name: 'Grep', input: { pattern: 'MARKER', path: '~/marker' } } },
      { toolUse: { name: 'Write', input: { file_path: '~/written', content: 'w' } } },
    ]
    const { decisions } = await turn({ paths, cwd })
    expect(decisions.map(([n, d]) => `${n}:${d.decision}`)).toEqual(['Read:deny', 'Glob:deny', 'Glob:deny', 'Grep:deny', 'Grep:deny', 'Write:deny'])
    expect(toolResults().map((b) => JSON.stringify(b.content)).filter((t) => t.includes(paths.stateDir) && !t.includes('reaches only'))).toEqual([])
    expect(existsSync(path.join(paths.stateDir, 'written'))).toBe(false)
  }, 60_000)

  it('offers no file tool to a turn with no working directory', async () => {
    const { paths } = await session()
    steps = []
    const { offered } = await turn({ paths, cwd: undefined as unknown as string })
    expect(offered).toEqual(expect.arrayContaining(['TaskCreate', 'TaskList']))
    for (const name of ['Read', 'Write', 'Edit', 'NotebookEdit', 'Glob', 'Grep']) expect(offered).not.toContain(name)
  }, 60_000)

  it('parks Bash as outward when a package switches it on', async () => {
    const { paths, cwd } = await session()
    const asked: ApprovalRequest[] = []
    steps = [{ toolUse: { name: 'Bash', input: { command: 'cat /etc/hostname' } } }]
    const { offered } = await turn({
      paths,
      cwd,
      nativeTools: [...DEFAULTS, { name: 'Bash', tier: 'outward' }],
      approvalGate: async (request) => {
        asked.push(request)
        return { approved: false, message: 'no' }
      },
    })
    expect(offered).toContain('Bash')
    expect(asked.map((r) => [r.toolName, r.tier])).toEqual([['Bash', 'outward']])
  }, 60_000)

  describe.skipIf(!TEST_DATABASE_URL)('with the workspace as the record', () => {
    let db: Database
    let drop: () => Promise<void>
    beforeEach(async () => {
      ;({ db, drop } = await throwawayDatabase())
    })
    afterEach(async () => {
      await drop()
    })

    it('keeps the task list across a move to another pod, through the workspace', async () => {
      const files = new SessionFiles(db.sql)
      const m = manager({ sql: db.sql, paths: { stateDir: await mkdtemp(path.join(os.tmpdir(), 'native-m-')) } })
      const sessionId = (await m.start(browser, { origin: 'chat' })).session.id
      m.abortAll()
      await m.warm.close()
      // Pod A: a turn adds a task; its end writes the directory back.
      const podA = await session()
      const dirsA = new SessionDirectory(files, await mkdtemp(path.join(os.tmpdir(), 'native-root-a-')))
      const placedA = await dirsA.place(sessionId)
      await linkTaskList(podA.paths, placedA.dir, sessionId)
      steps = [{ toolUse: { name: 'TaskCreate', input: { subject: 'carry me across', description: 'a task' } } }]
      await turn({ paths: podA.paths, cwd: placedA.dir, sessionId })
      await dirsA.writeBack(placedA)
      dirsA.done(placedA)
      // Pod B: another state dir and disk; the placement refills the list.
      const podB = await session()
      const dirsB = new SessionDirectory(files, await mkdtemp(path.join(os.tmpdir(), 'native-root-b-')))
      const placedB = await dirsB.place(sessionId)
      await linkTaskList(podB.paths, placedB.dir, sessionId)
      requests = []
      steps = [{ toolUse: { name: 'TaskList', input: {} } }]
      await turn({ paths: podB.paths, cwd: placedB.dir, sessionId })
      dirsB.done(placedB)
      expect(JSON.stringify(toolResults())).toContain('carry me across')
    }, 90_000)
  })
})
