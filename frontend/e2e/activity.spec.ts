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

  test('on a phone the bell is the way to the page, and the header fits', async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 })
    await page.goto('/')
    // Hidden from the nav row there, which would overflow.
    await expect(page.getByRole('navigation', { name: 'Main' }).getByRole('link', { name: 'Activity' })).toBeHidden()
    await page.getByRole('button', { name: /^Activity/ }).click()
    await page.getByRole('link', { name: 'All activity' }).click()
    await expect(page).toHaveURL(/\/activity$/)
    await expect(page.getByRole('heading', { name: 'Activity' })).toBeVisible()
    expect(await page.evaluate('document.documentElement.scrollWidth')).toBeLessThanOrEqual(390)
  })
})
