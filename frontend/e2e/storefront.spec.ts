import { expect, test } from '@playwright/test'

// #5282 (spec 2026-11-02 §5.1-§5.4): the public storefront page over the mocks
// (src/mocks/features/storefrontPublic.ts), on a phone. `vite preview` serves the page
// under `/store` (vite.config.ts `storefrontPreview`); the edge serves it at `/`.

test.describe('the public storefront', () => {
  test.skip(!!process.env.E2E_BASE_URL, 'msw-backed; the real stack serves the page from the storefront edge')
  test.use({ viewport: { width: 390, height: 844 } })

  test('browses, customizes, renders, estimates, orders and submits on a phone', async ({ page }) => {
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

    let width = Number(await page.evaluate('document.documentElement.scrollWidth'))
    expect(width).toBeLessThanOrEqual(390)

    // Submit (#5283, spec §5.5): an option that ships asks for the address.
    await page.getByLabel('Your name').fill('Ada')
    await page.getByLabel('Email').fill('ada@example.com')
    await page.getByRole('radio', { name: /Post/ }).check()
    await page.getByLabel('Recipient').fill('Ada Lovelace')
    await page.getByLabel('Street address').fill('1 Analytical Way')
    await page.getByLabel('Town or city').fill('London')
    width = Number(await page.evaluate('document.documentElement.scrollWidth'))
    expect(width).toBeLessThanOrEqual(390)
    await page.getByRole('button', { name: 'Send my order' }).click()
    await expect(page.getByText('We have your order. We sent a link to ada@example.com.')).toBeVisible()
    await expect(page.locator('.sf-item')).toHaveCount(0)
  })

  test("opens the customer's order from its link, accepts the design and sees Pay", async ({ page }) => {
    // #5283 (spec §5.6): the token is the URL fragment, which no request carries.
    const paths: string[] = []
    page.on('request', (request) => paths.push(request.url()))
    await page.goto('/store/o#tok')
    await expect(page.getByRole('heading', { name: 'Order ORD-2026-7', level: 1 })).toBeVisible()
    await expect(page.getByText('Price: $12.50')).toBeVisible()
    await page.getByRole('button', { name: 'Accept', exact: true }).click()
    await expect(page.getByText('You accepted this design.')).toBeVisible()
    await expect(page.getByText('Price: $12.50')).toBeVisible()
    await expect(page.getByRole('link', { name: 'Pay $12.50' })).toBeVisible()
    // The mock's manual deposit, beside the balance's link: the shop's own words.
    await expect(page.getByRole('heading', { name: 'How to pay' })).toBeVisible()
    await expect(page.getByText(/Venmo @example-shop/)).toBeVisible()
    expect(paths.filter((url) => /[/=?&]tok([/?&#]|$)/.test(url))).toEqual([])
    const width = Number(await page.evaluate('document.documentElement.scrollWidth'))
    expect(width).toBeLessThanOrEqual(390)
  })

  test('fits every page in a phone width', async ({ page }) => {
    for (const path of ['/store/', '/store/l/l1', '/store/l/l2', '/store/request', '/store/draft', '/store/o#tok']) {
      await page.goto(path)
      await expect(page.getByRole('heading', { level: 1 })).toBeVisible()
      const width = Number(await page.evaluate('document.documentElement.scrollWidth'))
      expect(width, path).toBeLessThanOrEqual(390)
    }
  })
})
