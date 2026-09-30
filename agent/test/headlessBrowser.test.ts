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
  browserInputGuard,
  browserTierOf,
  DISALLOWED_BROWSER_TOOLS,
  disallowedBrowserTools,
  materializeHeadlessBrowser,
  originToApprove,
  PLAYWRIGHT_MCP_VERSION,
  playwrightMcpCli,
  redirectGuardSource,
  TOOL_PREFIX,
  VENDORED_PLUGIN_DIR,
} from '../src/harness/headlessBrowser.js'
import {
  type BrowserOrigins,
  browserOrigins,
  BrowserOriginsError,
  parseBrowserAllowedOrigins,
} from '../src/harness/browserOrigins.js'
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

describe('the navigation guard and file names', () => {
  const only = browserOrigins({ backendUrl: ORIGIN })
  const none = new Set<string>()
  const check = (name: string, input: unknown, origins: BrowserOrigins = only, approved: ReadonlySet<string> = none) =>
    browserInputGuard(t(name), input, origins, approved)

  it('allows the backend origin, with any path, and default ports normalised', () => {
    expect(check('browser_navigate', { url: `${ORIGIN}/models/box?x=1` })).toBeUndefined()
    expect(check('browser_navigate', { url: 'https://ui.example:443/' }, browserOrigins({ backendUrl: 'https://ui.example' }))).toBeUndefined()
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
  ])('refuses %s when nothing else is allowed (the default)', (_label, url) => {
    expect(check('browser_navigate', { url })).toEqual({ deny: expect.stringMatching(/may only open ScadBuddy's own UI/) })
  })

  it('applies the same check to a new tab, and lets tab listing through', () => {
    expect(check('browser_tabs', { action: 'new', url: 'http://evil.example/' })).toEqual({ deny: expect.stringMatching(/own UI/) })
    expect(check('browser_tabs', { action: 'list' })).toBeUndefined()
  })

  it.each(['../escape.png', '/tmp/x.png', 'sub/dir.png', '.mcp.json', '..', 'a\\b.png', ''])('refuses the file name %j', (filename) => {
    expect(check('browser_take_screenshot', { filename })).toEqual({ deny: expect.stringMatching(/plain file name/) })
  })

  it('accepts a plain file name, and leaves other servers’ tools alone', () => {
    expect(check('browser_take_screenshot', { filename: 'preview.png' })).toBeUndefined()
    expect(browserInputGuard('mcp__scadbuddy__render_model', { url: 'http://evil.example/' }, only, none)).toBeUndefined()
  })

  it('denies through the permission seam whatever the tier', () => {
    const guard = (name: string, input: unknown) => browserInputGuard(name, input, only, none)
    expect(decide(t('browser_navigate'), browserTierOf, { url: 'http://evil.example/' }, guard)).toMatchObject({
      decision: 'deny',
      tier: 'read',
    })
    expect(decide(t('browser_navigate'), browserTierOf, { url: `${ORIGIN}/` }, guard)).toEqual({ decision: 'allow', tier: 'read' })
  })

  describe('aliases of the backend (SCADBUDDY_PUBLIC_URL, SCADBUDDY_ALLOWED_ORIGINS)', () => {
    const origins = browserOrigins({
      backendUrl: ORIGIN,
      publicUrl: 'https://scadbuddy.internal.example/',
      uiOrigins: 'https://scadbuddy.lan:8443, http://127.0.0.1:8000',
    })

    it('are the UI origins other than the backend', () => {
      expect(origins).toEqual({
        backend: ORIGIN,
        aliases: ['https://scadbuddy.internal.example', 'https://scadbuddy.lan:8443'],
        allowed: [],
      })
    })

    it('rewrites a URL on one to the same path, query and fragment on the backend', () => {
      const url = 'https://scadbuddy.internal.example/m/builtin%3Aspinning-top-pip?tab=params#preview'
      expect(check('browser_navigate', { url }, origins)).toEqual({
        input: { url: `${ORIGIN}/m/builtin%3Aspinning-top-pip?tab=params#preview` },
      })
      expect(check('browser_tabs', { action: 'new', url: 'https://scadbuddy.lan:8443/' }, origins)).toEqual({
        input: { action: 'new', url: `${ORIGIN}/` },
      })
      // Allowed at the tool's tier, with the new input.
      const guard = (name: string, input: unknown) => browserInputGuard(name, input, origins, none)
      expect(decide(t('browser_navigate'), browserTierOf, { url }, guard)).toEqual({
        decision: 'allow',
        tier: 'read',
        input: { url: `${ORIGIN}/m/builtin%3Aspinning-top-pip?tab=params#preview` },
      })
    })

    it('still refuses the alias host on another scheme or port', () => {
      expect(check('browser_navigate', { url: 'http://scadbuddy.internal.example/' }, origins)).toHaveProperty('deny')
      expect(check('browser_navigate', { url: 'https://scadbuddy.lan/' }, origins)).toHaveProperty('deny')
    })
  })

  describe('off-origin navigation (SCADBUDDY_BROWSER_ALLOWED_ORIGINS)', () => {
    const listed = browserOrigins({ backendUrl: ORIGIN, browserAllowed: 'https://docs.example, http://printer.lan:8080/' })
    const any = browserOrigins({ backendUrl: ORIGIN, browserAllowed: '*' })

    it('makes a listed origin outward until it is approved, then lets it run at the tool’s tier', () => {
      const url = 'https://docs.example/openscad/manual'
      expect(check('browser_navigate', { url }, listed)).toEqual({ outward: expect.stringMatching(/https:\/\/docs\.example/) })
      expect(originToApprove(t('browser_navigate'), { url }, listed, none)).toBe('https://docs.example')
      const approved = new Set(['https://docs.example'])
      expect(check('browser_navigate', { url }, listed, approved)).toBeUndefined()
      expect(originToApprove(t('browser_navigate'), { url }, listed, approved)).toBeUndefined()
      const guard = (name: string, input: unknown) => browserInputGuard(name, input, listed, none)
      expect(decide(t('browser_navigate'), browserTierOf, { url }, guard)).toMatchObject({ decision: 'needs_approval', tier: 'outward' })
    })

    it('still refuses an origin the list does not name, saying what it does allow', () => {
      expect(check('browser_navigate', { url: 'https://evil.example/' }, listed)).toEqual({
        deny: expect.stringMatching(/with a human's approval, https:\/\/docs\.example, http:\/\/printer\.lan:8080; "https:\/\/evil\.example\/" is none of them/),
      })
      // Approval of something not on the list does not count.
      expect(check('browser_navigate', { url: 'https://evil.example/' }, listed, new Set(['https://evil.example']))).toHaveProperty('deny')
    })

    it('under `*` allows any http(s) origin, each still behind its approval, and nothing else', () => {
      expect(check('browser_navigate', { url: 'http://192.168.1.20:1234/v1/models' }, any)).toHaveProperty('outward')
      expect(check('browser_tabs', { action: 'new', url: 'https://example.com/' }, any)).toHaveProperty('outward')
      for (const url of ['file:///etc/passwd', 'javascript:alert(1)', 'data:text/html,x', 'chrome://settings']) {
        expect(check('browser_navigate', { url }, any)).toHaveProperty('deny')
      }
    })

    it('never treats the backend or an alias as off-origin', () => {
      const both = browserOrigins({ backendUrl: ORIGIN, publicUrl: 'https://ui.example', browserAllowed: `${ORIGIN}, https://ui.example` })
      expect(both.allowed).toEqual([])
      expect(check('browser_navigate', { url: 'https://ui.example/x' }, both)).toEqual({ input: { url: `${ORIGIN}/x` } })
    })

    it.each([
      ['a path', 'https://docs.example/manual'],
      ['not an origin', 'docs.example'],
      ['`*` with others', '*, https://docs.example'],
      ['a file URL', 'file:///tmp'],
    ])('refuses a variable with %s', (_label, raw) => {
      expect(() => parseBrowserAllowedOrigins(raw)).toThrow(BrowserOriginsError)
    })
  })
})

describe('the per-session plugin', () => {
  it('writes the server config spec §5.3 asks for', async () => {
    const { configFile, outputDir, allowedOrigin } = await materialized()
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
        contextOptions: { extraHTTPHeaders?: Record<string, string> }
      }
    }
    const config = JSON.parse(readFileSync(configFile, 'utf8')) as Config
    expect(allowedOrigin).toBe(ORIGIN)
    expect(config.network).toEqual({ allowedOrigins: [ORIGIN] })
    expect(config.browser.isolated).toBe(true)
    expect(config.browser.userDataDir).toBeUndefined()
    expect(config.browser.launchOptions.headless).toBe(true)
    // The marker goes to the backend only, added by the request guard: the
    // context's extraHTTPHeaders would reach every origin.
    expect(config.browser.contextOptions.extraHTTPHeaders).toBeUndefined()
    expect(config.outputDir).toBe(outputDir)
    expect(config.allowUnrestrictedFileAccess).toBe(false)
    expect(config.webmcp).toBe(false)
    expect(config.capabilities).toBeUndefined()
  })

  it('loads the request guard on every page, with the backend, the session and its approved origins', async () => {
    const { configFile, sessionId, dir } = await materialized()
    const config = JSON.parse(readFileSync(configFile, 'utf8')) as { browser: { initPage?: string[] } }
    expect(config.browser.initPage).toHaveLength(1)
    const guard = readFileSync(config.browser.initPage![0]!, 'utf8')
    expect(guard).toContain(`const BACKEND = ${JSON.stringify(ORIGIN)}`)
    expect(guard).toContain(`const MARKER = ${JSON.stringify(AGENT_ACTOR_HEADER.toLowerCase())}`)
    expect(guard).toContain('maxRedirects: 0')
    const approvedFile = path.join(dir, 'approved-origins.json')
    expect(guard).toBe(redirectGuardSource({ backend: ORIGIN, aliases: [], sessionId, approvedFile }))
    expect(JSON.parse(readFileSync(approvedFile, 'utf8'))).toEqual([])
  })

  it('widens the server allow-list to the aliases and the listed origins, and drops it under `*`', async () => {
    const dir = await mkdtemp(path.join(os.tmpdir(), 'hb-'))
    const network = (file: string) => (JSON.parse(readFileSync(file, 'utf8')) as { network?: unknown }).network
    const listed = materializeHeadlessBrowser({
      sessionId: randomUUID(),
      backendUrl: ORIGIN,
      publicUrl: 'https://ui.example',
      browserAllowedOrigins: 'https://docs.example',
      dir: path.join(dir, 'a'),
    })
    expect(network(listed.configFile)).toEqual({ allowedOrigins: [ORIGIN, 'https://ui.example', 'https://docs.example'] })
    // "Default is to allow all" (config.d.ts): the hook and the request guard are the gate.
    const any = materializeHeadlessBrowser({ sessionId: randomUUID(), backendUrl: ORIGIN, browserAllowedOrigins: '*', dir: path.join(dir, 'b') })
    expect(network(any.configFile)).toBeUndefined()
  })

  it('writes the approved origins the variable still allows, and each new approval, for the request guard', async () => {
    const dir = await mkdtemp(path.join(os.tmpdir(), 'hb-'))
    const plugin = materializeHeadlessBrowser({
      sessionId: randomUUID(),
      backendUrl: ORIGIN,
      browserAllowedOrigins: 'https://docs.example, https://wiki.example',
      // Approved in an earlier turn, and since dropped from the list: not any more.
      approvedOrigins: ['https://docs.example', 'https://old.example'],
      dir,
    })
    const file = path.join(dir, 'approved-origins.json')
    expect([...plugin.approved]).toEqual(['https://docs.example'])
    expect(JSON.parse(readFileSync(file, 'utf8'))).toEqual(['https://docs.example'])
    plugin.approve('https://wiki.example')
    expect(JSON.parse(readFileSync(file, 'utf8'))).toEqual(['https://docs.example', 'https://wiki.example'])
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

  it('hands the tool an alias URL rewritten onto the backend', async () => {
    const stateDir = await mkdtemp(path.join(os.tmpdir(), 'hb-'))
    const options = buildHarnessOptions({
      paths: { stateDir },
      credential,
      prompt: 'x',
      headlessBrowser: {
        sessionId: randomUUID(),
        backendUrl: ORIGIN,
        publicUrl: 'https://scadbuddy.internal.example',
        dir: path.join(stateDir, 'b'),
      },
    })
    const ctx = { signal: new AbortController().signal, toolUseID: 'tu', suggestions: [] } as never
    await expect(
      options.canUseTool!(t('browser_navigate'), { url: 'https://scadbuddy.internal.example/m/builtin%3Abox?x=1' }, ctx),
    ).resolves.toEqual({ behavior: 'allow', updatedInput: { url: `${ORIGIN}/m/builtin%3Abox?x=1` } })
  })

  it('parks the first navigation to an allowed origin, then remembers the origin for the session', async () => {
    const stateDir = await mkdtemp(path.join(os.tmpdir(), 'hb-'))
    const dir = path.join(stateDir, 'b')
    const asked: string[] = []
    const remembered: [string, string | undefined][] = []
    const options = buildHarnessOptions({
      paths: { stateDir },
      credential,
      prompt: 'x',
      approvalGate: (request) => {
        asked.push(JSON.stringify(request.input))
        return Promise.resolve({ approved: true, input: request.input, approvalId: `a${asked.length}`, decision: 'approved' })
      },
      headlessBrowser: {
        sessionId: randomUUID(),
        backendUrl: ORIGIN,
        browserAllowedOrigins: 'https://docs.example',
        rememberOrigin: (origin, approvalId) => {
          remembered.push([origin, approvalId])
          return Promise.resolve()
        },
        dir,
      },
    })
    const ctx = { signal: new AbortController().signal, toolUseID: 'tu', suggestions: [] } as never
    const hook = options.hooks!.PreToolUse![0]!.hooks[0]!
    const pre = (url: string) =>
      hook(
        { hook_event_name: 'PreToolUse', tool_name: t('browser_navigate'), tool_input: { url } } as never,
        'tu',
        { signal: new AbortController().signal },
      )
    // The hook forces the prompt, so no allow rule can skip the gate.
    await expect(pre('https://docs.example/a')).resolves.toMatchObject({ hookSpecificOutput: { permissionDecision: 'ask' } })
    await expect(options.canUseTool!(t('browser_navigate'), { url: 'https://docs.example/a' }, ctx)).resolves.toEqual({
      behavior: 'allow',
      updatedInput: { url: 'https://docs.example/a' },
    })
    expect(remembered).toEqual([['https://docs.example', 'a1']])
    expect(JSON.parse(readFileSync(path.join(dir, 'approved-origins.json'), 'utf8'))).toEqual(['https://docs.example'])
    // The same origin again: no hook verdict, no approval.
    await expect(pre('https://docs.example/b')).resolves.toEqual({})
    await expect(options.canUseTool!(t('browser_navigate'), { url: 'https://docs.example/b' }, ctx)).resolves.toMatchObject({
      behavior: 'allow',
    })
    expect(asked).toHaveLength(1)
  })

  it('denies the navigation when the origin cannot be recorded, and without a gate', async () => {
    const stateDir = await mkdtemp(path.join(os.tmpdir(), 'hb-'))
    const ctx = { signal: new AbortController().signal, toolUseID: 'tu', suggestions: [] } as never
    const failing = buildHarnessOptions({
      paths: { stateDir },
      credential,
      prompt: 'x',
      approvalGate: (request) => Promise.resolve({ approved: true, input: request.input, approvalId: 'a1' }),
      headlessBrowser: {
        sessionId: randomUUID(),
        backendUrl: ORIGIN,
        browserAllowedOrigins: '*',
        rememberOrigin: () => Promise.reject(new Error('database down')),
        dir: path.join(stateDir, 'a'),
      },
    })
    await expect(failing.canUseTool!(t('browser_navigate'), { url: 'https://docs.example/' }, ctx)).resolves.toMatchObject({
      behavior: 'deny',
      message: expect.stringMatching(/database down/),
    })
    const ungated = buildHarnessOptions({
      paths: { stateDir },
      credential,
      prompt: 'x',
      headlessBrowser: { sessionId: randomUUID(), backendUrl: ORIGIN, browserAllowedOrigins: '*', dir: path.join(stateDir, 'b') },
    })
    await expect(ungated.canUseTool!(t('browser_navigate'), { url: 'https://docs.example/' }, ctx)).resolves.toMatchObject({
      behavior: 'deny',
      message: expect.stringMatching(/no approval surface/),
    })
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
