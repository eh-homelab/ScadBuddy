import { expect, test, type FrameLocator, type Page, type WebSocketRoute } from '@playwright/test'

/**
 * #254 — the tab's socket to the agent (`src/agent/link.ts`) in a real browser, at the
 * top level and inside Bambuddy's sandboxed frame. Playwright stands in for the agent
 * service at `/api/v1/ai/bridge` (`page.routeWebSocket`), speaking the agent's side of
 * the protocol (agent `src/bridge/protocol.ts`), so every call here crosses a WebSocket
 * into the page, runs through the bridge's handlers against the real DOM, and comes back
 * as a frame. The agent's own side of that socket, with the tab's link code, is agent
 * `test/bridge.e2e.test.ts`; `agent-link.real.spec.ts` runs the two together.
 *
 * The msw-mocked bundle opens the socket only when asked (`AgentLink.tsx`), so the
 * other specs are not affected.
 */

type Frame = { type: string; [key: string]: unknown }
type Outcome = { ok: true; result: unknown } | { ok: false; error: { code: string; message: string } }

const PAIRING = { id: 'p-1', label: 'MCP token “laptop”', expiresAt: new Date(Date.now() + 5 * 60_000).toISOString() }

/** The agent end of the tab's socket: what the tab sent, and calls into it. */
async function fakeAgent(page: Page) {
  const received: Frame[] = []
  let socket: WebSocketRoute | undefined
  let n = 0
  await page.addInitScript(() => {
    ;(globalThis as { __scadbuddyTabLink?: boolean }).__scadbuddyTabLink = true
  })
  await page.routeWebSocket(/\/api\/v1\/ai\/bridge$/, (ws) => {
    socket = ws
    ws.onMessage((message) => received.push(JSON.parse(String(message)) as Frame))
  })
  const send = (frame: Frame) => {
    if (!socket) throw new Error('the tab has not connected')
    socket.send(JSON.stringify({ v: 1, ...frame }))
  }
  const agent = {
    received,
    send,
    /** The frames of one type the tab sent, oldest first. */
    of: (type: string) => received.filter((frame) => frame.type === type),
    async hello(): Promise<Frame> {
      await expect.poll(() => agent.of('hello').length, { timeout: 15_000 }).toBe(1)
      return agent.of('hello')[0]!
    },
    async call(tool: string, args: Record<string, unknown> = {}): Promise<Outcome> {
      const id = `call-${++n}`
      send({ type: 'call', id, tool, args })
      await expect.poll(() => received.some((f) => f.type === 'result' && f.id === id), { timeout: 30_000 }).toBe(true)
      return received.find((f) => f.type === 'result' && f.id === id)!.outcome as Outcome
    },
    async result<T>(tool: string, args: Record<string, unknown> = {}): Promise<T> {
      const outcome = await agent.call(tool, args)
      expect(outcome, `${tool} ${JSON.stringify(args)}`).toMatchObject({ ok: true })
      return (outcome as { result: T }).result
    },
  }
  return agent
}

/** Bambuddy's External Link frame, as `downloads.spec.ts` replicates it. */
async function framed(page: Page, baseURL: string | undefined, path: string): Promise<FrameLocator> {
  const host = new URL('/mockServiceWorker.js', baseURL)
  host.hostname = host.hostname === 'localhost' ? '127.0.0.1' : 'localhost'
  await page.goto(host.href)
  await page.setContent(
    `<iframe src="${new URL(path, baseURL).href}" title="ScadBuddy"
      sandbox="allow-scripts allow-same-origin allow-forms allow-popups allow-popups-to-escape-sandbox"
      style="position: fixed; inset: 0; width: 100%; height: 100%; border: 0"></iframe>`,
  )
  return page.frameLocator('iframe')
}

async function drivesTheCustomizer(agent: Awaited<ReturnType<typeof fakeAgent>>, app: Page | FrameLocator) {
  const hello = await agent.hello()
  // The catalogue puts its view in the query ("/?view=cards").
  expect(hello).toMatchObject({ v: 1, route: expect.stringMatching(/^\/(\?|$)/), tabId: expect.stringMatching(/^[A-Za-z0-9_-]{22}$/) })
  expect(hello.live).toEqual(expect.arrayContaining(['navigate', 'snapshot', 'search', 'open_model']))

  // The customizer's tools answer "unavailable" until its page is open, never nothing.
  expect(await agent.call('get_params')).toMatchObject({ ok: false, error: { code: 'unavailable' } })
  const found = await agent.result<{ slug: string }[]>('search', { query: 'keychain' })
  expect(found.map((model) => model.slug)).toContain('name-keychain')
  await agent.result('open_model', { slug: 'name-keychain' })

  // The tab reports the move: its new route and the tools live there.
  await expect
    .poll(() => agent.of('state').at(-1), { timeout: 15_000 })
    .toMatchObject({ route: '/m/name-keychain', live: expect.arrayContaining(['set_param', 'render']) })

  expect((await agent.result<{ status: string }>('render', { timeout_ms: 20_000 })).status).toBe('done')
  await agent.result('set_param', { name: 'name', value: 'Nova' })
  await expect(app.getByRole('textbox', { name: 'Name on the tag' })).toHaveValue('Nova')
  await expect(app.locator('[data-param="name"]')).toHaveClass(/sb-agent-touch/)
  expect(await agent.call('set_param', { name: 'text_size', value: 99 })).toMatchObject({
    ok: false,
    error: { code: 'invalid_args' },
  })
  const snapshot = await agent.result<{ route: string }>('snapshot')
  expect(snapshot.route).toBe('/m/name-keychain')
}

async function pairsOnlyWithTheUser(agent: Awaited<ReturnType<typeof fakeAgent>>, app: Page | FrameLocator) {
  agent.send({ type: 'pairings', pending: [PAIRING], paired: [] })
  const prompt = app.getByRole('region', { name: 'Agent pairing' })
  await expect(prompt).toContainText('MCP token “laptop” asks to use this tab')

  // A paired agent's own fallbacks cannot answer for the user.
  expect(await agent.call('fill', { label: 'Pairing code', value: 'ABCD-EFGH' })).toMatchObject({
    ok: false,
    error: { code: 'refused' },
  })
  expect(await agent.call('click', { role: 'button', name: 'Deny' })).toMatchObject({ ok: false, error: { code: 'refused' } })
  expect(agent.of('pairing.accept')).toEqual([])
  expect(agent.of('pairing.deny')).toEqual([])

  // The user types the code the agent gave them.
  await prompt.getByRole('textbox', { name: 'Pairing code' }).fill('abcd-efgh')
  await prompt.getByRole('button', { name: 'Allow' }).click()
  await expect.poll(() => agent.of('pairing.accept')).toEqual([{ v: 1, type: 'pairing.accept', id: 'p-1', code: 'abcd-efgh' }])
  agent.send({ type: 'pairing.result', id: 'p-1', ok: false, message: 'That is not the code. 4 tries left.' })
  await expect(prompt.getByRole('alert')).toHaveText('That is not the code. 4 tries left.')

  agent.send({ type: 'pairings', pending: [], paired: [PAIRING] })
  await expect(prompt.getByRole('status')).toContainText('MCP token “laptop” can use this tab until')
  await prompt.getByRole('button', { name: 'Disconnect' }).click()
  await expect.poll(() => agent.of('pairing.end')).toEqual([{ v: 1, type: 'pairing.end', id: 'p-1' }])
  agent.send({ type: 'pairings', pending: [], paired: [] })
  await expect(prompt).toBeHidden()
}

test.describe('agent link (#254)', () => {
  test.skip(!!process.env.E2E_BASE_URL, 'msw-backed; the real stack has no mocked bundle')

  test('an agent drives the customizer through the tab socket', async ({ page }) => {
    const agent = await fakeAgent(page)
    await page.goto('/')
    await drivesTheCustomizer(agent, page)
  })

  test('the pairing prompt answers only for the user', async ({ page }) => {
    const agent = await fakeAgent(page)
    await page.goto('/')
    await agent.hello()
    await pairsOnlyWithTheUser(agent, page)
  })

  test('the same, inside Bambuddy’s sandboxed frame', async ({ page, baseURL }) => {
    const agent = await fakeAgent(page)
    const frame = await framed(page, baseURL, '/')
    await drivesTheCustomizer(agent, frame)
    await pairsOnlyWithTheUser(agent, frame)
  })
})
