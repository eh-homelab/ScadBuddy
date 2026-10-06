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
    await expect(dialog.getByTestId('filament-slot-1')).toBeVisible()
    return dialog
  }

  test('opens in Simple mode on the spools and Print alone, and prints (#768)', async ({ page }) => {
    const dialog = await openDialog(page)
    // No pipelines any more, and no nozzle, quality, plate, options, project or copies
    // until Advanced is on.
    await expect(dialog.getByText(/pipeline/i)).toHaveCount(0)
    await expect(dialog.getByRole('group', { name: 'Nozzles' })).toHaveCount(0)
    await expect(dialog.getByLabel('Copies')).toHaveCount(0)
    await expect(dialog.getByLabel('Plate')).toHaveCount(0)
    await expect(dialog.getByTestId('print-checks')).toBeVisible()

    await dialog.getByRole('button', { name: 'Print', exact: true }).click()
    await expect(dialog.getByTestId('queued-items')).toContainText('Queue #')
  })

  test('picks 0.2 mm and Fine in Advanced, prints and reports the queue entry', async ({ page }) => {
    const dialog = await openDialog(page)
    await dialog.getByRole('switch', { name: 'Advanced' }).click()

    await dialog.getByRole('radio', { name: /0\.2 mm/ }).check()
    await dialog.getByLabel('Process').selectOption('0.08mm High Quality @BBL H2C 0.2 nozzle')
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

  test('lists the checks with their sources, suppresses one, and still prints', async ({ page }) => {
    const dialog = await openDialog(page)
    const checks = dialog.getByTestId('print-checks')
    await expect(checks.getByTestId('checks-headline')).toHaveText('2 suggestions')

    const overhang = checks.getByTestId('diagnostic-SB1003')
    await expect(overhang).toContainText('Overhangs past the support threshold')
    await expect(overhang).toContainText('Where: a 18.0 × 8.0 × 1.0 mm region')
    const source = overhang.getByRole('link', {
      name: 'Bambu Studio PrintConfig.cpp: support_threshold_angle',
    })
    await expect(source).toHaveAttribute('target', '_blank')

    // Suppressed at a scope, with the reason a suppression requires.
    const edges = checks.getByTestId('diagnostic-SB1002:part-2')
    await edges.getByRole('button', { name: 'Suppress…' }).click()
    const form = edges.getByRole('form', { name: 'Suppress SB1002:part-2' })
    await expect(form.getByRole('button', { name: 'Suppress' })).toBeDisabled()
    await form.getByLabel('Scope').selectOption({ label: 'This template' })
    await form.getByLabel('Reason').fill('the seam is inside the ring')
    await form.getByRole('button', { name: 'Suppress' }).click()
    await expect(edges).toHaveCount(0)
    await expect(checks.getByTestId('checks-headline')).toHaveText('1 suggestion')
    await checks.getByText('1 not shown').click()
    await expect(checks.getByTestId('checks-set-aside')).toContainText(
      'suppressed for This template: the seam is inside the ring',
    )

    // Advisory: Print is not held back by what the checks found.
    await dialog.getByRole('button', { name: 'Print', exact: true }).click()
    await expect(dialog.getByTestId('queued-items')).toContainText('Queue #')
  })

  test('names a process and a slot preset in Advanced mode', async ({ page }) => {
    const dialog = await openDialog(page)
    await expect(dialog.getByLabel('Process')).toHaveCount(0)

    await dialog.getByRole('switch', { name: 'Advanced' }).click()
    await dialog.getByLabel('Process').selectOption('0.24mm Standard @BBL H2C')
    await dialog.getByLabel('Preset for slot 1').selectOption({ label: 'Bambu ABS @BBL H2C' })
    // High Flow is offered per side, and sliced as High Flow (#484): nothing says otherwise.
    await dialog.getByRole('radio', { name: 'Left High Flow' }).check()
    await expect(dialog.getByText(/slices this as Standard flow/)).toHaveCount(0)

    await dialog.getByRole('button', { name: 'Print', exact: true }).click()
    await expect(dialog.getByTestId('queued-items')).toBeVisible()
    await expect(dialog.getByText(/slices this as Standard flow/)).toHaveCount(0)
  })
})
