import { expect, test } from '@playwright/test'

// #6131 (design 2026-11-13 §3) — "Sell on storefront" over the mocks: from the Customize
// page's Generate menu to a published listing, the editor filled in from the render on
// screen. The mocked price book has no filament of the keychain's two colours, so each
// colour starts at one on offer (#6144).

test.describe('sell on storefront', () => {
  test.skip(!!process.env.E2E_BASE_URL, 'msw-backed; the real stack has no storefront listings yet')

  test('lists the design on screen in one step and publishes it', async ({ page }) => {
    await page.goto('/m/name-keychain')
    await expect(page.getByTestId('bbox-readout')).toContainText('64.1', { timeout: 30_000 })

    await page.getByTestId('generate-menu').click()
    await page.getByTestId('generate-sell').click()
    await expect(page).toHaveURL(/\/storefront\/listings\/new$/)

    // Filled in: the title, the picture, the name the customer types, the geometry set.
    await expect(page.getByLabel('Title', { exact: true })).toHaveValue('Name Keychain')
    const picture = page.getByTestId('listing-picture')
    await expect(picture).toHaveCount(1)
    await expect(picture.getByRole('img', { name: 'thumbnail.png' })).toBeVisible()
    const preview = page.getByTestId('listing-preview')
    await expect(preview.getByLabel('Name on the tag')).toBeVisible()
    await expect(preview.getByLabel('Text size')).toHaveCount(0)
    // #6290 — a locked setting shows its value in place, in the one list.
    await expect(page.getByRole('region', { name: 'Settings' }).getByLabel('Value of Text size')).toHaveValue('14')

    // #6144 — no filament on offer has either template colour, so each starts at one that is,
    // and the person may choose another.
    await expect(page.getByTestId('colour-problems')).toHaveCount(0)
    const settings = page.getByRole('region', { name: 'Settings' })
    await expect(settings.getByRole('combobox', { name: 'Default of Plate' })).toContainText('PLA Sunflower Yellow')
    await settings.getByRole('combobox', { name: 'Default of Text' }).click()
    await page.getByRole('option', { name: 'PETG Black' }).click()
    await expect(settings.getByRole('combobox', { name: 'Default of Text' })).toContainText('PETG Black')

    // #6258 — the materials allowed start as the shop's default; the listing chooses its own.
    const allowed = page.getByRole('group', { name: 'Materials allowed' })
    await expect(allowed).toContainText('Shop default (PETG, PLA)')
    await allowed.getByRole('button', { name: 'Choose for this listing' }).click()
    await allowed.getByRole('button', { name: 'Move PLA up' }).click()
    await expect(allowed.getByTestId('material-row')).toHaveText([/^1PLA/, /^2PETG/])
    // The colours on offer are grouped by material, in the list's order.
    const colours = page.getByRole('group', { name: 'Colours on offer' })
    const showAll = colours.getByRole('button', { name: /^Show all/ })
    if (await showAll.isVisible()) await showAll.click()
    await expect(colours.getByTestId('material-colours').first()).toHaveAttribute('aria-label', 'PLA colours')
    await expect(colours.getByTestId('material-colours').nth(1)).toHaveAttribute('aria-label', 'PETG colours')
    // Without PETG, its ticked filament is named apart, and publish refuses it beside the list.
    await allowed.getByRole('button', { name: 'Remove PETG' }).click()
    await expect(page.getByTestId('materials-not-allowed')).toContainText('PETG Black')
    await page.getByTestId('listing-actions').getByRole('button', { name: 'Publish' }).click()
    await expect(allowed.getByRole('alert')).toContainText('PETG Black is PETG, which this listing does not allow')
    await page.getByTestId('materials-not-allowed').getByRole('button', { name: 'Allow PETG' }).click()
    await expect(allowed.getByRole('alert')).toHaveCount(0)

    await page.getByTestId('listing-actions').getByRole('button', { name: 'Publish' }).click()
    await expect(page.getByTestId('storefront-listing-page').getByText('Published', { exact: true })).toBeVisible()
    // #6213 — the picture the public site now shows is marked live.
    await expect(picture.getByText('Live', { exact: true })).toBeVisible()
    await page.getByRole('link', { name: 'Back to listings' }).click()
    await expect(page.getByTestId('storefront-listing')).toContainText('Published')
  })
})
