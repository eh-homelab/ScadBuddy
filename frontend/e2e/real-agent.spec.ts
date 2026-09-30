import { expect, test } from '@playwright/test'

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
 *   E2E_AGENT_SCRIPTED  `1` when the model endpoint follows this script, which the
 *                       approval test needs:
 *                       - a request whose messages hold a `tool_result` gets the text
 *                         "Done." (Claude Code appends its own context after the
 *                         result, so the result is not always the last message);
 *                       - otherwise, messages containing "[outward]" get one `tool_use` of
 *                         `mcp__scadbuddy__set_print_options` with input
 *                         `{"scope": "global", "options": {}}`;
 *                       - anything else gets the text "Hello from the fake model."
 *
 * A local run, for example: backend on :8080, the agent on :8081 with
 * `SCADBUDDY_DATABASE_URL` and a key file, a gateway credential saved through
 * `PUT /api/v1/ai/credentials` whose `base_url` is the fake endpoint, then
 * `pnpm build && pnpm preview`, and
 * `E2E_BASE_URL=http://127.0.0.1:4173 E2E_AGENT=1 pnpm exec playwright test real-agent`.
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
    await expect(tool).toContainText('set_print_options')
    await expect(tool).toContainText('outward')

    await card.getByRole('button', { name: 'Approve' }).click()
    await expect(card).toContainText('Approved by You.')
    await expect(panel.getByTestId('agent-status')).toHaveText('Idle', { timeout: 60_000 })
    await expect(panel.getByRole('log', { name: 'Conversation' }).getByText('Done.')).toBeVisible()
  })
})
