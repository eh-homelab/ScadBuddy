import { expect, test } from '@playwright/test'

test.describe('customizer', () => {
  // These drive the msw worker. Against a real backend the numbers are the real
  // renderer's, which e2e/real-backend.spec.ts covers instead.
  test.skip(
    !!process.env.E2E_BASE_URL,
    'msw-backed; the real stack is covered by real-backend.spec.ts',
  )

  test('opens a model, changes a parameter and generates an output', async ({ page }) => {
    await page.goto('/')

    await expect(page.getByRole('heading', { name: 'Models' })).toBeVisible()
    await page.getByRole('link', { name: /Name Keychain/ }).click()

    // The first render happens without being asked (spec §5.3).
    const bbox = page.getByTestId('bbox-readout')
    await expect(bbox).toContainText('64.1 × 37.2 × 6.8 mm')
    await expect(page.getByTestId('preview-canvas')).toBeVisible()

    const name = page.getByRole('textbox', { name: 'Name on the tag' })
    await name.fill('Nova')
    await expect(bbox).toContainText('46.7 × 37.2 × 6.8 mm')

    const generate = page.getByTestId('generate')
    await expect(generate).toBeEnabled()
    await generate.click()

    await expect(page.getByText(/^Saved /)).toBeVisible()
    await expect(page.getByRole('button', { name: 'Download 3MF' })).toBeEnabled()
    await expect(page.getByRole('button', { name: 'Send to Bambuddy' })).toBeEnabled()
  })

  test('keeps the previous preview while the next render runs', async ({ page }) => {
    await page.goto('/m/name-keychain')
    await expect(page.getByTestId('bbox-readout')).toContainText('64.1')

    await page.getByRole('textbox', { name: 'Name on the tag' }).fill('Workshop')
    await expect(page.getByText('Rendering')).toBeVisible()
    // The old dimensions stay on screen rather than blanking out.
    await expect(page.getByTestId('bbox-readout')).toBeVisible()
    await expect(page.getByTestId('bbox-readout')).toContainText('81.4', { timeout: 10_000 })
  })

  test('shows the OpenSCAD log when a render fails', async ({ page }) => {
    await page.goto('/m/name-keychain')
    await expect(page.getByTestId('bbox-readout')).toBeVisible()

    await page.getByRole('textbox', { name: 'Name on the tag' }).fill('boom')
    await expect(page.getByTestId('render-log')).toContainText('Compilation failed')
  })
})

test.describe('font picker', () => {
  test.skip(
    !!process.env.E2E_BASE_URL,
    'msw-backed; the real stack is covered by real-backend.spec.ts',
  )

  test('searches Google Fonts, previews the name and puts the pick on the parameter', async ({
    page,
  }) => {
    await page.goto('/m/name-keychain')
    await expect(page.getByTestId('bbox-readout')).toBeVisible()

    await page.getByRole('button', { name: 'Browse' }).click()
    const dialog = page.getByRole('dialog', { name: 'Choose a font' })

    // The preview defaults to the text the model will actually set.
    await expect(dialog.getByRole('textbox', { name: 'Sample text' })).toHaveValue('Reagan')

    await dialog.getByRole('searchbox', { name: 'Search fonts' }).fill('Pacifico')
    const row = dialog.getByRole('button', { name: /Pacifico/ })
    await expect(row).toBeVisible()
    await expect(row.locator('span').first()).toHaveCSS('font-family', /Pacifico/)

    await row.click()

    await expect(dialog).toBeHidden()
    await expect(page.getByRole('combobox', { name: 'Typeface' })).toHaveValue(
      'Pacifico:style=Regular',
    )
  })

  test('says so when a font cannot be downloaded', async ({ page }) => {
    await page.goto('/m/name-keychain')
    await expect(page.getByTestId('bbox-readout')).toBeVisible()

    await page.getByRole('button', { name: 'Browse' }).click()
    const dialog = page.getByRole('dialog', { name: 'Choose a font' })
    await dialog.getByRole('searchbox', { name: 'Search fonts' }).fill('Playfair')
    await dialog.getByRole('button', { name: /Playfair Display/ }).click()

    await expect(dialog.getByRole('alert')).toContainText('could not be downloaded')
    await expect(dialog).toBeVisible()
  })
})
