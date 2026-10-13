import { expect, test } from '@playwright/test'

test.describe('activity', () => {
  test.skip(!!process.env.E2E_BASE_URL, 'msw-backed; the real stack is covered by real-backend.spec.ts')

  test('filters, opens a row, and shows Advanced', async ({ page }) => {
    await page.goto('/')
    await page.getByRole('link', { name: 'Activity' }).click()
    await page.getByRole('button', { name: /^Failed/ }).click()
    await expect(page).toHaveURL(/status=failed/)
    await page.getByTestId(/activity-row-operation/).first().getByRole('button', { name: 'Import a model' }).click()
    await page.getByRole('switch', { name: 'Advanced' }).click()
    await expect(page.getByText(/op-model_import/)).toBeVisible()
  })
})
