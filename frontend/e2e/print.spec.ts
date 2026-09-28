import { expect, test, type Page } from '@playwright/test'

test.describe('print dialog', () => {
  // These drive the msw worker; the real stack is covered by real-backend.spec.ts.
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
    await expect(dialog.getByRole('group', { name: 'Nozzles' })).toBeVisible()
    return dialog
  }

  test('picks 0.2 mm and Fine, prints and reports the queue entry', async ({ page }) => {
    const dialog = await openDialog(page)
    // No pipelines any more: the dialog opens on the spools, nozzles, quality and plate.
    await expect(dialog.getByText(/pipeline/i)).toHaveCount(0)

    await dialog.getByRole('radio', { name: /0\.2 mm/ }).check()
    await dialog.getByRole('radio', { name: /Fine/ }).check()
    await expect(dialog.getByRole('radio', { name: /Fine — 0\.08mm/ })).toBeChecked()
    await dialog.getByRole('button', { name: 'Print', exact: true }).click()

    const queued = dialog.getByTestId('queued-items')
    await expect(queued).toContainText('Sliced and queued for 3DP-31B-598')
    await expect(queued).toContainText('Queue #')
  })

  test('picks the filaments per slot and queues the print for one printer', async ({ page }) => {
    const dialog = await openDialog(page)
    // Both slots arrive pre-selected: the server auto-matches the plate's colors against
    // the inventory, so the common case is a read rather than four clicks.
    const slotOne = dialog.getByTestId('filament-slot-1')
    const slotTwo = dialog.getByTestId('filament-slot-2')
    await expect(slotOne.getByTestId('spool-21')).toBeChecked()
    await expect(slotTwo.getByTestId('spool-27')).toBeChecked()

    // The suggestion for slot 2 is on a shelf, so it comes with a "load this in" advisory.
    await expect(dialog.getByTestId('filament-warnings-2')).toContainText('Load Elegoo')
    // Swap it for the pink already in the AMS-HT.
    await slotTwo.getByTestId('spool-22').check()
    await expect(dialog.getByTestId('filament-warnings-2')).toBeHidden()

    await dialog.getByRole('button', { name: 'Print', exact: true }).click()
    await expect(dialog.getByTestId('queued-items')).toContainText('Queue #')
  })

  test('names a process and a slot preset in Advanced mode', async ({ page }) => {
    const dialog = await openDialog(page)
    await expect(dialog.getByLabel('Process')).toHaveCount(0)

    await dialog.getByRole('switch', { name: 'Advanced' }).click()
    await dialog.getByLabel('Process').selectOption('0.24mm Standard @BBL H2C')
    await dialog.getByLabel('Preset for slot 1').selectOption({ label: 'Bambu ABS @BBL H2C' })
    // High Flow is offered per side, and says it slices as Standard.
    await dialog.getByRole('radio', { name: 'Left High Flow' }).check()
    await expect(dialog.getByText(/High Flow presets aren't supported/)).toBeVisible()

    await dialog.getByRole('button', { name: 'Print', exact: true }).click()
    await expect(dialog.getByTestId('queued-items')).toBeVisible()
    await expect(dialog.getByTestId('run-warnings')).toContainText('slices this as Standard flow')
  })
})
