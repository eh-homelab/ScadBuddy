import { randomUUID } from 'node:crypto'
import { readFileSync, writeFileSync } from 'node:fs'
import { mkdtemp } from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'
import { describe, expect, it } from 'vitest'
import {
  AGENT_ACTOR_HEADER,
  assertHeadlessPlugin,
  AUTHORIZE_TOOL_NAME,
  BROWSER_TOOL_TIERS,
  browserInputProblem,
  browserTierOf,
  DISALLOWED_BROWSER_TOOLS,
  disallowedBrowserTools,
  materializeHeadlessBrowser,
  PLAYWRIGHT_MCP_VERSION,
  playwrightMcpCli,
  redirectGuardSource,
  TOOL_PREFIX,
  VENDORED_PLUGIN_DIR,
} from '../src/harness/headlessBrowser.js'
import { probeChromiumSandbox } from '../src/harness/headlessSandbox.js'
import { decide } from '../src/harness/permissions.js'
import { buildHarnessOptions } from '../src/harness/run.js'

// The headless browser's configuration and guards (#349, spec §5.3), without a
// browser. The real server and Chromium are headlessBrowser.server.test.ts and
// headlessBrowser.e2e.test.ts.

const ORIGIN = 'http://127.0.0.1:8000'
const t = (name: string) => `${TOOL_PREFIX}${name}`

async function materialized() {
  const dir = await mkdtemp(path.join(os.tmpdir(), 'hb-'))
  const sessionId = randomUUID()
  return { sessionId, dir, ...materializeHeadlessBrowser({ sessionId, backendUrl: `${ORIGIN}/`, dir }) }
}

describe('pinning', () => {
  it('runs the exact @playwright/mcp version agent/package.json pins', () => {
    const pkg = JSON.parse(readFileSync(new URL('../package.json', import.meta.url), 'utf8')) as {
      dependencies: Record<string, string>
    }
    expect(pkg.dependencies['@playwright/mcp']).toBe(PLAYWRIGHT_MCP_VERSION)
    expect(playwrightMcpCli()).toMatch(/@playwright[/\\]mcp[/\\]cli\.js$/)
  })

  it('vendors the official plugin manifest byte for byte, and not its `.mcp.json`', () => {
    const manifest = readFileSync(path.join(VENDORED_PLUGIN_DIR, '.claude-plugin', 'plugin.json'), 'utf8')
    expect(JSON.parse(manifest)).toMatchObject({ name: 'playwright', author: { name: 'Microsoft' } })
    expect(() => readFileSync(path.join(VENDORED_PLUGIN_DIR, '.mcp.json'))).toThrow()
  })
})

describe('tiers (spec §5.3)', () => {
  it('reads navigating, snapshots, screenshots, console, network and waiting', () => {
    for (const name of [
      'browser_navigate',
      'browser_navigate_back',
      'browser_snapshot',
      'browser_take_screenshot',
      'browser_console_messages',
      'browser_network_requests',
      'browser_wait_for',
    ]) {
      expect(browserTierOf(t(name))).toBe('read')
    }
  })

  it('writes clicking, typing, filling, selecting, keys and closing', () => {
    for (const name of ['browser_click', 'browser_type', 'browser_fill_form', 'browser_select_option', 'browser_press_key', 'browser_close']) {
      expect(browserTierOf(t(name))).toBe('write')
    }
  })

  it('never tiers a disallowed tool, an unknown one, or another server’s tool', () => {
    for (const name of DISALLOWED_BROWSER_TOOLS) expect(browserTierOf(t(name))).toBeUndefined()
    expect(browserTierOf(t('browser_something_new'))).toBeUndefined()
    expect(browserTierOf(t('__proto__'))).toBeUndefined()
    expect(browserTierOf('mcp__playwright__browser_click')).toBeUndefined()
    // Unknown means outward (spec §8.1): needs approval, i.e. not run.
    expect(decide(t('browser_something_new'), browserTierOf).decision).toBe('needs_approval')
  })

  it('makes the authorize tool outward: it always parks for a human approval', () => {
    expect(AUTHORIZE_TOOL_NAME).toBe('mcp__scadbuddy_browser__authorize_request')
    expect(browserTierOf(AUTHORIZE_TOOL_NAME)).toBe('outward')
    expect(decide(AUTHORIZE_TOOL_NAME, browserTierOf).decision).toBe('needs_approval')
  })

  it('has no tool both tiered and disallowed', () => {
    for (const name of DISALLOWED_BROWSER_TOOLS) expect(Object.keys(BROWSER_TOOL_TIERS)).not.toContain(name)
    expect(disallowedBrowserTools()).toEqual(DISALLOWED_BROWSER_TOOLS.map(t))
  })
})

describe('the navigation allow-list and file names', () => {
  it('allows the backend origin, with any path, and default ports normalised', () => {
    expect(browserInputProblem(t('browser_navigate'), { url: `${ORIGIN}/models/box?x=1` }, ORIGIN)).toBeUndefined()
    expect(browserInputProblem(t('browser_navigate'), { url: 'https://ui.example:443/' }, 'https://ui.example')).toBeUndefined()
  })

  it.each([
    ['another port', 'http://127.0.0.1:9000/'],
    ['another host', 'http://localhost:8000/'],
    ['another scheme', 'https://127.0.0.1:8000/'],
    ['a cloud metadata address', 'http://169.254.169.254/latest/meta-data/'],
    ['a file URL', 'file:///etc/passwd'],
    ['a javascript URL', 'javascript:alert(1)'],
    ['a data URL', 'data:text/html,<h1>x</h1>'],
    ['userinfo that looks like the origin', 'http://127.0.0.1:8000@evil.example/'],
    ['not a URL', 'nonsense'],
    ['not a string', 42],
  ])('refuses %s', (_label, url) => {
    expect(browserInputProblem(t('browser_navigate'), { url }, ORIGIN)).toMatch(/may only open ScadBuddy's own UI/)
  })

  it('applies the same check to a new tab, and lets tab listing through', () => {
    expect(browserInputProblem(t('browser_tabs'), { action: 'new', url: 'http://evil.example/' }, ORIGIN)).toMatch(/own UI/)
    expect(browserInputProblem(t('browser_tabs'), { action: 'list' }, ORIGIN)).toBeUndefined()
  })

  it.each(['../escape.png', '/tmp/x.png', 'sub/dir.png', '.mcp.json', '..', 'a\\b.png', ''])('refuses the file name %j', (filename) => {
    expect(browserInputProblem(t('browser_take_screenshot'), { filename }, ORIGIN)).toMatch(/plain file name/)
  })

  it('accepts a plain file name, and leaves other servers’ tools alone', () => {
    expect(browserInputProblem(t('browser_take_screenshot'), { filename: 'preview.png' }, ORIGIN)).toBeUndefined()
    expect(browserInputProblem('mcp__scadbuddy__render_model', { url: 'http://evil.example/' }, ORIGIN)).toBeUndefined()
  })

  it('denies through the permission seam whatever the tier', () => {
    const guard = (name: string, input: unknown) => browserInputProblem(name, input, ORIGIN)
    expect(decide(t('browser_navigate'), browserTierOf, { url: 'http://evil.example/' }, guard)).toMatchObject({
      decision: 'deny',
      tier: 'read',
    })
    expect(decide(t('browser_navigate'), browserTierOf, { url: `${ORIGIN}/` }, guard)).toEqual({ decision: 'allow', tier: 'read' })
  })
})

describe('the per-session plugin', () => {
  it('writes the server config spec §5.3 asks for', async () => {
    const { sessionId, configFile, outputDir, allowedOrigin } = await materialized()
    type Config = {
      network: unknown
      outputDir: string
      allowUnrestrictedFileAccess: boolean
      webmcp: boolean
      capabilities?: unknown
      browser: {
        isolated: boolean
        userDataDir?: string
        launchOptions: { headless: boolean }
        contextOptions: { extraHTTPHeaders: Record<string, string> }
      }
    }
    const config = JSON.parse(readFileSync(configFile, 'utf8')) as Config
    expect(allowedOrigin).toBe(ORIGIN)
    expect(config.network).toEqual({ allowedOrigins: [ORIGIN] })
    expect(config.browser.isolated).toBe(true)
    expect(config.browser.userDataDir).toBeUndefined()
    expect(config.browser.launchOptions.headless).toBe(true)
    expect(config.browser.contextOptions.extraHTTPHeaders).toEqual({ [AGENT_ACTOR_HEADER]: sessionId })
    expect(config.outputDir).toBe(outputDir)
    expect(config.allowUnrestrictedFileAccess).toBe(false)
    expect(config.webmcp).toBe(false)
    expect(config.capabilities).toBeUndefined()
  })

  it('loads the redirect guard on every page, for this origin only', async () => {
    const { configFile } = await materialized()
    const config = JSON.parse(readFileSync(configFile, 'utf8')) as { browser: { initPage?: string[] } }
    expect(config.browser.initPage).toHaveLength(1)
    const guard = readFileSync(config.browser.initPage![0]!, 'utf8')
    expect(guard).toContain(`const ALLOWED = ${JSON.stringify(ORIGIN)}`)
    expect(guard).toContain('maxRedirects: 0')
    expect(guard).toBe(redirectGuardSource(ORIGIN))
  })

  it('asks for the Chromium sandbox only when told it works', async () => {
    const dir = await mkdtemp(path.join(os.tmpdir(), 'hb-'))
    type Launch = { browser: { launchOptions: { chromiumSandbox?: boolean } } }
    const read = (file: string) => (JSON.parse(readFileSync(file, 'utf8')) as Launch).browser.launchOptions
    const off = materializeHeadlessBrowser({ sessionId: randomUUID(), backendUrl: ORIGIN, dir: path.join(dir, 'a') })
    expect(read(off.configFile).chromiumSandbox).toBeUndefined()
    const on = materializeHeadlessBrowser({ sessionId: randomUUID(), backendUrl: ORIGIN, dir: path.join(dir, 'b'), sandbox: true })
    expect(read(on.configFile).chromiumSandbox).toBe(true)
  })

  it('starts the pinned server under `env -i`, and passes its own check', async () => {
    const { pluginDir, configFile } = await materialized()
    const mcp = JSON.parse(readFileSync(path.join(pluginDir, '.mcp.json'), 'utf8')) as {
      mcpServers: Record<string, { command: string; args: string[] }>
    }
    expect(Object.keys(mcp.mcpServers)).toEqual(['playwright'])
    const { command, args } = mcp.mcpServers.playwright!
    expect(command).toBe('/usr/bin/env')
    expect(args[0]).toBe('-i')
    expect(args.filter((a) => /^[A-Z_]+=/.test(a)).map((a) => a.split('=')[0])).toEqual(
      expect.arrayContaining(['HOME', 'TMPDIR']),
    )
    expect(args.join(' ')).not.toMatch(/ANTHROPIC|CLAUDE_CONFIG_DIR|DATABASE_URL/)
    expect(args).toEqual(expect.arrayContaining([playwrightMcpCli(), '--config', configFile, '--headless', '--isolated', '--no-webmcp']))
    expect(() => assertHeadlessPlugin(pluginDir)).not.toThrow()
  })

  it.each([
    ['a command other than env -i', (s: Record<string, unknown>) => ({ ...s, command: 'npx', args: ['@playwright/mcp@latest'] })],
    ['unrestricted file access', (s: { args: string[] }) => ({ ...s, args: [...s.args, '--allow-unrestricted-file-access'] })],
    ['a persistent profile', (s: { args: string[] }) => ({ ...s, args: [...s.args, '--user-data-dir=/tmp/p'] })],
    ['extra capabilities', (s: { args: string[] }) => ({ ...s, args: [...s.args, '--caps', 'vision'] })],
    ['a missing --isolated', (s: { args: string[] }) => ({ ...s, args: s.args.filter((a) => a !== '--isolated') })],
  ])('refuses a plugin with %s', async (_label, mutate) => {
    const { pluginDir } = await materialized()
    const file = path.join(pluginDir, '.mcp.json')
    const mcp = JSON.parse(readFileSync(file, 'utf8')) as { mcpServers: { playwright: { args: string[] } } }
    mcp.mcpServers.playwright = mutate(mcp.mcpServers.playwright as never) as never
    writeFileSync(file, JSON.stringify(mcp))
    expect(() => assertHeadlessPlugin(pluginDir)).toThrow(/is refused/)
  })

  it('refuses a second server', async () => {
    const { pluginDir } = await materialized()
    const file = path.join(pluginDir, '.mcp.json')
    const mcp = JSON.parse(readFileSync(file, 'utf8')) as { mcpServers: Record<string, unknown> }
    mcp.mcpServers.extra = mcp.mcpServers.playwright
    writeFileSync(file, JSON.stringify(mcp))
    expect(() => assertHeadlessPlugin(pluginDir)).toThrow(/expected one server/)
  })

  it('refuses a session id that is not a UUID, and a backend URL that is not http(s)', async () => {
    const dir = await mkdtemp(path.join(os.tmpdir(), 'hb-'))
    expect(() => materializeHeadlessBrowser({ sessionId: '../x', backendUrl: ORIGIN, dir })).toThrow(/not a session id/)
    expect(() => materializeHeadlessBrowser({ sessionId: randomUUID(), backendUrl: 'file:///x', dir })).toThrow(/not an http/)
  })
})

describe('buildHarnessOptions with the headless browser', () => {
  const credential = { kind: 'anthropic_api_key' as const, secret: 'sk-ant-unit' }

  it('loads the per-session plugin by local path, removes the disallowed tools and tiers the rest', async () => {
    const stateDir = await mkdtemp(path.join(os.tmpdir(), 'hb-'))
    const sessionId = randomUUID()
    const dir = path.join(stateDir, 'browser', sessionId)
    const options = buildHarnessOptions({
      paths: { stateDir },
      credential,
      prompt: 'x',
      headlessBrowser: { sessionId, backendUrl: ORIGIN, dir },
    })
    expect(options.plugins).toEqual([{ type: 'local', path: path.join(dir, 'plugin') }])
    expect(options.disallowedTools).toEqual(disallowedBrowserTools())
    // Measured (headlessBrowser.e2e.test.ts): with strictMcpConfig a plugin's
    // MCP servers are not started at all.
    expect(options.strictMcpConfig).toBe(false)
    expect(options.tools).toEqual([])
    expect(options.settingSources).toEqual([])
  })

  it('keeps strictMcpConfig and adds no plugin without it', async () => {
    const stateDir = await mkdtemp(path.join(os.tmpdir(), 'hb-'))
    const options = buildHarnessOptions({ paths: { stateDir }, credential, prompt: 'x' })
    expect(options.strictMcpConfig).toBe(true)
    expect(options.plugins).toBeUndefined()
    expect(options.disallowedTools).toBeUndefined()
  })

  it('denies an off-origin navigation in canUseTool, and allows an on-origin one', async () => {
    const stateDir = await mkdtemp(path.join(os.tmpdir(), 'hb-'))
    const sessionId = randomUUID()
    const options = buildHarnessOptions({
      paths: { stateDir },
      credential,
      prompt: 'x',
      headlessBrowser: { sessionId, backendUrl: ORIGIN, dir: path.join(stateDir, 'b') },
    })
    const ctx = { signal: new AbortController().signal, toolUseID: 'tu', suggestions: [] } as never
    await expect(options.canUseTool!(t('browser_navigate'), { url: 'http://evil.example/' }, ctx)).resolves.toMatchObject({
      behavior: 'deny',
    })
    await expect(options.canUseTool!(t('browser_navigate'), { url: `${ORIGIN}/` }, ctx)).resolves.toMatchObject({
      behavior: 'allow',
    })
    await expect(options.canUseTool!(t('browser_click'), { target: 'e3' }, ctx)).resolves.toMatchObject({ behavior: 'allow' })
  })
})

describe('the sandbox probe', () => {
  const page = { setContent: () => Promise.resolve() }

  it('says available when Chromium starts with its sandbox', async () => {
    let asked: unknown
    const launcher = {
      launch: (options: unknown) => {
        asked = options
        return Promise.resolve({ newPage: () => Promise.resolve(page), close: () => Promise.resolve() })
      },
    }
    expect(await probeChromiumSandbox({ launcher })).toEqual({ available: true, detail: expect.any(String) })
    expect(asked).toMatchObject({ headless: true, chromiumSandbox: true })
  })

  it('says unavailable, with the sandbox line of the error, and never throws', async () => {
    const launcher = {
      launch: () =>
        Promise.reject(new Error('browserType.launch: closed\nBrowser logs:\nChromium sandboxing failed!\nmore')),
    }
    expect(await probeChromiumSandbox({ launcher })).toEqual({ available: false, detail: 'Chromium sandboxing failed!' })
  })
})
