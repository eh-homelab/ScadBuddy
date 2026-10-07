import type * as Sdk from '@anthropic-ai/claude-agent-sdk'
import type { Options, SessionStore } from '@anthropic-ai/claude-agent-sdk'
import { readdirSync, readFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { describe, expect, it, vi } from 'vitest'
import type { HarnessRun } from '../src/harness/run.js'

// Since SDK 0.3.286, an omitted `permissionMode` is left to Claude Code, which
// applies a settings `defaultMode` and, on a third-party provider or with
// telemetry off, starts in auto mode "like `claude -p`" (its changelog). Tool
// calls would then run without reaching `canUseTool`, the seam every approval
// tier hangs on (#258). So every query passes `permissionMode: 'default'`
// (harness/run.ts), and this file fails when one does not (#1540).

const seen = vi.hoisted(() => ({ options: [] as Options[] }))

vi.mock('@anthropic-ai/claude-agent-sdk', async (importOriginal) => {
  const actual = await importOriginal<typeof Sdk>()
  // A query that ends at once: only the options it was given matter here.
  const query = ({ options }: { options: Options }) => {
    seen.options.push(options)
    const inner = (async function* () {})()
    return Object.assign(inner, { interrupt: () => Promise.resolve() })
  }
  return { ...actual, query }
})

const { runHarness } = await import('../src/harness/run.js')
const { OWN_PLUGIN_DIR } = await import('../src/harness/ownPlugin.js')

const SDK = '@anthropic-ai/claude-agent-sdk'
const agentRoot = fileURLToPath(new URL('..', import.meta.url))

function sources(dir: string): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const full = path.join(dir, entry.name)
    if (entry.isDirectory()) return sources(full)
    return entry.name.endsWith('.ts') && !entry.name.endsWith('.d.ts') ? [full] : []
  })
}

/** Whether a file can call the SDK's `query()`: a value import of it, a namespace import, or a dynamic import. */
function reachesQuery(text: string): boolean {
  for (const [, names = ''] of text.matchAll(/import\s*\{([^}]*)\}\s*from\s*'@anthropic-ai\/claude-agent-sdk'/g)) {
    const values = names
      .split(',')
      .map((name) => name.trim())
      .filter((name) => name && !name.startsWith('type '))
    if (values.some((name) => name === 'query' || name.startsWith('query as '))) return true
  }
  return (
    /import\s+\*\s+as\s+\w+\s+from\s*'@anthropic-ai\/claude-agent-sdk'/.test(text) ||
    text.includes(`import('${SDK}')`)
  )
}

describe("every query() is the harness's, with permissionMode 'default'", () => {
  it('only harness/run.ts calls the SDK’s query()', () => {
    const callers = [...sources(path.join(agentRoot, 'src')), ...sources(path.join(agentRoot, 'evals'))]
      .filter((file) => reachesQuery(readFileSync(file, 'utf8')))
      .map((file) => path.relative(agentRoot, file))
    expect(callers).toEqual([path.join('src', 'harness', 'run.ts')])
  })

  it('passes it on every kind of run, whatever else the run adds to the options', async () => {
    const store: SessionStore = { append: () => Promise.resolve(), load: () => Promise.resolve(null) }
    const base: HarnessRun = {
      paths: { stateDir: '/var/lib/scadbuddy-agent' },
      credential: { kind: 'anthropic_api_key', secret: 'sk-ant-api03-unit-test-key-000011112222' },
      prompt: 'hi',
    }
    const runs: HarnessRun[] = [
      base,
      { ...base, credential: { kind: 'gateway', baseUrl: 'https://llm.example', secret: 'gw-token' } },
      { ...base, credential: { kind: 'claude_oauth_token', secret: 'sk-ant-oat01-test-token-0000' } },
      {
        ...base,
        ownPlugin: OWN_PLUGIN_DIR,
        questionGate: () => Promise.resolve({ answered: false, message: 'nobody replied' }),
        resume: '00000000-0000-4000-8000-000000000000',
        sessionStore: store,
        includePartialMessages: true,
        systemPromptAppend: 'route: /customize',
        stderr: () => {},
      },
    ]
    seen.options.length = 0
    for (const run of runs) {
      for await (const _ of runHarness(run)) {
        // drain
      }
    }
    expect(seen.options).toHaveLength(runs.length)
    for (const options of seen.options) expect(options.permissionMode).toBe('default')
  })
})
