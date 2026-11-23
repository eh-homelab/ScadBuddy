import { expect, test } from '@playwright/test'

/**
 * #6936 — Settings → MCP access' plugin feed takes an installed plugin package in and out,
 * against the msw stand-ins for the agent service (`src/mocks/features/pluginFeeds.ts` over
 * `src/mocks/aiPlugins.ts`). A package is listed only once its pin is approved.
 */

test.describe('plugin feed packages (#6936)', () => {
  test.skip(!!process.env.E2E_BASE_URL, 'msw-backed; the agent service is not in the real stack yet')

  test('adds an installed package, shows it waits for approval, and removes it', async ({ page }) => {
    await page.goto('/settings/tools')
    const packages = page.getByRole('region', { name: 'Plugin packages' })
    await packages.getByLabel('Repository URL').fill('https://git.example/greeter.git')
    await packages.getByRole('button', { name: 'Fetch and review' }).click()
    await expect(packages.getByRole('listitem', { name: 'Plugin package greeter' })).toBeVisible()

    await page.goto('/settings/mcp')
    const feed = page.getByTestId('plugin-feed')
    const list = feed.getByRole('list', { name: 'Plugins in the feed' })
    await expect(list).toBeVisible()
    await expect(list.getByText('greeter', { exact: true })).toHaveCount(1)

    // The seeded feed already names a hosted "greeter"; the package installed above is offered
    // under its own name only when the feed does not name it, so take that one out first.
    await feed.getByRole('button', { name: 'Remove greeter from the feed' }).click()
    await expect(list.getByText('greeter', { exact: true })).toHaveCount(0)

    await feed.getByRole('combobox', { name: 'Plugin package to add' }).click()
    await page.getByRole('option', { name: 'greeter' }).click()
    await expect(feed.getByText('It is listed once you approve its pin under Tools & plugins.')).toBeVisible()
    await feed.getByRole('button', { name: 'Add to feed' }).click()
    const row = list.getByRole('listitem').filter({ hasText: 'greeter' })
    await expect(row).toContainText('Not listed until its pin is approved')

    await feed.getByRole('button', { name: 'Remove greeter from the feed' }).click()
    await expect(row).toHaveCount(0)
  })
})
