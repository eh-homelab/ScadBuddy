import { expect, test } from '@playwright/test'

// #1057 — the Workflows page over the msw flow runs (src/mocks/features/flows.ts).

test.describe('workflows (#1057)', () => {
  test.skip(!!process.env.E2E_BASE_URL, 'msw-backed; the real stack is covered by real-backend.spec.ts')

  test('the nav opens the runs, and an answered run moves on', async ({ page }) => {
    await page.goto('/')
    await page.getByRole('navigation', { name: 'Main' }).getByRole('link', { name: 'Workflows' }).click()
    await expect(page).toHaveURL(/\/workflows$/)
    const runs = page.getByRole('list', { name: 'Flow runs' })
    await expect(runs.locator('[data-run]')).toHaveCount(3)
    await expect(page.locator('[data-run="run-ask"]')).toContainText('Waiting for your answer: Swap to pink?')

    await page.locator('[data-run="run-ask"]').getByRole('link', { name: 'swap v1' }).click()
    await expect(page).toHaveURL(/\/workflows\/runs\/run-ask$/)
    await page.getByRole('textbox', { name: 'Your answer' }).fill('yes')
    await page.getByRole('button', { name: 'Answer' }).click()
    await expect(page.getByTestId('flow-run-status')).toHaveText('Running')
    await expect(page.getByRole('textbox', { name: 'Your answer' })).toHaveCount(0)
  })
})
