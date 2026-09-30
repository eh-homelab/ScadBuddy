import { expect, test, type FrameLocator, type Page } from '@playwright/test'

// Settings → MCP access tokens (#251), against the msw stand-in for the agent
// service's /api/v1/ai/mcp-tokens (src/mocks/features/mcpTokens.ts).

type Scope = Page | FrameLocator

async function createToken(scope: Scope, name: string) {
  const section = scope.getByRole('region', { name: 'MCP access tokens' })
  await section.getByLabel('Token name').fill(name)
  await section.getByLabel('Access').selectOption('write')
  await section.getByRole('button', { name: 'Create token' }).click()
  const panel = section.getByTestId('minted-token')
  await expect(panel).toContainText(`New token: ${name}`)
  await expect(panel).toContainText('This is the only time it is shown')
  const value = panel.getByTestId('minted-token-value')
  await expect(value).toHaveText(/^sbmcp_[A-Za-z0-9_-]{43}$/)
  return { section, panel, token: (await value.textContent()) ?? '' }
}

test.describe('MCP access tokens', () => {
  test.skip(
    !!process.env.E2E_BASE_URL,
    'msw-backed; the real stack is covered by real-backend.spec.ts',
  )

  test('creates a token, copies it once, and revokes it after a confirmation', async ({
    page,
    context,
    baseURL,
  }) => {
    await context.grantPermissions(['clipboard-read', 'clipboard-write'], { origin: baseURL })
    await page.goto('/settings')
    const { section, panel, token } = await createToken(page, 'Claude Code')

    await panel.getByRole('button', { name: 'Copy token' }).click()
    await expect(panel.getByRole('status')).toHaveText('Copied to the clipboard.')
    expect(await page.evaluate('navigator.clipboard.readText()')).toBe(token)

    const rows = section.getByRole('list', { name: 'Tokens' }).getByTestId('mcp-token')
    await expect(rows).toHaveCount(3)
    await expect(rows.first()).toContainText('Claude Code')
    await expect(rows.first()).toContainText('Write')

    await panel.getByRole('button', { name: 'Done' }).click()
    await expect(panel).toBeHidden()
    await expect(page.locator('body')).not.toContainText(token)

    await section.getByRole('button', { name: 'Revoke Claude Code' }).click()
    const dialog = page.getByRole('dialog', { name: 'Revoke Claude Code?' })
    await dialog.getByRole('button', { name: 'Revoke token' }).click()
    await expect(dialog).toBeHidden()
    await expect(rows.first()).toContainText('Revoked')
    await expect(section.getByRole('button', { name: 'Revoke Claude Code' })).toHaveCount(0)
  })

  test('copies the token inside Bambuddy’s sandboxed frame', async ({ page, context, baseURL, browserName }) => {
    // Bambuddy's External Link frame: another origin, its sandbox flags and no
    // allow="clipboard-write", so the async Clipboard API is not granted to it.
    const host = new URL('/mockServiceWorker.js', baseURL)
    host.hostname = host.hostname === 'localhost' ? '127.0.0.1' : 'localhost'
    // Bambuddy's page, not ours: served without ScadBuddy's page CSP, which would refuse
    // to frame another origin.
    await page.route(host.href, (route) => route.fulfill({ contentType: 'text/html', body: '<!doctype html>' }))
    // A routed page is not on the loopback address space, so Chrome's Local Network
    // Access checks refuse its loopback frame. Bambuddy on the LAN is local; grant it.
    // Chromium's permission: another browser's context throws on the unknown name.
    if (browserName === 'chromium') await context.grantPermissions(['local-network-access'])
    await page.goto(host.href)
    await page.setContent(
      `<iframe src="${new URL('/settings', baseURL).href}" title="ScadBuddy"
        sandbox="allow-scripts allow-same-origin allow-forms allow-popups allow-popups-to-escape-sandbox"
        style="position: fixed; inset: 0; width: 100%; height: 100%; border: 0"></iframe>`,
    )
    const frame = page.frameLocator('iframe')
    const { panel, token } = await createToken(frame, 'In the frame')

    await panel.getByRole('button', { name: 'Copy token' }).click()
    // Either the fallback copy worked, or the token is selected for Ctrl+C.
    await expect(panel.getByRole('status')).toHaveText(/Copied to the clipboard\.|press Ctrl\+C/)
    const app = page.frames().find((candidate) => candidate.url().endsWith('/settings'))
    if (!app) throw new Error('the ScadBuddy frame is not loaded')
    const status = await panel.getByRole('status').textContent()
    if (status?.includes('Ctrl+C')) {
      expect(await app.evaluate('String(window.getSelection())')).toBe(token)
    }

    // The revoke dialog works in the frame too.
    await frame.getByRole('button', { name: 'Revoke In the frame' }).click()
    await frame.getByRole('dialog', { name: 'Revoke In the frame?' }).getByRole('button', { name: 'Revoke token' }).click()
    await expect(frame.getByRole('button', { name: 'Revoke In the frame' })).toHaveCount(0)
  })
})
