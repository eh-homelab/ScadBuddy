import { randomUUID } from 'node:crypto'
import { existsSync, readFileSync } from 'node:fs'
import { mkdir, mkdtemp, readdir, stat } from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'
import { Client } from '@modelcontextprotocol/sdk/client/index.js'
import { StdioClientTransport } from '@modelcontextprotocol/sdk/client/stdio.js'
import { afterAll, afterEach, beforeAll, describe, expect, it } from 'vitest'
import {
  AGENT_ACTOR_HEADER,
  BROWSER_TOOL_TIERS,
  DISALLOWED_BROWSER_TOOLS,
  type HeadlessBrowserOptions,
  materializeHeadlessBrowser,
  PAGE_MAX_BYTES,
  SERVER_NAME,
  SPOOL_OVER_BYTES,
} from '../src/harness/headlessBrowser.js'
import { type DownloadTarget, drainDownloads, writeDownloadBudget } from '../src/harness/browserDownloads.js'
import type { DownloadBudget } from '../src/harness/headlessBrowser.js'
import { BIG_PAGE, DOWNLOAD_BODY, endlessSent, type Hit, type PageServer, startOtherOrigin, startUi, testChromium } from './support/browserPages.js'

// The pinned @playwright/mcp server itself, started exactly as the per-session
// plugin's `.mcp.json` starts it, driven over stdio by an MCP client. No model
// and no Claude Code: this measures what the SERVER does with the config
// headlessBrowser.ts writes, i.e. the spec §3.2 rows for §5.3 that are about
// the server (origin allow-list vs. page JavaScript and redirects, the marker
// header, isolation, output files, the environment). The harness side is
// headlessBrowser.e2e.test.ts.

const chromium = testChromium()
const LEAK = 'sk-must-not-leak-0000'
const clients: Client[] = []

type ToolResult = { content?: { type: string; text?: string }[]; isError?: boolean }

/**
 * Starts the server as a session's `.mcp.json` says, the way Claude Code does:
 * with this process's environment (plus a credential that must not leak) and
 * the session's cwd, which is separate from the browser directory as in
 * production (stateDirs.ts `sessionBrowserDir`).
 */
async function connect(
  backendUrl: string,
  sessionId = randomUUID(),
  extra: Partial<Pick<HeadlessBrowserOptions, 'publicUrl' | 'browserAllowedOrigins' | 'approvedOrigins'>> = {},
) {
  const root = await mkdtemp(path.join(os.tmpdir(), 'pw-'))
  const cwd = path.join(root, 'cwd')
  await mkdir(cwd)
  const plugin = materializeHeadlessBrowser({
    sessionId,
    backendUrl,
    dir: path.join(root, 'browser'),
    ...extra,
    ...chromium,
  })
  const mcp = JSON.parse(readFileSync(path.join(plugin.pluginDir, '.mcp.json'), 'utf8')) as {
    mcpServers: Record<string, { command: string; args: string[] }>
  }
  const server = mcp.mcpServers[SERVER_NAME]!
  const transport = new StdioClientTransport({
    command: server.command,
    args: server.args,
    env: { ...(process.env as Record<string, string>), ANTHROPIC_AUTH_TOKEN: LEAK },
    cwd,
    stderr: 'pipe',
  })
  const client = new Client({ name: 'scadbuddy-test', version: '0' })
  await client.connect(transport)
  clients.push(client)
  const call = async (name: string, args: Record<string, unknown> = {}): Promise<{ text: string; isError: boolean }> => {
    const result = (await client.callTool({ name, arguments: args })) as ToolResult
    return {
      text: (result.content ?? []).map((c) => c.text ?? `[${c.type}]`).join('\n'),
      isError: result.isError === true,
    }
  }
  return { client, call, plugin, sessionId, cwd, root, pid: transport.pid }
}

const refOf = (snapshot: string, role: string, name: string): string => {
  const match = new RegExp(`${role} "${name}"[^\\n]*\\[ref=((?:f\\d+)?e\\d+)\\]`).exec(snapshot)
  if (!match?.[1]) throw new Error(`no ${role} "${name}" in:\n${snapshot}`)
  return match[1]
}

const marker = (h: Hit | undefined) => h?.headers[AGENT_ACTOR_HEADER.toLowerCase()]

describe.skipIf(!chromium)(`@playwright/mcp as configured for a session${chromium ? '' : ' (skipped: no Chromium)'}`, () => {
  let ui: PageServer
  let other: PageServer
  let third: PageServer

  beforeAll(async () => {
    other = await startOtherOrigin()
    third = await startOtherOrigin()
    ui = await startUi(other.origin)
  })
  afterEach(async () => {
    await Promise.all(clients.splice(0).map((c) => c.close()))
    ui.hits.length = 0
    other.hits.length = 0
    third.hits.length = 0
  })
  afterAll(async () => {
    await ui.close()
    await other.close()
    await third.close()
  })

  it('offers the core tools, including the four the harness must disallow', async () => {
    const { client } = await connect(ui.origin)
    const names = (await client.listTools()).tools.map((t) => t.name).sort()
    // The server offers them; disallowedTools in the harness is what removes them.
    for (const name of ['browser_run_code_unsafe', 'browser_evaluate', 'browser_file_upload', 'browser_drop']) {
      expect(names).toContain(name)
    }
    // Every other tool is in the tier map: nothing new slipped in with the pin.
    const disallowed: readonly string[] = DISALLOWED_BROWSER_TOOLS
    expect(names.filter((n) => !disallowed.includes(n))).toEqual(Object.keys(BROWSER_TOOL_TIERS).sort())
  }, 60_000)

  it.skipIf(process.platform !== 'linux')('runs without the environment it was started with', async () => {
    const { call, pid, plugin } = await connect(ui.origin)
    await call('browser_navigate', { url: `${ui.origin}/` })
    const environ = readFileSync(`/proc/${pid}/environ`, 'utf8').split('\0').filter(Boolean)
    expect(environ.join('\n')).not.toContain(LEAK)
    expect(environ.map((e) => e.split('=')[0]).sort()).toEqual(
      ['HOME', 'TMPDIR', ...(process.env.PLAYWRIGHT_BROWSERS_PATH ? ['PLAYWRIGHT_BROWSERS_PATH'] : [])].sort(),
    )
    expect(environ).toContain(`HOME=${path.join(path.dirname(plugin.pluginDir), 'home')}`)
  }, 60_000)

  it('sends the marker on every request to the backend, and a page fetch cannot replace it', async () => {
    const { call, sessionId } = await connect(ui.origin)
    expect((await call('browser_navigate', { url: `${ui.origin}/` })).isError).toBe(false)
    const snap = (await call('browser_snapshot')).text
    await call('browser_click', { element: 'Print', target: refOf(snap, 'button', 'Print') })
    await call('browser_wait_for', { text: 'print 403' })
    expect(ui.hits.length).toBeGreaterThan(1)
    for (const hit of ui.hits) expect(marker(hit)).toBe(sessionId)
    // The page's fetch set the header to "forged" itself; the context's value wins.
    expect(marker(ui.hits.find((h) => h.url === '/api/v1/prints'))).toBe(sessionId)
  }, 60_000)

  it('keeps a site\'s cookies across requests now that the guard makes them itself', async () => {
    const { call } = await connect(ui.origin)
    expect((await call('browser_navigate', { url: `${ui.origin}/cookie-set` })).isError).toBe(false)
    expect((await call('browser_navigate', { url: `${ui.origin}/` })).isError).toBe(false)
    expect(ui.hits.filter((h) => h.url === '/').at(-1)?.headers.cookie).toBe('sb_a=1; sb_b=2')
  }, 60_000)

  it('blocks page JavaScript from reaching another origin, by fetch or WebSocket', async () => {
    const { call } = await connect(ui.origin)
    await call('browser_navigate', { url: `${ui.origin}/` })
    const snap = (await call('browser_snapshot')).text
    await call('browser_click', { element: 'Probe', target: refOf(snap, 'button', 'Probe') })
    await call('browser_wait_for', { text: 'probe blocked' })
    await call('browser_click', { element: 'Socket', target: '#socket' })
    await call('browser_wait_for', { text: 'socket closed' })
    expect(other.hits).toEqual([])
    // Closed by the request guard before any connection was tried: Chromium
    // logs no failed connection (compare the approved case below).
    expect((await call('browser_console_messages')).text).not.toContain('WebSocket connection to')
  }, 60_000)

  // SCADBUDDY_BROWSER_ALLOWED_ORIGINS (browserOrigins.ts): what the request
  // guard lets through once the harness has recorded an origin as approved.
  // The approval itself is the harness's (headlessBrowser.test.ts, the session
  // e2e test); here the plugin's approve() stands in for it, mid-session, as
  // run.ts calls it.

  it('reaches an approved origin without the marker, and still marks the backend', async () => {
    const { call, plugin, sessionId } = await connect(ui.origin, randomUUID(), { browserAllowedOrigins: '*' })
    // Not approved yet: the guard refuses it even though the variable allows it.
    expect((await call('browser_navigate', { url: `${other.origin}/before` })).text).toContain('Blocked')
    expect(other.hits).toEqual([])
    plugin.approve(other.origin)
    const opened = await call('browser_navigate', { url: `${other.origin}/page` })
    expect(opened.text).toContain('Page Title: Other origin')
    // The backend's page fetching the approved origin: through, unmarked.
    await call('browser_navigate', { url: `${ui.origin}/` })
    await call('browser_click', { element: 'Probe', target: '#probe' })
    await call('browser_wait_for', { text: 'probe reached' })
    expect(other.hits.map((h) => h.url)).toEqual(['/page', '/probe'])
    for (const hit of other.hits) expect(marker(hit)).toBeUndefined()
    expect(ui.hits.length).toBeGreaterThan(0)
    for (const hit of ui.hits) expect(marker(hit)).toBe(sessionId)
    // A WebSocket to it is handed to Chromium now. Measured: Chromium then
    // refuses it itself (net::ERR_BLOCKED_BY_LOCAL_NETWORK_ACCESS_CHECKS), since
    // a page the guard fulfilled has no address space of its own and this
    // target is loopback; what matters here is that the guard let it try.
    await call('browser_click', { element: 'Socket', target: '#socket' })
    await call('browser_wait_for', { text: 'socket closed' })
    expect((await call('browser_console_messages')).text).toContain(
      `WebSocket connection to '${other.origin.replace(/^http/, 'ws')}/socket' failed`,
    )
  }, 60_000)

  it('follows a redirect to an approved origin, and refuses one from it to an unapproved origin', async () => {
    const { call } = await connect(ui.origin, randomUUID(), {
      browserAllowedOrigins: `${other.origin}, ${third.origin}`,
      approvedOrigins: [other.origin],
    })
    // The backend redirecting to the approved origin lands there.
    const away = await call('browser_navigate', { url: `${ui.origin}/redirect-away` })
    await call('browser_wait_for', { time: 1 })
    expect(away.isError).toBe(false)
    expect(other.hits.map((h) => [h.url, marker(h)])).toEqual([['/', undefined]])
    // The approved origin redirecting to one that is allowed but not approved: refused.
    const onward = await call('browser_navigate', { url: `${other.origin}/redirect?to=${encodeURIComponent(`${third.origin}/`)}` })
    expect(onward.text).toContain('Blocked')
    await call('browser_wait_for', { time: 1 })
    expect(third.hits).toEqual([])
  }, 60_000)

  it("opens ScadBuddy's public origin as it is, with the marker, and no longer the backend's (#983)", async () => {
    // The same server under another origin: the backend is 127.0.0.1, the public URL localhost.
    const publicOrigin = ui.origin.replace('127.0.0.1', 'localhost')
    const { call, sessionId } = await connect(ui.origin, randomUUID(), { publicUrl: publicOrigin })
    const opened = await call('browser_navigate', { url: `${publicOrigin}/m/box?via=public` })
    await call('browser_wait_for', { time: 1 })
    expect(opened.isError).toBe(false)
    const hit = ui.hits.find((h) => h.url === '/m/box?via=public')
    expect(hit?.headers.host).toBe(new URL(publicOrigin).host)
    expect(marker(hit!)).toBe(sessionId)
    // The page's own request to the public origin carries the marker too.
    const snapshot = await call('browser_snapshot')
    await call('browser_click', { element: 'Print', target: refOf(snapshot.text, 'button', 'Print') })
    await call('browser_wait_for', { text: 'print 403' })
    expect(ui.hits.filter((h) => h.url === '/api/v1/prints').map((h) => [h.headers.host, marker(h)])).toEqual([
      [new URL(publicOrigin).host, sessionId],
    ])
    // Nothing reaches a path the ingress routes to the agent (review of #1934): a page's
    // POST that would answer the session's own approval, a socket, a redirect, a
    // direct navigation, nor an encoded or dotted spelling of one.
    await call('browser_click', { element: 'Approve', target: '#approve' })
    await call('browser_wait_for', { text: 'approve blocked' })
    await call('browser_click', { element: 'Agent socket', target: '#agent-socket' })
    await call('browser_wait_for', { text: 'agent socket closed' })
    for (const url of ['/redirect-agent', '/api/v1/ai/status', '/api/v1/%61i/status', '/m/%2e%2e/api/v1/ai/status', '/mcp']) {
      const refused = await call('browser_navigate', { url: `${publicOrigin}${url}` })
      if (url !== '/redirect-agent') expect(refused.text, url).toContain('Blocked')
    }
    await call('browser_wait_for', { time: 1 })
    const agentHits = ui.hits.filter((h) => /^\/(api\/v1\/(ai|%61i)|mcp|m\/%2e)/i.test(h.url))
    expect(agentHits).toEqual([])
    // The backend's own origin is not a place any more.
    const before = ui.hits.length
    const backend = await call('browser_navigate', { url: `${ui.origin}/m/box?via=backend` })
    expect(backend.text).toContain('Blocked')
    expect(ui.hits.length).toBe(before)
  }, 60_000)

  it('refuses a direct navigation off the origin, and a redirect off it, but follows one on it', async () => {
    const { call, sessionId } = await connect(ui.origin)
    // A blocked navigation gets the guard's 403 page, not an abort (an abort
    // leaves the tab on chrome-error:// and breaks later navigations).
    const direct = await call('browser_navigate', { url: `${other.origin}/` })
    expect(direct.text).toContain('Blocked')
    expect(other.hits).toEqual([])
    // Without the redirect guard (headlessBrowser.ts redirectGuardSource) this
    // reached the other origin, marker and all: the allow-list "does not
    // affect redirects". With it, the 3xx is fetched with maxRedirects 0 and
    // aborted because its Location is off the origin.
    const redirect = await call('browser_navigate', { url: `${ui.origin}/redirect-away` })
    expect(redirect.text).toContain('Blocked')
    expect(other.hits).toEqual([])
    // A redirect on the origin still works, and still carries the marker.
    ui.hits.length = 0
    const home = await call('browser_navigate', { url: `${ui.origin}/redirect-home` })
    expect(home.isError).toBe(false)
    expect(home.text).toContain(`${ui.origin}/?from=redirect`)
    expect(ui.hits.map((h) => [h.url, marker(h)])).toEqual([
      ['/redirect-home', sessionId],
      ['/?from=redirect', sessionId],
    ])
    // A chain that stays on the origin for one hop and then leaves it.
    const chain = await call('browser_navigate', { url: `${ui.origin}/redirect-chain` })
    await call('browser_wait_for', { time: 1 })
    expect(chain.text).not.toContain('other origin reached')
    expect(other.hits).toEqual([])
  }, 60_000)

  it('aborts a redirect answered to page JavaScript', async () => {
    const { call } = await connect(ui.origin)
    await call('browser_navigate', { url: `${ui.origin}/` })
    await call('browser_click', { element: 'Redirected fetch', target: '#redirected-fetch' })
    await call('browser_wait_for', { text: 'fetch failed' })
    expect(ui.hits.filter((h) => h.url === '/?from=redirect')).toEqual([])
  }, 60_000)

  it('keeps two sessions apart: storage does not carry over', async () => {
    const a = await connect(ui.origin)
    const b = await connect(ui.origin)
    await a.call('browser_navigate', { url: `${ui.origin}/` })
    expect((await a.call('browser_navigate', { url: `${ui.origin}/` })).text).toContain('seen=xx')
    const fresh = (await b.call('browser_navigate', { url: `${ui.origin}/` })).text
    expect(fresh).toContain('seen=x')
    expect(fresh).not.toContain('seen=xx')
  }, 60_000)

  describe('downloads (#2329)', () => {
    const CAP = 1000
    const budget = (over: Partial<DownloadBudget> = {}): DownloadBudget => ({
      call: 1,
      fileMaxBytes: CAP,
      callMaxBytes: 4000,
      callMaxCount: 5,
      turnMaxBytes: 8000,
      turnMaxCount: 20,
      ...over,
    })
    function store() {
      const written: { path: string; bytes: string; source: unknown }[] = []
      const target: DownloadTarget = {
        ref: { kind: 'session', sessionId: randomUUID() },
        files: {
          limits: async () => ({ fileMaxBytes: CAP, workspaceMaxBytes: 8000 }),
          usage: async () => ({ files: 0, bytes: 0 }),
          write: async (_ws, p, bytes, options) => {
            written.push({ path: p, bytes: Buffer.from(bytes).toString(), source: options?.source })
            return { path: p, mediaType: 'model/stl', size: bytes.byteLength, sha256: '0'.repeat(64), source: null, createdAt: new Date(), updatedAt: new Date() }
          },
        },
      }
      return { written, target }
    }
    async function clickOn(call: (name: string, args?: Record<string, unknown>) => Promise<{ text: string; isError: boolean }>, role: string, name: string) {
      await call('browser_navigate', { url: `${ui.origin}/` })
      const snap = (await call('browser_snapshot')).text
      return call('browser_click', { element: name, target: refOf(snap, role, name) })
    }
    /** Every file under `dir`, with its size, so a test sees nothing over the cap stayed on the disk. */
    async function sizes(dir: string): Promise<number[]> {
      // Not the server's own snapshots and console logs, which grow with the test page.
      const names = (await readdir(dir).catch(() => [] as string[])).filter((n) => !/^(page|console)-.*\.(yml|log)$/.test(n))
      return Promise.all(names.map(async (n) => (await stat(path.join(dir, n))).size))
    }

    it('saves one for the harness, which moves it into the workspace without its query', async () => {
      const { call, plugin } = await connect(ui.origin)
      writeDownloadBudget(plugin.budgetFile, budget())
      expect((await clickOn(call, 'link', 'Download part')).isError).toBe(false)
      const { written, target } = store()
      const drained = await drainDownloads(plugin.downloadsDir, target, { waitMs: 15_000 })
      expect(drained.refused).toEqual([])
      expect(written).toEqual([{ path: 'downloads/part.stl', bytes: DOWNLOAD_BODY, source: { tool: 'browser', url: `${ui.origin}/files/part.stl` } }])
      expect(await readdir(plugin.downloadsDir)).toEqual([])
    }, 60_000)

    it("starts a turn's totals again when the budget names a new turn, as a durable session's browser outlives one (#2402)", async () => {
      const { call, plugin } = await connect(ui.origin)
      const { written, target } = store()
      const click = async (over: Partial<DownloadBudget>) => {
        writeDownloadBudget(plugin.budgetFile, budget({ turnMaxCount: 1, ...over }))
        await clickOn(call, 'link', 'Download part')
        return drainDownloads(plugin.downloadsDir, target, { waitMs: 15_000 })
      }
      expect((await click({ call: 1, turn: 'turn-a' })).saved).toHaveLength(1)
      expect((await click({ call: 2, turn: 'turn-a' })).refused).toEqual([{ name: 'part.stl', reason: expect.stringMatching(/more than 1 downloads in one turn/) }])
      expect((await click({ call: 1, turn: 'turn-b' })).saved).toHaveLength(1)
      expect(written).toHaveLength(2)
    }, 90_000)

    it('downloads nothing before the harness writes a budget', async () => {
      const { call, plugin } = await connect(ui.origin)
      const clicked = await clickOn(call, 'link', 'Download part')
      expect(clicked.text).toMatch(/HTTP status: 413/)
      const { written, target } = store()
      const drained = await drainDownloads(plugin.downloadsDir, target, { waitMs: 5_000 })
      expect(written).toEqual([])
      expect(drained.refused).toHaveLength(1)
    }, 60_000)

    it.each([
      // The route reads the body, finds it over the cap, and answers 413: no download starts.
      ['Download stream', 'a body past the cap with no Content-Length', [{ name: 'stream.bin', reason: expect.stringMatching(/over the 1000-byte download limit/) }]],
      // Content-Length beside Transfer-Encoding does not parse (Node's HTTP client), so the route answers 502.
      ['Download lying', 'a Content-Length under the cap on a body past it', []],
    ])('refuses %s (%s) before a byte reaches the disk', async (link, _what, refused) => {
      const { call, plugin } = await connect(ui.origin)
      writeDownloadBudget(plugin.budgetFile, budget())
      const clicked = await clickOn(call, 'link', link)
      expect(clicked.text).toMatch(/HTTP status: (413|502)/)
      const { written, target } = store()
      const drained = await drainDownloads(plugin.downloadsDir, target, { waitMs: 5_000 })
      expect(written).toEqual([])
      expect(drained).toEqual({ saved: [], refused, running: 0 })
      expect(await sizes(plugin.downloadsDir)).toEqual([])
      expect((await sizes(plugin.outputDir)).filter((s) => s > CAP)).toEqual([])
    }, 60_000)

    it('stops reading an endless download at the cap and answers 413', async () => {
      const { call, plugin } = await connect(ui.origin)
      writeDownloadBudget(plugin.budgetFile, budget())
      const started = performance.now()
      const clicked = await clickOn(call, 'link', 'Download endless')
      expect(clicked.text).toMatch(/HTTP status: 413/)
      const { written, target } = store()
      const drained = await drainDownloads(plugin.downloadsDir, target, { waitMs: 5_000 })
      expect(written).toEqual([])
      expect(drained.refused).toEqual([{ name: 'endless.bin', reason: expect.stringMatching(/over the 1000-byte download limit/) }])
      // The guard read past the cap by one chunk and closed the connection: what
      // the server got out is the cap plus the sockets' buffers, not the body.
      await waitForClose('/files/endless.bin')
      expect(endlessSent.get('/files/endless.bin')).toBeLessThan(16 * 1024 * 1024)
      expect(performance.now() - started).toBeLessThan(20_000)
    }, 60_000)

    it(`stops reading an endless page at ${PAGE_MAX_BYTES} bytes and says why, as a 502`, async () => {
      const { call } = await connect(ui.origin)
      const started = performance.now()
      const opened = await call('browser_navigate', { url: `${ui.origin}/endless.html` })
      expect(opened.text).toMatch(/HTTP status: 502/)
      expect((await call('browser_snapshot')).text).toContain(`is larger than ${PAGE_MAX_BYTES} bytes, the most the headless browser reads`)
      await waitForClose('/endless.html')
      const sent = endlessSent.get('/endless.html')!
      expect(sent).toBeGreaterThan(PAGE_MAX_BYTES)
      expect(sent).toBeLessThan(PAGE_MAX_BYTES + 16 * 1024 * 1024)
      expect(performance.now() - started).toBeLessThan(30_000)
    }, 60_000)

    it('hands on a page past the spool threshold whole, and leaves no spool file', async () => {
      expect(BIG_PAGE).toBeGreaterThan(SPOOL_OVER_BYTES)
      const { call, plugin } = await connect(ui.origin)
      const opened = await call('browser_navigate', { url: `${ui.origin}/big.html` })
      expect(opened.isError).toBe(false)
      expect(opened.text).toContain('Big page')
      // The comment that pads it ends the page: all of it reached Chromium.
      const whole = await call('browser_evaluate', { function: `() => document.documentElement.outerHTML.length > ${BIG_PAGE - 1024}` })
      expect(whole.text).toMatch(/\btrue\b/)
      expect(await readdir(plugin.spoolDir)).toEqual([])
    }, 60_000)

    it('cancels and deletes one made by page script that passes the cap on the disk', async () => {
      const { call, plugin } = await connect(ui.origin)
      writeDownloadBudget(plugin.budgetFile, budget())
      await clickOn(call, 'button', 'Blob big')
      const { written, target } = store()
      const drained = await drainDownloads(plugin.downloadsDir, target, { waitMs: 15_000 })
      expect(written).toEqual([])
      expect(drained.refused).toEqual([{ name: 'big.bin', reason: `stopped by the download limits: over the ${CAP}-byte workspace file limit` }])
      expect(await readdir(plugin.downloadsDir)).toEqual([])
      await new Promise((r) => setTimeout(r, 1500))
      expect((await sizes(plugin.outputDir)).filter((s) => s > CAP)).toEqual([])
    }, 60_000)

    it('lets a call make only so many', async () => {
      const { call, plugin } = await connect(ui.origin)
      writeDownloadBudget(plugin.budgetFile, budget({ callMaxCount: 5 }))
      await clickOn(call, 'button', 'Blob many')
      const { written, target } = store()
      const drained = await drainDownloads(plugin.downloadsDir, target, { waitMs: 15_000 })
      expect(written).toHaveLength(5)
      expect(drained.refused).toHaveLength(2)
      for (const r of drained.refused) expect(r.reason).toMatch(/more than 5 downloads in one browser call/)
    }, 60_000)
  })

  it('writes unnamed output to the output dir, named output to the cwd, and nothing outside', async () => {
    const { call, plugin, cwd, root } = await connect(ui.origin)
    await call('browser_navigate', { url: `${ui.origin}/` })
    expect((await call('browser_take_screenshot', {})).isError).toBe(false)
    expect((await call('browser_take_screenshot', { filename: 'preview.png' })).isError).toBe(false)
    const escape = await call('browser_take_screenshot', { filename: '../escape.png' })
    const absolute = await call('browser_take_screenshot', { filename: path.join(root, 'abs.png') })
    // The server's own restriction ("a convenience defense … not a secure
    // boundary"); the harness refuses such names before this (headlessBrowser.ts).
    expect(escape.isError).toBe(true)
    expect(escape.text).toContain('outside allowed roots')
    expect(absolute.isError).toBe(true)
    expect(existsSync(path.join(root, 'escape.png'))).toBe(false)
    expect(existsSync(path.join(root, 'abs.png'))).toBe(false)
    expect((await readdir(plugin.outputDir)).some((f) => f.endsWith('.png'))).toBe(true)
    expect(await readdir(cwd)).toEqual(['preview.png'])
  }, 60_000)
})

/** Waits for an endless response's count to stop moving (its connection closed), up to 10 s. */
async function waitForClose(url: string): Promise<void> {
  let last = -1
  for (let i = 0; i < 40; i++) {
    const now = endlessSent.get(url) ?? 0
    if (now === last && now > 0) return
    last = now
    await new Promise((r) => setTimeout(r, 250))
  }
}
