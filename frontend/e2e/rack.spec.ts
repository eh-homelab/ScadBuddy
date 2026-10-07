import { expect, test, type Page } from '@playwright/test'

/** #836 — the H2C's rack step, end to end in the browser against the msw worker. */
test.describe('rack nozzle', () => {
  test.skip(
    !!process.env.E2E_BASE_URL,
    'msw-backed; the real stack is covered by real-backend.spec.ts',
  )

  async function openDialog(page: Page) {
    await page.goto('/m/name-keychain')
    await expect(page.getByTestId('bbox-readout')).toBeVisible()
    await page.getByTestId('generate').click()
    await expect(page.getByText(/^Saved /)).toBeVisible()
    await page.getByTestId('print').click()
    const dialog = page.getByRole('dialog', { name: 'Print' })
    await expect(dialog.getByTestId('filament-slot-1')).toBeVisible()
    return dialog
  }

  test('Simple mode names the automatic pick and prints with it', async ({ page }) => {
    const dialog = await openDialog(page)
    await expect(dialog.getByText('Rack nozzle: position 4', { exact: false })).toBeVisible()
    await expect(dialog.getByLabel('Rack nozzle position')).toHaveCount(0)

    await dialog.getByRole('button', { name: 'Print', exact: true }).click()
    await expect(dialog.getByTestId('rack-pick')).toHaveText('Rack nozzle: position 4')
  })

  test('a position picked by hand in Advanced is the one sent', async ({ page }) => {
    const dialog = await openDialog(page)
    await dialog.getByRole('switch', { name: 'Advanced' }).click()

    const position = dialog.getByLabel('Rack nozzle position')
    await expect(position.locator('option')).toHaveText([
      'Automatic',
      /^Position 2 · 0\.4 Standard · PLA · 14 prints/,
      /^Position 4 · 0\.4 Standard · material unknown · 3 prints$/,
      /^Position 6 · 0\.4 Standard · PETG · 9 prints/,
    ])
    await position.selectOption('6')
    await expect(dialog.getByText('Rack nozzle: position 6', { exact: false })).toBeVisible()

    await dialog.getByRole('button', { name: 'Print', exact: true }).click()
    await expect(dialog.getByTestId('rack-pick')).toHaveText('Rack nozzle: position 6')
  })

  test('back in Simple mode the hand pick is dropped', async ({ page }) => {
    const dialog = await openDialog(page)
    const advanced = dialog.getByRole('switch', { name: 'Advanced' })
    await advanced.click()
    await dialog.getByLabel('Rack nozzle position').selectOption('2')
    await expect(dialog.getByText('Rack nozzle: position 2', { exact: false })).toBeVisible()
    await advanced.click()

    await expect(dialog.getByText('Rack nozzle: position 4', { exact: false })).toBeVisible()
    await dialog.getByRole('button', { name: 'Print', exact: true }).click()
    await expect(dialog.getByTestId('rack-pick')).toHaveText('Rack nozzle: position 4')
  })

  test('the algorithm is remembered for the printer', async ({ page }) => {
    const dialog = await openDialog(page)
    await dialog.getByRole('switch', { name: 'Advanced' }).click()
    const saved = page.waitForRequest(
      (request) => request.method() === 'PUT' && request.url().endsWith('/rack-algorithm'),
    )
    await dialog.getByLabel('Rack algorithm').selectOption('oldest_first')
    expect((await saved).postDataJSON()).toEqual({ algorithm: 'oldest_first', version: expect.any(Number) })
  })
})
