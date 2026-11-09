import { expect, test } from '@playwright/test'

// #5282 (spec 2026-11-02 §5.1-§5.4): the public storefront page over the mocks
// (src/mocks/features/storefrontPublic.ts), on a phone. `vite preview` serves the page
// under `/store` (vite.config.ts `storefrontPreview`); the edge serves it at `/`.

test.describe('the public storefront', () => {
  test.skip(!!process.env.E2E_BASE_URL, 'msw-backed; the real stack serves the page from the storefront edge')
  test.use({ viewport: { width: 390, height: 844 } })

  test('browses, customizes, renders, estimates and orders on a phone', async ({ page }) => {
    await page.goto('/store/')
    await expect(page.getByRole('heading', { level: 1 })).toBeVisible()

    await page.getByRole('link', { name: 'Customize Name keychain' }).click()
    await expect(page.getByRole('heading', { name: 'Name keychain', level: 1 })).toBeVisible()

    await page.getByLabel('Name', { exact: true }).fill('Ada')
    await page.getByRole('button', { name: 'Render' }).click()
    await expect(page.getByText('about $12.40, confirmed after we check it')).toBeVisible()
    // The real viewer drew the render.
    await expect(page.locator('canvas')).toBeVisible()

    await page.getByRole('button', { name: 'Add to order' }).click()
    await expect(page.getByText('Added to your order.')).toBeVisible()

    await page.getByRole('link', { name: 'See your order' }).click()
    await expect(page.getByRole('heading', { name: 'Your order', level: 1 })).toBeVisible()
    await expect(page.locator('.sf-item')).toHaveCount(1)
    await expect(page.locator('.sf-item').getByText('Ada')).toBeVisible()

    const width = Number(await page.evaluate('document.documentElement.scrollWidth'))
    expect(width).toBeLessThanOrEqual(390)
  })

  test('fits every page in a phone width', async ({ page }) => {
    for (const path of ['/store/', '/store/l/l1', '/store/l/l2', '/store/request', '/store/draft']) {
      await page.goto(path)
      await expect(page.getByRole('heading', { level: 1 })).toBeVisible()
      const width = Number(await page.evaluate('document.documentElement.scrollWidth'))
      expect(width, path).toBeLessThanOrEqual(390)
    }
  })
})
