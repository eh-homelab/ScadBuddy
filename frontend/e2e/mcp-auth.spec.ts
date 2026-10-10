import { expect, test } from '@playwright/test'

// Settings → MCP authentication (#251), against the msw stand-in for the agent
// service's /api/v1/ai/mcp/auth (src/mocks/features/mcpTokens.ts).

test.describe('MCP authentication', () => {
  test.skip(
    !!process.env.E2E_BASE_URL,
    'msw-backed; the real stack is covered by real-backend.spec.ts',
  )

  test('turning authentication off takes a confirmation, then warns in both sections', async ({
    page,
  }) => {
    await page.goto('/settings')
    const auth = page.getByRole('region', { name: 'MCP authentication' })
    const tokens = page.getByRole('region', { name: 'MCP access tokens' })
    await expect(auth.getByRole('radio', { name: /Require an access token/ })).toBeChecked()

    await auth.getByRole('radio', { name: /Allow calls without a token/ }).check()
    await auth.getByLabel('Access without a token').selectOption('write')
    await auth.getByRole('button', { name: 'Save' }).click()

    const dialog = page.getByRole('dialog', { name: 'Allow MCP calls without a token?' })
    await expect(dialog).toContainText('read and write')
    await dialog.getByRole('button', { name: 'Cancel' }).click()
    await expect(dialog).toBeHidden()
    await expect(auth.getByTestId('mcp-auth-disabled-warning')).toHaveCount(0)

    await auth.getByRole('button', { name: 'Save' }).click()
    await dialog.getByRole('button', { name: 'Turn authentication off' }).click()
    await expect(dialog).toBeHidden()
    await expect(auth.getByTestId('mcp-auth-disabled-warning')).toContainText(
      'without a token, with read and write',
    )
    await expect(tokens.getByTestId('mcp-auth-note')).toContainText(
      'MCP authentication is turned off',
    )
    // #1921: one banner at the top of Settings, and one in the assistant panel.
    const panel = page.getByRole('complementary', { name: 'Assistant' })
    // The panel is closed, so the one banner is Settings' own.
    await expect(page.getByTestId('mcp-auth-banner')).toContainText('with read and write access')
    await page.keyboard.press('Control+Backquote')
    await expect(panel.getByTestId('mcp-auth-banner')).toContainText('MCP authentication is off')
    await expect(panel.getByRole('link', { name: 'Settings' })).toHaveAttribute('href', '/settings#assistant')
    await page.keyboard.press('Escape')
    await expect(panel).toBeHidden()

    // Back on: no confirmation, and both warnings go.
    await auth.getByRole('radio', { name: /Require an access token/ }).check()
    await auth.getByRole('button', { name: 'Save' }).click()
    await expect(page.getByRole('dialog')).toHaveCount(0)
    await expect(auth.getByTestId('mcp-auth-disabled-warning')).toHaveCount(0)
    await expect(tokens.getByTestId('mcp-auth-note')).toHaveCount(0)
    await expect(page.getByTestId('mcp-auth-banner')).toHaveCount(0)
  })
})
