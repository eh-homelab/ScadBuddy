import { expect, test } from '@playwright/test'

// #6133 (spec 2026-11-13 §4.3, §10.5): a customer's own models over the mocks
// (src/mocks/features/storefrontModels.ts), on a phone. The browser PUTs each file to the
// mock bucket's presigned URL; the cart shows the file "Checking", then "Ready" with its
// preview. The order `/o#models` shows its files, links and things to buy.

test.describe("a customer's own models", () => {
  test.skip(!!process.env.E2E_BASE_URL, 'msw-backed; the real stack serves the page from the storefront edge')
  test.use({ viewport: { width: 390, height: 844 } })

  test('uploads a model, and the cart shows it checked', async ({ page }) => {
    await page.goto('/store/print-my-model')
    await expect(page.getByRole('heading', { name: 'Print my own model', level: 1 })).toBeVisible()
    await page.getByLabel('Model files').setInputFiles({ name: 'bracket.stl', mimeType: 'model/stl', buffer: Buffer.from('solid bracket\nendsolid bracket\n') })
    await page.getByLabel('Link to a model').fill('https://www.printables.com/model/77-hinge')
    await page.getByRole('button', { name: 'Add link' }).click()
    let width = Number(await page.evaluate('document.documentElement.scrollWidth'))
    expect(width).toBeLessThanOrEqual(390)
    await page.getByRole('button', { name: 'Add to my order' }).click()
    await expect(page.getByText('Your models are in your order.')).toBeVisible()

    await page.getByRole('link', { name: 'See your order' }).click()
    const models = page.getByRole('list', { name: 'Your models' })
    await expect(models.getByRole('listitem')).toHaveCount(2)
    // The link is text: the page never makes a customer's link an anchor.
    await expect(models.getByText('https://www.printables.com/model/77-hinge')).toBeVisible()
    await expect(models.locator('a[href^="https://"]')).toHaveCount(0)
    // The cart reads itself again while the file is checked.
    await expect(models.getByText('Ready')).toBeVisible({ timeout: 15_000 })
    await expect(models.getByRole('img', { name: 'Preview of bracket.stl' })).toBeVisible()
    width = Number(await page.evaluate('document.documentElement.scrollWidth'))
    expect(width).toBeLessThanOrEqual(390)
  })

  test("shows the order's files, links and things to buy, with the token in no request", async ({ page }) => {
    const urls: string[] = []
    page.on('request', (request) => urls.push(request.url()))
    await page.goto('/store/o#models')
    await expect(page.getByRole('heading', { name: 'Order ORD-2026-9', level: 1 })).toBeVisible()
    await expect(page.getByRole('img', { name: 'Preview of bracket.3mf' })).toBeVisible()
    await expect(page.getByText('Purchase pending: the shop is pricing 1 thing to buy for you.')).toBeVisible()
    await expect(page.getByRole('link', { name: 'Upload it again' })).toBeVisible()
    expect(urls.filter((url) => /[/=?&]models([/?&#]|$)/.test(url))).toEqual([])
    const width = Number(await page.evaluate('document.documentElement.scrollWidth'))
    expect(width).toBeLessThanOrEqual(390)
  })
})
