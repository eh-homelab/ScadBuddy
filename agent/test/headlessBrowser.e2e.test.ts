import { randomUUID } from 'node:crypto'
import { existsSync, readdirSync, readFileSync, writeFileSync } from 'node:fs'
import { mkdtemp } from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'
import type { SDKMessage, SDKResultMessage } from '@anthropic-ai/claude-agent-sdk'
import { afterAll, beforeAll, describe, expect, it } from 'vitest'
import { bundledCliPath } from '../src/harness/cliVersion.js'
import { AGENT_ACTOR_HEADER, BROWSER_TOOL_TIERS, TOOL_PREFIX } from '../src/harness/headlessBrowser.js'
import type { ToolDecision } from '../src/harness/permissions.js'
import { runHarness } from '../src/harness/run.js'
import { ensureSessionDir, ensureStateDirs, sessionBrowserDir } from '../src/harness/stateDirs.js'
import { type FakeAnthropic, type RecordedRequest, type Reply, startFakeAnthropic } from './support/fakeAnthropic.js'
import { type PageServer, startOtherOrigin, startUi, testChromium } from './support/browserPages.js'

// The headless browser end to end (#349, spec §13 "Container e2e"): the real
// SDK and bundled Claude Code load the per-session copy of the playwright
// plugin by local path, start the pinned @playwright/mcp with a real Chromium,
// and a scripted "model" (the local fake Anthropic endpoint, so nothing reaches
// Anthropic) drives it: open the customizer, set a parameter, render,
// screenshot the preview, then try what must not work. Skips without a Chromium
// (test/support/browserPages.ts `testChromium`) or without the bundled binary.

const TOKEN = 'gw-headless-browser-token-777788889999'
const chromium = testChromium()
let cliMissing: string | undefined
try {
  bundledCliPath()
} catch (err) {
  cliMissing = (err as Error).message
}
const skip = !chromium || cliMissing !== undefined

type Block = { type: string; content?: unknown; tool_use_id?: string }

/** Every tool_result the conversation holds so far, as text. */
function toolResults(r: RecordedRequest): string[] {
  const out: string[] = []
  for (const m of r.body?.messages ?? []) {
    if (!Array.isArray(m.content)) continue
    for (const b of m.content as Block[]) {
      if (b.type !== 'tool_result') continue
      out.push(typeof b.content === 'string' ? b.content : JSON.stringify(b.content))
    }
  }
  return out
}

const ref = (text: string, role: string, name: string): string => {
  const match = new RegExp(`${role} \\\\?"${name}\\\\?"[^\\n]*?\\[ref=(e\\d+)\\]`).exec(text)
  if (!match?.[1]) throw new Error(`no ${role} "${name}" in ${text.slice(0, 2000)}`)
  return match[1]
}

/** The environment of every running @playwright/mcp server started from `configFile`. */
function serverEnvs(configFile: string): string[] {
  const envs: string[] = []
  for (const pid of readdirSync('/proc').filter((p) => /^\d+$/.test(p))) {
    try {
      const cmdline = readFileSync(`/proc/${pid}/cmdline`, 'utf8')
      if (cmdline.includes('@playwright') && cmdline.includes(configFile)) {
        envs.push(readFileSync(`/proc/${pid}/environ`, 'utf8'))
      }
    } catch {
      // gone, or not ours
    }
  }
  return envs
}

describe.skipIf(skip)(`the headless browser in the harness${skip ? ` (skipped: ${cliMissing ?? 'no Chromium'})` : ''}`, () => {
  let fake: FakeAnthropic
  let ui: PageServer
  let other: PageServer
  let stateDir: string
  const sessionId = randomUUID()
  const messages: SDKMessage[] = []
  const decisions: [string, ToolDecision['decision'], string][] = []
  const stderr: string[] = []
  let serverEnv: string[] = []
  let result: SDKResultMessage | undefined
  const scriptErrors: string[] = []

  beforeAll(async () => {
    other = await startOtherOrigin()
    ui = await startUi(other.origin)
    stateDir = await mkdtemp(path.join(os.tmpdir(), 'headless-'))
    await ensureStateDirs({ stateDir })
    const cwd = await ensureSessionDir({ stateDir }, sessionId)
    // With the plugin, strictMcpConfig is off (run.ts); a project `.mcp.json`
    // in the session's cwd must still not be loaded (settingSources: []).
    writeFileSync(
      path.join(cwd, '.mcp.json'),
      JSON.stringify({ mcpServers: { planted: { command: '/bin/sh', args: ['-c', 'touch planted-ran'] } } }),
    )
    const browserDir = sessionBrowserDir({ stateDir }, sessionId)
    const configFile = path.join(browserDir, 'playwright-mcp.json')

    // The scripted model: one step per tool result so far.
    const steps: ((results: string[]) => Reply)[] = [
      () => ({ toolUse: { name: `${TOOL_PREFIX}browser_navigate`, input: { url: `${ui.origin}/` } } }),
      () => ({ toolUse: { name: `${TOOL_PREFIX}browser_snapshot`, input: {} } }),
      (r) => {
        serverEnv = serverEnvs(configFile)
        const snapshot = r.at(-1) ?? ''
        return {
          toolUse: {
            name: `${TOOL_PREFIX}browser_type`,
            input: { element: 'Width', target: ref(snapshot, 'textbox', 'Width'), text: '42' },
          },
        }
      },
      (r) => ({
        toolUse: {
          name: `${TOOL_PREFIX}browser_click`,
          input: { element: 'Render', target: ref(r[1] ?? '', 'button', 'Render') },
        },
      }),
      () => ({ toolUse: { name: `${TOOL_PREFIX}browser_take_screenshot`, input: { filename: 'preview.png' } } }),
      () => ({ toolUse: { name: `${TOOL_PREFIX}browser_snapshot`, input: {} } }),
      // What must not work, in turn:
      () => ({ toolUse: { name: `${TOOL_PREFIX}browser_navigate`, input: { url: `${other.origin}/` } } }),
      () => ({ toolUse: { name: `${TOOL_PREFIX}browser_take_screenshot`, input: { filename: '../../escape.png' } } }),
      () => ({ toolUse: { name: `${TOOL_PREFIX}browser_evaluate`, input: { function: '() => document.cookie' } } }),
      () => ({ toolUse: { name: `${TOOL_PREFIX}browser_run_code_unsafe`, input: { code: 'process.exit(1)' } } }),
      () => ({ text: 'Done.' }),
    ]
    fake = await startFakeAnthropic((r) => {
      const results = toolResults(r)
      const step = steps[results.length]
      try {
        return step ? step(results) : { text: 'Done.' }
      } catch (err) {
        scriptErrors.push(String(err))
        return { text: 'Stopped: the script could not continue.' }
      }
    })

    try {
      for await (const m of runHarness({
        paths: { stateDir },
        credential: { kind: 'gateway', baseUrl: fake.url, secret: TOKEN },
        prompt: 'Set the width to 42 and show me the preview.',
        model: 'claude-sonnet-4-5',
        cwd,
        sessionId,
        maxTurns: 20,
        headlessBrowser: { sessionId, backendUrl: ui.origin, dir: browserDir, ...chromium },
        onDecision: (name, d) => decisions.push([name, d.decision, d.tier]),
        stderr: (l) => stderr.push(l),
      })) {
        messages.push(m)
      }
    } catch (err) {
      if (!messages.some((m) => m.type === 'result')) throw new Error(`${String(err)}\n${stderr.join('')}`, { cause: err })
    }
    result = messages.find((m): m is SDKResultMessage => m.type === 'result')
    if (scriptErrors.length) {
      console.error('script errors:', scriptErrors, '\ninit:', JSON.stringify(init()), '\nstderr:', stderr.join(''))
    }
  }, 180_000)

  afterAll(async () => {
    await fake?.close()
    await ui?.close()
    await other?.close()
  })

  const results = () => toolResults(fake.messageCalls().at(-1)!)
  const init = () => messages.find((m) => m.type === 'system' && m.subtype === 'init') as
    | (SDKMessage & { tools: string[]; plugins?: { name: string; path: string }[]; mcp_servers?: { name: string; status: string }[] })
    | undefined

  it('ran the whole script', () => {
    expect(scriptErrors).toEqual([])
  })

  it('loads the plugin from its per-session local path and names its tools with the plugin prefix', () => {
    const i = init()
    expect(i?.plugins?.map((p) => p.name)).toContain('playwright')
    // Exactly the plugin's server: the planted project `.mcp.json` was not loaded.
    expect(i?.mcp_servers).toEqual([{ name: 'plugin:playwright:playwright', status: 'connected', source: 'plugin' }])
    expect(existsSync(path.join(stateDir, 'work', 'sessions', sessionId, 'planted-ran'))).toBe(false)
    const browserTools = (i?.tools ?? []).filter((t) => t.startsWith(TOOL_PREFIX))
    expect(browserTools).toContain(`${TOOL_PREFIX}browser_navigate`)
    // Measured on Claude Code 2.1.283: exactly the 21 core tools the server
    // offers minus the four disallowed ones (test/headlessBrowser.server.test.ts).
    expect(browserTools.map((t) => t.slice(TOOL_PREFIX.length)).sort()).toEqual(Object.keys(BROWSER_TOOL_TIERS).sort())
  })

  it('never offers the disallowed tools to the model', () => {
    const offered = new Set(fake.messageCalls().flatMap((c) => (c.body?.tools ?? []).map((t) => t.name)))
    for (const name of ['browser_run_code_unsafe', 'browser_evaluate', 'browser_file_upload', 'browser_drop', 'browser_install']) {
      expect(offered.has(`${TOOL_PREFIX}${name}`)).toBe(false)
      expect(init()?.tools).not.toContain(`${TOOL_PREFIX}${name}`)
    }
    expect(offered.has(`${TOOL_PREFIX}browser_click`)).toBe(true)
  })

  it('opens the customizer, sets a parameter and screenshots the preview', () => {
    const r = results()
    expect(r[5]).toContain('Preview: width 42')
    expect(existsSync(path.join(stateDir, 'work', 'sessions', sessionId, 'preview.png'))).toBe(true)
  })

  it('records each call at its tier: navigating and screenshots are read, typing and clicking write', () => {
    const tierOf = (tool: string) => decisions.find(([name]) => name === `${TOOL_PREFIX}${tool}`)?.[2]
    expect(tierOf('browser_navigate')).toBe('read')
    expect(tierOf('browser_snapshot')).toBe('read')
    expect(tierOf('browser_take_screenshot')).toBe('read')
    expect(tierOf('browser_type')).toBe('write')
    expect(tierOf('browser_click')).toBe('write')
  })

  it('refuses a navigation off the allowed origin before the browser is asked', () => {
    expect(results()[6]).toMatch(/may only open ScadBuddy's own UI/)
    expect(other.hits).toEqual([])
    expect(decisions).toContainEqual([`${TOOL_PREFIX}browser_navigate`, 'deny', 'read'])
  })

  it('refuses a file name with a directory part', () => {
    expect(results()[7]).toMatch(/plain file name/)
    expect(existsSync(path.join(stateDir, 'work', 'escape.png'))).toBe(false)
  })

  it('cannot call a disallowed tool even when the model names it', () => {
    expect(results()[8]).toMatch(/No such tool available|not available/i)
    expect(results()[9]).toMatch(/No such tool available|not available/i)
  })

  it('marks every request the page made with the session', () => {
    const marked = ui.hits.map((h) => h.headers[AGENT_ACTOR_HEADER.toLowerCase()])
    expect(marked.length).toBeGreaterThan(0)
    expect(new Set(marked)).toEqual(new Set([sessionId]))
  })

  it('starts the server without the credential in its environment', () => {
    expect(serverEnv.length).toBeGreaterThan(0)
    for (const env of serverEnv) {
      expect(env).not.toContain(TOKEN)
      expect(env).not.toContain('ANTHROPIC_')
    }
    expect(result?.subtype).toBe('success')
  })
})
