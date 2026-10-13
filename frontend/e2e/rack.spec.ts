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
    await expect(dialog.getByText('Right nozzle from the rack: position 4', { exact: false })).toBeVisible()
    await expect(dialog.getByTestId('rack-step')).toHaveCount(0)

    await dialog.getByRole('button', { name: 'Print', exact: true }).click()
    await expect(dialog.getByTestId('rack-pick')).toHaveText('Rack nozzle: position 4')
  })

  test('a position picked by hand in Advanced is the one sent', async ({ page }) => {
    const dialog = await openDialog(page)
    await dialog.getByRole('switch', { name: 'Advanced' }).click()

    // #2166 — each hotend named by the filament it last ran, never by a hex.
    const step = dialog.getByTestId('rack-step')
    await expect(step).toContainText('Position 2 · 0.4 Standard · last ran white PLA')
    await expect(step).toContainText('Position 4 · 0.4 Standard3 prints')
    await expect(step).toContainText('Position 6 · 0.4 Standard · last ran blue PETG')
    await expect(step).not.toContainText('#1E90FF')
    await dialog.getByTestId('rack-position-6').check()
    await expect(dialog.getByText('Right nozzle from the rack: position 6', { exact: false })).toBeVisible()

    await dialog.getByRole('button', { name: 'Print', exact: true }).click()
    await expect(dialog.getByTestId('rack-pick')).toHaveText('Rack nozzle: position 6')
  })

  test('back in Simple mode the hand pick is dropped', async ({ page }) => {
    const dialog = await openDialog(page)
    const advanced = dialog.getByRole('switch', { name: 'Advanced' })
    await advanced.click()
    await dialog.getByTestId('rack-position-2').check()
    await expect(dialog.getByText('Right nozzle from the rack: position 2', { exact: false })).toBeVisible()
    await advanced.click()

    await expect(dialog.getByText('Right nozzle from the rack: position 4', { exact: false })).toBeVisible()
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
