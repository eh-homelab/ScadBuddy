import { expect, test } from '@playwright/test'

// #5283 (spec 2026-11-02 §5.2): the storefront's sign-in, "Your orders" and sign-out over
// the mocks (src/mocks/features/storefrontAccount.ts), on a phone. `vite preview` serves
// the page under `/store`.

test.describe('the storefront account', () => {
  test.skip(!!process.env.E2E_BASE_URL, 'msw-backed; the real stack serves the page from the storefront edge')
  test.use({ viewport: { width: 390, height: 844 } })

  test('signs in from a mailed link, lists the orders and signs out on a phone', async ({ page }) => {
    await page.goto('/store/account/sign-in#code=ABCD-EFGH-JKMN-PQRS')
    await expect(page.getByLabel('Code')).toHaveValue('ABCD-EFGH-JKMN-PQRS')
    // The code leaves the address once read.
    await expect(page).not.toHaveURL(/#code=/)

    await page.getByLabel('Email').fill('ada@example.com')
    await page.getByRole('button', { name: 'Sign in' }).click()
    await expect(page.getByRole('heading', { name: 'Your account', level: 1 })).toBeVisible()
    await expect(page.getByText('ada@example.com')).toBeVisible()
    await expect(page.getByRole('list', { name: 'Your orders' }).getByRole('link', { name: 'ORD-2026-14' })).toHaveAttribute(
      'href',
      '/store/o?order=o-2',
    )
    const width = Number(await page.evaluate('document.documentElement.scrollWidth'))
    expect(width).toBeLessThanOrEqual(390)

    await page.getByRole('button', { name: 'Sign out' }).click()
    await expect(page.getByRole('status')).toHaveText('You are signed out.')
    await expect(page.getByRole('link', { name: 'Sign in' })).toBeVisible()
  })
})
