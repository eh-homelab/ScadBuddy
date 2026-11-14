import { expect, test, type Page } from '@playwright/test'

// #5282 (spec 2026-11-02 §5.3) — the back office's Storefront page over the mocks
// (src/mocks/features/storefront.ts): make and publish two listings, put the second
// first, and the order is still there after a reload.

async function makeListing(page: Page, title: string) {
  await page.getByRole('button', { name: 'New listing' }).click()
  const titleField = page.getByLabel('Title')
  // The new listing takes the first template's name once the templates load.
  await expect(titleField).toHaveValue('Name Keychain')
  await titleField.fill(title)
  // Its parameters load from the schema before it can be published as offered.
  await expect(page.getByTestId('listing-preview').getByLabel('Name on the tag')).toBeVisible()
  await page.getByRole('button', { name: 'Publish' }).click()
  await expect(page.getByRole('button', { name: `Edit ${title}` })).toContainText('Published')
}

test.describe('storefront listings', () => {
  test.skip(!!process.env.E2E_BASE_URL, 'msw-backed; the real stack has no storefront listings yet')

  test('makes, publishes and orders two listings, and the order survives a reload', async ({ page }) => {
    await page.goto('/')
    await page.getByRole('navigation', { name: 'Main' }).getByRole('link', { name: 'Storefront' }).click()
    await expect(page.getByRole('heading', { name: 'Storefront', level: 1 })).toBeVisible()

    await makeListing(page, 'Keychain')
    await makeListing(page, 'Tag')
    const titles = page.getByTestId('storefront-listing').getByRole('button', { name: /^Edit / })
    await expect(titles).toHaveText([/^Keychain/, /^Tag/])

    await page.getByRole('button', { name: 'Move Tag up' }).click()
    await expect(titles).toHaveText([/^Tag/, /^Keychain/])

    await page.reload()
    await expect(titles).toHaveText([/^TagPublished/, /^KeychainPublished/])
  })
})
