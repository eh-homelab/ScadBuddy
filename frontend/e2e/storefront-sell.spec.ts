import { expect, test } from '@playwright/test'

// #6131 (design 2026-11-13 §3) — "Sell on storefront" over the mocks: from the Customize
// page's Generate menu to a published listing, the editor filled in from the render on
// screen. The mocked price book has no filament of the keychain's two colours, so the
// editor names them and a filament is chosen for each before Publish.

test.describe('sell on storefront', () => {
  test.skip(!!process.env.E2E_BASE_URL, 'msw-backed; the real stack has no storefront listings yet')

  test('lists the design on screen in one step and publishes it', async ({ page }) => {
    await page.goto('/m/name-keychain')
    await expect(page.getByTestId('bbox-readout')).toContainText('64.1', { timeout: 30_000 })

    await page.getByTestId('generate-menu').click()
    await page.getByTestId('generate-sell').click()
    await expect(page).toHaveURL(/\/storefront$/)

    // Filled in: the title, the picture, the name the customer types, the geometry set.
    await expect(page.getByLabel('Title')).toHaveValue('Name Keychain')
    await expect(page.getByRole('checkbox', { name: 'Show thumbnail.png' })).toBeChecked()
    const preview = page.getByTestId('listing-preview')
    await expect(preview.getByLabel('Name on the tag')).toBeVisible()
    await expect(preview.getByLabel('Text size')).toHaveCount(0)
    await expect(page.getByRole('region', { name: 'Set by you' })).toContainText('Text size 14')

    const problems = page.getByTestId('colour-problems')
    await expect(problems).toContainText('Plate is #1B6CA8')
    await problems.getByRole('combobox', { name: 'Filament for Plate' }).click()
    await page.getByRole('option', { name: 'PETG Black' }).click()
    await problems.getByRole('combobox', { name: 'Filament for Text' }).click()
    await page.getByRole('option', { name: 'PLA Sunflower Yellow' }).click()
    await expect(problems).toHaveCount(0)

    await page.getByTestId('listing-actions').getByRole('button', { name: 'Publish' }).click()
    await expect(page.getByRole('button', { name: 'Edit Name Keychain' })).toContainText('Published')
  })
})
