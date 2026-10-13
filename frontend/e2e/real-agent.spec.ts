import { expect, type Page, test } from '@playwright/test'

/**
 * The real assistant: the built SPA, the real backend and the real agent sidecar, on
 * one origin, with no msw anywhere. The panel talks to the agent's WebSocket
 * `/api/v1/ai/chat`, and the agent runs Claude Code against whatever its stored
 * credential points at. Use a local Anthropic-format endpoint, such as
 * `agent/test/support/fakeAnthropic.ts`, never a real model.
 *
 * Environment:
 *
 *   E2E_BASE_URL        The stack's one origin: the ingress, or `pnpm preview` without
 *                       `VITE_MOCK_API`, which routes `/api/v1/ai/*` and `/mcp` to the
 *                       agent (vite.config.ts, docs/ai/operating.md §1.1).
 *   E2E_AGENT           `1` when that stack includes the agent with a credential saved.
 *                       Without it this file skips, because CI's `image` job runs
 *                       real-backend.spec.ts against the backend container alone.
 *   E2E_AGENT_SCRIPTED  `1` when the model endpoint is the scripted one,
 *                       `agent/test/support/serveScriptedModel.ts`, which the approval
 *                       and scenario tests need. Its script (`realAgentScript.ts` beside it)
 *                       picks a scenario from a marker in the prompt, `[keychain]` or
 *                       `[bosl2:<new model's name>]` say, makes that scenario's tool
 *                       calls one result at a time, and ends with "Done." only when every
 *                       result was what the scenario expects: a render the backend
 *                       finished, say. Otherwise it ends with "Not done: <the result>".
 *                       A prompt without a marker gets "Hello from the fake model.".
 *
 * The scenarios render with the backend's own OpenSCAD and BOSL2 (the image seeds it,
 * `backend/scadbuddy/library/library_seed.py`), so run them against the image, not a
 * backend without them. A local run, for example: Postgres and a Temporal dev server;
 * the backend image on :18080 (as CI's `image` job starts it); the agent image on the
 * host network, so on :8081, with `SCADBUDDY_DATABASE_URL`, `SCADBUDDY_BACKEND_URL` and
 * a key file; `node test/support/serveScriptedModel.ts 17924` in `agent/`; a gateway
 * credential saved through `PUT /api/v1/ai/credentials` (with a loopback `Origin`) whose
 * `base_url` is `http://127.0.0.1:17924` and whose secret is not a word the replies use,
 * since the agent redacts the secret from everything it shows; then, here,
 * `SCADBUDDY_BACKEND_URL=http://127.0.0.1:18080 pnpm build && pnpm preview`, and
 * `E2E_BASE_URL=http://127.0.0.1:4173 E2E_AGENT=1 E2E_AGENT_SCRIPTED=1 pnpm exec playwright test real-agent`.
 * The file starts six chats, and the agent starts at most ten a minute for one user
 * (`MAX_NEW_SESSIONS`, agent `src/sessions/manager.ts`): run it again straight away and
 * a test fails on "Too many new chats", so leave a minute between runs.
 */
test.describe('real agent (#249)', () => {
  test.skip(!process.env.E2E_BASE_URL || process.env.E2E_AGENT !== '1', 'set E2E_BASE_URL and E2E_AGENT=1')
  test.describe.configure({ mode: 'serial' })

  test('routes the agent paths to the agent and the rest to the backend, on one origin', async ({ request }) => {
    // Spec §4.2: the agent's paths win over the backend's /api/v1/*. Every agent
    // response names the service (agent src/app.ts).
    const status = await request.get('/api/v1/ai/status')
    expect(status.ok()).toBe(true)
    expect(status.headers()['x-scadbuddy-service']).toBe('agent')
    expect(await status.json()).toMatchObject({ available: true, state: 'enabled', ai: 'enabled' })

    const mcp = await request.post('/mcp', { data: {}, failOnStatusCode: false })
    expect(mcp.headers()['x-scadbuddy-service']).toBe('agent')

    const settings = await request.get('/api/v1/settings')
    expect(settings.ok()).toBe(true)
    expect(settings.headers()['x-scadbuddy-service']).toBeUndefined()
  })

  test('the panel runs a whole turn through the agent', async ({ page }) => {
    test.setTimeout(120_000)
    await page.goto('/settings')
    await expect(page.getByTestId('ai-status')).toHaveAttribute('data-state', 'configured')

    await page.getByRole('button', { name: 'Assistant' }).click()
    const panel = page.getByRole('complementary', { name: 'Assistant' })
    const composer = panel.getByRole('textbox', { name: 'Message the assistant' })
    await composer.fill('Say hello')
    await composer.press('Enter')

    const log = panel.getByRole('log', { name: 'Conversation' })
    await expect(log.getByText('Say hello')).toBeVisible()
    await expect(panel.getByTestId('agent-status')).toHaveText('Idle', { timeout: 60_000 })
    if (process.env.E2E_AGENT_SCRIPTED === '1') {
      await expect(log.getByText('Hello from the fake model.')).toBeVisible()
    }
    // The page context is for the model; the transcript shows what was typed.
    await expect(log.getByText('<page_context>')).toHaveCount(0)
  })

  test('an outward tool call waits for approval in the panel', async ({ page }) => {
    test.skip(process.env.E2E_AGENT_SCRIPTED !== '1', 'needs the scripted model endpoint')
    test.setTimeout(120_000)
    await page.goto('/')
    await page.getByRole('button', { name: 'Assistant' }).click()
    const panel = page.getByRole('complementary', { name: 'Assistant' })
    const composer = panel.getByRole('textbox', { name: 'Message the assistant' })
    await composer.fill('Remember my print options [outward]')
    await composer.press('Enter')

    const card = panel.getByRole('region', { name: 'Needs your approval' })
    await expect(card).toBeVisible({ timeout: 60_000 })
    await expect(panel.getByTestId('agent-status')).toHaveText('Waiting for approval')
    const tool = panel.getByTestId('agent-tool').first()
    await expect(tool).toContainText('Set print options')
    await expect(tool).toContainText('outward')

    await card.getByRole('button', { name: 'Approve' }).click()
    await expect(card).toContainText('Approved by You.')
    await expect(panel.getByTestId('agent-status')).toHaveText('Idle', { timeout: 60_000 })
    await expect(panel.getByRole('log', { name: 'Conversation' }).getByText('Done.')).toBeVisible()
  })

  // #259's scenarios (#1923), each one turn of the scripted model above against the
  // real backend: "Done." is only said once every call the scenario made succeeded.
  test.describe('scenarios', () => {
    test.skip(process.env.E2E_AGENT_SCRIPTED !== '1', 'needs the scripted model endpoint')
    test.setTimeout(180_000)

    async function ask(page: Page, prompt: string) {
      await page.goto('/')
      await page.getByRole('button', { name: 'Assistant' }).click()
      const panel = page.getByRole('complementary', { name: 'Assistant' })
      const composer = panel.getByRole('textbox', { name: 'Message the assistant' })
      await composer.fill(prompt)
      await composer.press('Enter')
      await expect(panel.getByTestId('agent-status')).toHaveText('Idle', { timeout: 150_000 })
      return { panel, log: panel.getByRole('log', { name: 'Conversation' }) }
    }

    /** The model the turn made, found by the unique name the prompt gave it. */
    async function madeModel(page: Page, name: string) {
      const listed = await page.request.get('/api/v1/models')
      expect(listed.ok()).toBe(true)
      const body = (await listed.json()) as { slug: string; name: string }[] | { items: { slug: string; name: string }[] }
      const made = (Array.isArray(body) ? body : body.items).find((model) => model.name === name)
      expect(made, `a model named ${name}`).toBeDefined()
      const path = `/api/v1/models/${encodeURIComponent(made!.slug)}`
      const [got, source] = await Promise.all([page.request.get(path), page.request.get(`${path}/source`)])
      expect(got.ok()).toBe(true)
      expect(source.ok()).toBe(true)
      return { ...((await got.json()) as { libraries: { name: string }[]; upstream: unknown }), source: await source.text() }
    }

    test("changes the keychain's text and colour, and the backend renders it", async ({ page }) => {
      const { panel, log } = await ask(page, 'Make the keychain say Ada, with red letters [keychain]')
      await expect(log.getByText('Done.', { exact: true })).toBeVisible()
      await expect(panel.getByTestId('agent-tool').first()).toContainText('Render')
      await expect(panel.getByRole('region', { name: 'Needs your approval' })).toHaveCount(0)
    })

    test('makes a new cable label from the bundled one and renders it', async ({ page }) => {
      const name = `E2E cable label ${Date.now()}`
      const { log } = await ask(page, `Make me a new cable label model [cable-label:${name}]`)
      await expect(log.getByText('Done.', { exact: true })).toBeVisible()
      const model = await madeModel(page, name)
      // Kept linked to the template it came from.
      expect(JSON.stringify(model.upstream)).toContain('cable-label')
    })

    test('adds BOSL2 to a new model, rounds its plate with it, and renders it', async ({ page }) => {
      const name = `E2E rounded plate ${Date.now()}`
      const { log } = await ask(page, `Add BOSL2 and use a rounded cube [bosl2:${name}]`)
      await expect(log.getByText('Done.', { exact: true })).toBeVisible()
      const model = await madeModel(page, name)
      expect(model.libraries.map((library) => library.name)).toContain('BOSL2')
      expect(model.source).toContain('include <BOSL2/std.scad>')
      expect(model.source).toContain('cuboid(')
    })

    test('an outward call denied at the confirmation is not made', async ({ page }) => {
      await page.goto('/')
      await page.getByRole('button', { name: 'Assistant' }).click()
      const panel = page.getByRole('complementary', { name: 'Assistant' })
      const composer = panel.getByRole('textbox', { name: 'Message the assistant' })
      await composer.fill('Remember my print options [outward]')
      await composer.press('Enter')

      const card = panel.getByRole('region', { name: 'Needs your approval' })
      await expect(card).toBeVisible({ timeout: 60_000 })
      await card.getByRole('button', { name: 'Deny' }).click()
      await expect(card).toContainText('Denied by You.')
      await expect(card.getByRole('button', { name: 'Approve' })).toHaveCount(0)
      await expect(panel.getByTestId('agent-status')).toHaveText('Idle', { timeout: 60_000 })
      // The denial came back to the model as the call's result, which it reports.
      const log = panel.getByRole('log', { name: 'Conversation' })
      await expect(log.getByText(/^Not done: /)).toBeVisible()
      await expect(log.getByText('Done.', { exact: true })).toHaveCount(0)
    })
  })
})
