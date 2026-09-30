import { expect, test, type FrameLocator, type Page } from '@playwright/test'

/**
 * #254 — the whole round trip, against a real agent service: an MCP client calls the
 * agent's browser_* tools over `/mcp`, the agent forwards each call over the tab's socket
 * (`/api/v1/ai/bridge`), the page runs it against its DOM through the bridge, and the
 * answer goes back the same way. The page is the msw-mocked bundle (so no backend is
 * needed for the catalogue and the customizer); only its bridge socket leaves the page,
 * relayed by Playwright to the agent with the agent's own origin as `Origin`, as the
 * ingress would deliver it.
 *
 * Environment:
 *
 *   E2E_AGENT_BRIDGE_URL  The agent service's own origin, e.g. `http://127.0.0.1:8081`:
 *                         `node dist/main.js` in agent/ with `SCADBUDDY_DATABASE_URL`
 *                         (pairings and MCP tokens are in Postgres; no credential or key
 *                         file is needed, since no model runs). Unset, this file skips.
 *
 * For example: `docker run -d -e POSTGRES_PASSWORD=postgres -p 55433:5432 postgres:17`,
 * then in agent/ `pnpm build && SCADBUDDY_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:55433/postgres
 * SCADBUDDY_BACKEND_URL=http://127.0.0.1:9 node dist/main.js`, and here
 * `E2E_AGENT_BRIDGE_URL=http://127.0.0.1:8081 pnpm exec playwright test agent-link.real`.
 */

const AGENT = process.env.E2E_AGENT_BRIDGE_URL

type McpResult = { isError?: boolean; content: { type: string; text: string }[] }

/** A minimal MCP Streamable HTTP client (https://modelcontextprotocol.io/specification/2025-06-18/basic/transports). */
async function mcpSession(base: string, token: string) {
  let session: string | undefined
  let id = 0
  const post = async (body: Record<string, unknown>) => {
    const res = await fetch(`${base}/mcp`, {
      method: 'POST',
      headers: {
        authorization: `Bearer ${token}`,
        'content-type': 'application/json',
        accept: 'application/json, text/event-stream',
        ...(session ? { 'mcp-session-id': session } : {}),
      },
      body: JSON.stringify({ jsonrpc: '2.0', ...body }),
    })
    session ??= res.headers.get('mcp-session-id') ?? undefined
    const text = await res.text()
    if (!('id' in body)) return undefined
    // An SSE answer carries the JSON-RPC response in its `data:` lines.
    const json = res.headers.get('content-type')?.includes('text/event-stream')
      ? text
          .split('\n')
          .filter((line) => line.startsWith('data:'))
          .map((line) => line.slice(5).trim())
          .map((data) => JSON.parse(data) as { id?: number; result?: unknown })
          .find((message) => message.id === body.id)
      : (JSON.parse(text) as { result?: unknown })
    return json?.result
  }
  await post({ id: ++id, method: 'initialize', params: { protocolVersion: '2025-06-18', capabilities: {}, clientInfo: { name: 'e2e', version: '0' } } })
  await post({ method: 'notifications/initialized' })
  return {
    async call(name: string, args: Record<string, unknown> = {}): Promise<{ isError: boolean; text: string; body: unknown }> {
      const result = (await post({ id: ++id, method: 'tools/call', params: { name, arguments: args } })) as McpResult
      const text = result.content[0]?.text ?? ''
      // Tool results arrive in the untrusted-data envelope (agent src/safety/untrusted.ts,
      // `{"untrusted_data": {"tool", "source", "content"}}`); ScadBuddy's own notices do not.
      let body: unknown = text
      try {
        const parsed = JSON.parse(text) as { untrusted_data?: { content?: unknown } }
        body = parsed.untrusted_data ? parsed.untrusted_data.content : parsed
      } catch {
        // Plain text: an error or a ScadBuddy notice.
      }
      return { isError: result.isError ?? false, text, body }
    },
  }
}

/** Relays the page's bridge socket to the agent, as the ingress would. */
async function relayBridge(page: Page, base: string) {
  await page.addInitScript(() => {
    ;(globalThis as { __scadbuddyTabLink?: boolean }).__scadbuddyTabLink = true
  })
  await page.routeWebSocket(/\/api\/v1\/ai\/bridge$/, (ws) => {
    // Node's WebSocket takes headers (undici's WebSocketInit); the agent checks Origin.
    const Upstream = WebSocket as unknown as new (url: string, init: { headers: Record<string, string> }) => WebSocket
    const upstream = new Upstream(`${base.replace(/^http/, 'ws')}/api/v1/ai/bridge`, { headers: { origin: base } })
    const early: string[] = []
    upstream.onopen = () => {
      for (const message of early.splice(0)) upstream.send(message)
    }
    upstream.onmessage = (event) => ws.send(String(event.data))
    upstream.onclose = () => ws.close()
    ws.onMessage((message) => {
      if (upstream.readyState === upstream.OPEN) upstream.send(String(message))
      else early.push(String(message))
    })
    ws.onClose(() => upstream.close())
  })
}

test.describe('agent link against the real agent (#254)', () => {
  test.skip(!AGENT, 'set E2E_AGENT_BRIDGE_URL to a running agent service')
  test.skip(!!process.env.E2E_BASE_URL, 'uses the msw-mocked bundle for the page')

  /** Mints an outward token (a Settings write: the agent's own origin, from loopback) and opens /mcp with it. */
  async function mcpClient(base: string, name: string) {
    const minted = await fetch(`${base}/api/v1/ai/mcp-tokens`, {
      method: 'POST',
      headers: { 'content-type': 'application/json', origin: base },
      body: JSON.stringify({ name, tier: 'outward' }),
    })
    expect(minted.status).toBe(201)
    const { token } = (await minted.json()) as { token: string }
    return mcpSession(base, token)
  }

  async function pairsAndDrives(app: Page | FrameLocator, name: string) {
    const mcp = await mcpClient(AGENT!, name)
    expect((await mcp.call('browser_status')).body).toMatchObject({ attached: false })

    const pair = (await mcp.call('browser_pair')).body as { code: string; shown_to_user_as: string }
    expect(pair.shown_to_user_as).toBe(`MCP token “${name}”`)
    const prompt = app.getByRole('region', { name: 'Agent pairing' })
    await expect(prompt).toContainText(`MCP token “${name}” asks to use this tab`, { timeout: 15_000 })
    // Every open tab lists every request; the user answers this one, in this tab.
    const request = prompt.getByRole('form', { name: `Pairing request from MCP token “${name}”` })
    await request.getByRole('textbox', { name: 'Pairing code' }).fill(pair.code)
    await request.getByRole('button', { name: 'Allow' }).click()
    const paired = prompt.getByRole('status').filter({ hasText: `MCP token “${name}” can use this tab` })
    await expect(paired).toBeVisible()

    await expect.poll(async () => (await mcp.call('browser_status')).body).toMatchObject({ attached: true, via: 'pairing' })
    const found = (await mcp.call('browser_search', { query: 'keychain' })).body as { slug: string }[]
    expect(found.map((model) => model.slug)).toContain('name-keychain')
    await mcp.call('browser_open_model', { slug: 'name-keychain' })
    await expect
      .poll(async () => ((await mcp.call('browser_status')).body as { live_tools?: string[] }).live_tools ?? [])
      .toContain('browser_set_param')
    expect(((await mcp.call('browser_status')).body as { route: string }).route).toBe('/m/name-keychain')

    const set = await mcp.call('browser_set_param', { name: 'name', value: 'Nova' })
    expect(set.isError, set.text).toBe(false)
    await expect(app.getByRole('textbox', { name: 'Name on the tag' })).toHaveValue('Nova')
    const values = (await mcp.call('browser_get_params')).body
    expect(JSON.stringify(values)).toContain('Nova')

    // The outward tool stops at the approval gate: nothing opens in the tab.
    expect((await mcp.call('browser_open_print_dialog', { kind: 'send' })).body).toMatchObject({ status: 'pending_approval' })
    await expect(app.getByRole('dialog')).toHaveCount(0)

    // The user disconnects it; the agent can no longer reach the tab.
    await paired.getByRole('button', { name: 'Disconnect' }).click()
    await expect(paired).toBeHidden()
    expect((await mcp.call('browser_get_params')).text).toMatch(/no browser attached/)
  }

  test('an MCP client pairs with the tab the user typed its code into, and drives it', async ({ page }) => {
    await relayBridge(page, AGENT!)
    await page.goto('/')
    await pairsAndDrives(page, `e2e laptop ${Date.now()}`)
  })

  test('the same, inside Bambuddy’s sandboxed frame', async ({ page, baseURL }) => {
    await relayBridge(page, AGENT!)
    // Bambuddy's External Link frame, as downloads.spec.ts replicates it.
    const host = new URL('/mockServiceWorker.js', baseURL)
    host.hostname = host.hostname === 'localhost' ? '127.0.0.1' : 'localhost'
    await page.goto(host.href)
    await page.setContent(
      `<iframe src="${new URL('/', baseURL).href}" title="ScadBuddy"
        sandbox="allow-scripts allow-same-origin allow-forms allow-popups allow-popups-to-escape-sandbox"
        style="position: fixed; inset: 0; width: 100%; height: 100%; border: 0"></iframe>`,
    )
    await pairsAndDrives(page.frameLocator('iframe'), `e2e framed ${Date.now()}`)
  })
})
