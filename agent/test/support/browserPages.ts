import { existsSync, readdirSync, readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { createServer, type IncomingHttpHeaders, type Server, type ServerResponse } from 'node:http'
import type { AddressInfo } from 'node:net'
import os from 'node:os'
import path from 'node:path'

// Local pages for the headless-browser tests (#349). `ui` stands in for
// ScadBuddy's SPA on the allowed origin: a customizer-like form whose "Render"
// updates a preview, a "Print" button that POSTs an outward route, and a probe
// that fetches another origin from page JavaScript. `other` is a second origin
// the browser must not reach. Every request each server receives is recorded
// with its headers, which is how the tests see the agent-actor marker and
// whether page JavaScript reached another origin. A WebSocket upgrade is
// recorded too (method `UPGRADE`) and then dropped.

export type Hit = { method: string; url: string; headers: IncomingHttpHeaders }

export type PageServer = { origin: string; hits: Hit[]; close(): Promise<void> }

type Handler = (url: string, method: string, res: ServerResponse, headers: IncomingHttpHeaders) => void | Promise<void>

async function serve(handler: Handler): Promise<PageServer> {
  const hits: Hit[] = []
  const server: Server = createServer((req, res) => {
    req.resume()
    req.on('end', () => {
      hits.push({ method: req.method ?? 'GET', url: req.url ?? '/', headers: req.headers })
      void Promise.resolve(handler(req.url ?? '/', req.method ?? 'GET', res, req.headers)).catch(() => {
        res.writeHead(500)
        res.end()
      })
    })
  })
  server.on('upgrade', (req, socket) => {
    hits.push({ method: 'UPGRADE', url: req.url ?? '/', headers: req.headers })
    socket.destroy()
  })
  await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve))
  const { port } = server.address() as AddressInfo
  return {
    origin: `http://127.0.0.1:${port}`,
    hits,
    close: () =>
      new Promise<void>((resolve) => {
        server.closeAllConnections()
        server.close(() => resolve())
      }),
  }
}

/** Another origin: a page for any path, and `/redirect?to=<url>` answers a 302 there. */
export async function startOtherOrigin(): Promise<PageServer> {
  return serve((url, _method, res) => {
    const to = new URL(url, 'http://x').searchParams.get('to')
    if (url.startsWith('/redirect?') && to) {
      res.writeHead(302, { location: to })
      res.end()
      return
    }
    res.writeHead(200, { 'content-type': 'text/html', 'access-control-allow-origin': '*' })
    res.end('<!doctype html><title>Other origin</title><h1>other origin reached</h1>')
  })
}

/**
 * Answers the stand-in's outward route (`POST /api/v1/prints`) with a status.
 * By default 403, what backend/scadbuddy/api/agent_actor.py answers a marked
 * outward request with no grant.
 */
export type OutwardGate = (headers: IncomingHttpHeaders) => Promise<number>

/** The stand-in UI; `otherOrigin` is what its probe and redirect point at. */
export async function startUi(otherOrigin: string, gate: OutwardGate = () => Promise.resolve(403)): Promise<PageServer> {
  const page = `<!doctype html>
<html><head><title>Customizer</title></head>
<body>
  <h1>Box</h1>
  <label>Width <input id="width" name="width" value="20"></label>
  <button id="render" onclick="document.getElementById('preview').textContent = 'Preview: width ' + document.getElementById('width').value">Render</button>
  <p id="preview">Preview: width 20</p>
  <button id="print" onclick="fetch('/api/v1/prints', {method: 'POST', headers: {'X-ScadBuddy-Agent-Session': 'forged'}}).then(r => document.getElementById('status').textContent = 'print ' + r.status)">Print</button>
  <p id="status">idle</p>
  <button id="probe" onclick="fetch('${otherOrigin}/probe').then(r => r.text()).then(() => document.getElementById('probe-result').textContent = 'probe reached', () => document.getElementById('probe-result').textContent = 'probe blocked')">Probe</button>
  <p id="probe-result">probe not run</p>
  <button id="redirected-fetch" onclick="fetch('/redirect-home').then(() => document.getElementById('fetch-result').textContent = 'fetch followed', () => document.getElementById('fetch-result').textContent = 'fetch failed')">Redirected fetch</button>
  <p id="fetch-result">fetch not run</p>
  <button id="socket" onclick="const ws = new WebSocket('${otherOrigin.replace(/^http/, 'ws')}/socket'); ws.onopen = () => document.getElementById('socket-result').textContent = 'socket open'; ws.onerror = ws.onclose = () => document.getElementById('socket-result').textContent = 'socket closed'">Socket</button>
  <p id="socket-result">socket not run</p>
  <script>localStorage.setItem('seen', (localStorage.getItem('seen') || '') + 'x'); document.title = 'Customizer seen=' + localStorage.getItem('seen')</script>
</body></html>`
  return serve(async (url, method, res, headers) => {
    if (url === '/redirect-chain') {
      // On the origin first, then off it: a browser that follows the first hop
      // on its own would follow the second too.
      res.writeHead(302, { location: '/redirect-away' })
      res.end()
      return
    }
    if (url === '/redirect-home') {
      res.writeHead(302, { location: '/?from=redirect' })
      res.end()
      return
    }
    if (url === '/redirect-away') {
      res.writeHead(302, { location: `${otherOrigin}/` })
      res.end()
      return
    }
    if (url === '/api/v1/prints' && method === 'POST') {
      const status = await gate(headers)
      res.writeHead(status, { 'content-type': 'application/json' })
      res.end(status === 200 ? '{"ok":true}' : '{"detail":"refused"}')
      return
    }
    res.writeHead(200, { 'content-type': 'text/html' })
    res.end(page)
  })
}

/** How the tests launch Chromium: nothing to override, or an explicit executable. */
export type TestChromium = { executablePath?: string }

/**
 * A Chromium the tests can launch, or undefined to skip them:
 *
 * - SCADBUDDY_TEST_CHROMIUM, an explicit executable, when set;
 * - otherwise the chromium-headless-shell build the pinned `playwright-core`
 *   expects, under PLAYWRIGHT_BROWSERS_PATH or Playwright's default cache: then
 *   nothing is overridden and the launch resolves exactly as in the image
 *   (`install-browser --only-shell chromium`, as ci.yml's agent job runs);
 * - otherwise a `chromium` link at the top of PLAYWRIGHT_BROWSERS_PATH, as a
 *   dev container with another Chromium revision pre-installed has.
 */
export function testChromium(): TestChromium | undefined {
  const fromEnv = process.env.SCADBUDDY_TEST_CHROMIUM
  if (fromEnv) return existsSync(fromEnv) ? { executablePath: fromEnv } : undefined
  const root = process.env.PLAYWRIGHT_BROWSERS_PATH || path.join(os.homedir(), '.cache', 'ms-playwright')
  if (pinnedHeadlessShell(root)) return {}
  const link = path.join(root, 'chromium')
  return existsSync(link) ? { executablePath: link } : undefined
}

/** Whether `root` holds the headless shell at the revision playwright-core pins. */
function pinnedHeadlessShell(root: string): boolean {
  try {
    // playwright-core is @playwright/mcp's dependency, so resolve it from there.
    const fromMcp = createRequire(createRequire(import.meta.url).resolve('@playwright/mcp/package.json'))
    const core = path.dirname(fromMcp.resolve('playwright-core/package.json'))
    const browsers = JSON.parse(readFileSync(path.join(core, 'browsers.json'), 'utf8')) as {
      browsers: { name: string; revision: string }[]
    }
    const revision = browsers.browsers.find((b) => b.name === 'chromium-headless-shell')?.revision
    const dir = path.join(root, `chromium_headless_shell-${revision}`)
    return revision !== undefined && readdirSync(dir).some((d) => existsSync(path.join(dir, d, 'headless_shell')))
  } catch {
    return false
  }
}
