import { mkdir, mkdtemp, writeFile } from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'
import type { Sql } from 'postgres'
import { afterAll, beforeAll, describe, expect, it } from 'vitest'
import { durableEvalSkip, type DurableEvalStack, startDurableStack } from '../evals/durable.js'
import { DURABLE_EVAL_TOOL_PREFIX, type Scenario } from '../evals/runner.js'
import { hashTree } from '../src/plugins/packages/hash.js'
import type { LoadedPackage, PackagesForRun } from '../src/plugins/packages/install.js'
import { turnServers } from '../src/temporal/turnServers.js'
import { SessionFiles } from '../src/workspace/store.js'
import { type FakeAnthropic, type RecordedRequest, type Reply, startFakeAnthropic } from './support/fakeAnthropic.js'

// #2393 end to end: a plugin package approved to load as it is names Write and Read; a
// durable session gets them as the `builtin` turn tools, told so in its system prompt,
// and each call parks for approval, then acts on the session's workspace. The real
// agent-durable worker and its bundled Claude Code, on the fake Anthropic endpoint.

const skip = durableEvalSkip()
const TOKEN = 'gw-durable-builtins-token-5555666677778888'
const WRITE = `${DURABLE_EVAL_TOOL_PREFIX}builtin__Write`
const READ = `${DURABLE_EVAL_TOOL_PREFIX}builtin__Read`

type Block = { type: string; content?: unknown }
function toolResults(r: RecordedRequest): string[] {
  const out: string[] = []
  for (const m of r.body?.messages ?? []) {
    if (!Array.isArray(m.content)) continue
    for (const b of m.content as Block[]) {
      if (b.type === 'tool_result') out.push(typeof b.content === 'string' ? b.content : JSON.stringify(b.content))
    }
  }
  return out
}

async function allowedPackage(): Promise<LoadedPackage> {
  const dir = await mkdtemp(path.join(os.tmpdir(), 'builtins-pkg-'))
  await mkdir(path.join(dir, '.claude-plugin'))
  await writeFile(path.join(dir, '.claude-plugin', 'plugin.json'), JSON.stringify({ name: 'notes' }))
  await mkdir(path.join(dir, 'skills', 'jot'), { recursive: true })
  await writeFile(path.join(dir, 'skills', 'jot', 'SKILL.md'), '---\nname: jot\ndescription: jots\nallowed-tools: Read, Write\n---\nJot it down.\n')
  return { name: 'notes', contentHash: (await hashTree(dir)).hash, dir, allowed: true, builtinTools: ['Read', 'Write'] }
}

describe.skipIf(skip !== undefined)(`an allowed package's built-ins in a durable session${skip ? ` (skipped: ${skip})` : ''}`, () => {
  let fake: FakeAnthropic
  let stack: DurableEvalStack
  let sql: Sql
  let steps: Reply[] = []
  const decided: string[] = []

  beforeAll(async () => {
    fake = await startFakeAnthropic((r) => {
      if (!r.body?.tools?.some((t) => t.name.startsWith(DURABLE_EVAL_TOOL_PREFIX))) return { text: 'ok' }
      return steps[toolResults(r).length] ?? { text: 'Done.' }
    })
    const pkg = await allowedPackage()
    const root = await mkdtemp(path.join(os.tmpdir(), 'builtins-share-'))
    const load = async (): Promise<PackagesForRun> => ({ paths: [], allowedPaths: [pkg.dir], builtinTools: pkg.builtinTools, loaded: [pkg], problems: [], release: () => {} })
    stack = await startDurableStack({
      credential: { kind: 'gateway', baseUrl: fake.url, secret: TOKEN },
      decide: (tool) => {
        decided.push(tool)
        return 'approve'
      },
      turnServers: (deps) => {
        sql = deps.sql
        return turnServers({
          packages: { load, root, localRoot: path.join(deps.stateDir, 'plugins-local'), forward: async () => ({ plugins: [], problems: [], secrets: [], release: () => {} }) },
          builtins: { files: new SessionFiles(deps.sql), nested: () => ({ ask: async () => '', search: async () => ({ text: '' }) }) },
        })
      },
    })
  }, 300_000)
  afterAll(async () => {
    await stack?.close()
    await fake?.close()
  }, 60_000)

  it('offers Write and Read as turn tools, parks each call, and writes the workspace', async () => {
    steps = [
      { toolUse: { name: WRITE, input: { file_path: '/workspace/notes/a.txt', content: 'jotted\n' } } },
      { toolUse: { name: READ, input: { file_path: '/workspace/notes/a.txt' } } },
      { text: 'Jotted.' },
    ]
    const scenario: Scenario = { id: 'durable-builtins', title: 'durable-builtins', prepare: () => ({ prompt: 'jot it down', script: [] }), checks: [] }
    const outcome = await stack.run(scenario, { timeoutMs: 180_000 })
    expect(outcome.error).toBeUndefined()
    expect(outcome.approvals.map((a) => a.toolName)).toEqual([WRITE, READ])
    expect(decided).toEqual(['builtin__Write', 'builtin__Read'])
    const [file] = await sql<{ data: Buffer }[]>`
      SELECT data FROM ai_session_files WHERE workspace = ${`session:${outcome.sessionId!}`} AND path = 'notes/a.txt'`
    expect(file?.data.toString('utf8')).toBe('jotted\n')
    const last = fake.messageCalls().at(-1)!
    expect(toolResults(last)).toEqual(['File created successfully at: /workspace/notes/a.txt', '1\tjotted\n2\t'])
    // Told which built-ins it has this way.
    expect(JSON.stringify(last.body?.system)).toContain('Read (mcp__durable__builtin__Read), Write (mcp__durable__builtin__Write)')
    expect(last.body?.tools?.map((t) => t.name)).toEqual(expect.arrayContaining([WRITE, READ]))
  }, 240_000)
})
