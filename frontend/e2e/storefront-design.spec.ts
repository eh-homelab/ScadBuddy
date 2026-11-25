import { expect, test, type Page } from '@playwright/test'

// #5701 (spec 2026-11-17 §5, §10.3) over the mocks: a pipeline template's design, sold from its
// page, set up in its own interface and published. The template is the dollhouse
// (src/mocks/ui/dollhouse-kit/ is a copy of its ui/).
test.skip(!!process.env.E2E_BASE_URL, 'msw-backed; the real stack has no storefront listings yet')

async function sell(page: Page) {
  await page.goto('/m/builtin%3Adollhouse-kit')
  await expect(page.getByTestId('template-ui').locator('button[data-entry]').first()).toBeAttached({ timeout: 15_000 })
  await expect(page.getByTestId('bbox-readout')).toBeVisible({ timeout: 30_000 })
  await page.getByTestId('generate-menu').click()
  await page.getByTestId('generate-sell').click()
  await expect(page).toHaveURL(/\/storefront\/listings\/new$/)
}

async function setUp(page: Page) {
  await page.getByRole('button', { name: 'Set up the design' }).click()
  const dialog = page.getByRole('dialog', { name: 'Set up the design' })
  await expect(dialog.getByTestId('template-ui').locator('button[data-entry]').first()).toBeAttached({ timeout: 15_000 })
  return dialog
}

test('sells a pipeline design, sets it up in its own interface and publishes it', async ({ page }) => {
  await sell(page)
  const block = page.getByRole('group', { name: 'Design', exact: true })
  await expect(block).toContainText('The design the template starts with')
  await expect(block).toContainText('Set by you. Customers cannot change it.')
  // What the pipeline sets itself has no setting here.
  const settings = page.getByRole('region', { name: 'Settings' })
  await expect(settings.getByRole('group', { name: 'Course height' })).toBeVisible()
  await expect(settings.getByRole('group', { name: 'Piece', exact: true })).toHaveCount(0)

  const dialog = await setUp(page)
  // Its preview renders the working copy; the dialog has no print, output or preset of its own.
  await expect(dialog.getByTestId('bbox-readout')).toBeVisible({ timeout: 30_000 })
  const wide = dialog.getByRole('spinbutton', { name: 'Modules wide' })
  await wide.fill('4')
  await wide.blur()
  await dialog.getByRole('button', { name: 'Cancel' }).click()
  await expect(block).toContainText('The design the template starts with')
  await expect(page.getByRole('button', { name: 'Set up the design' })).toBeFocused()

  const again = await setUp(page)
  await again.getByRole('spinbutton', { name: 'Modules wide' }).fill('3')
  await again.getByRole('spinbutton', { name: 'Modules wide' }).blur()
  await again.getByRole('button', { name: 'Use this design' }).click()
  await expect(page.getByRole('dialog')).toHaveCount(0)
  // The template's own words for it.
  await expect(block).toContainText('3×1-module house')

  await page.getByTestId('listing-actions').getByRole('button', { name: 'Publish' }).click()
  await expect(page.getByTestId('storefront-listing-page').getByText('Published', { exact: true })).toBeVisible()
  // Read again, it is in the schema's words.
  await page.getByRole('link', { name: 'Back to listings' }).click()
  await page.getByTestId('storefront-listing').getByRole('link', { name: 'Dollhouse Kit' }).click()
  await expect(block).toContainText('House: Modules wide 3, Modules deep 1')
  await expect(page.getByText('Unsaved changes')).toHaveCount(0)
})

test.describe('on a phone', () => {
  test.use({ viewport: { width: 390, height: 844 } })

  test('the dialog is the whole screen, and its buttons stay in reach', async ({ page }) => {
    await sell(page)
    const dialog = await setUp(page)
    // Settled after its opening animation.
    await expect.poll(async () => dialog.boundingBox()).toEqual({ x: 0, y: 0, width: 390, height: 844 })
    await expect(dialog.getByRole('button', { name: 'Use this design' })).toBeInViewport()
    await expect(dialog.getByRole('button', { name: 'Cancel' })).toBeInViewport()
  })
})
