import { expect, test } from '@playwright/test'

test.describe('print picker', () => {
  // These drive the msw worker; the real stack is covered by real-backend.spec.ts.
  test.skip(
    !!process.env.E2E_BASE_URL,
    'msw-backed; the real stack is covered by real-backend.spec.ts',
  )

  test('picks a pipeline, runs it and reports the queue entry', async ({ page }) => {
    await page.goto('/m/name-keychain')
    await expect(page.getByTestId('bbox-readout')).toBeVisible()

    await page.getByTestId('generate').click()
    await expect(page.getByText(/^Saved /)).toBeVisible()

    await page.getByTestId('print').click()

    // The pipelines arrive first, then their eligibility — which uploads the 3MF, because
    // Bambuddy judges a library file rather than a plate in the browser.
    const dialog = page.getByRole('dialog', { name: 'Print' })
    await expect(dialog.getByRole('radio', { name: /Textured PEI · 0\.20 mm/ })).toBeChecked()
    await expect(dialog.getByText('ready').first()).toBeVisible()
    // The pipeline that is not ready lists Bambuddy's own reasons, per slot.
    await expect(dialog.getByText(/filament type mismatch \(slot 1\)/)).toBeVisible()

    await page.getByTestId('run-pipeline').click()

    await expect(dialog.getByText(/Pipeline run/)).toBeVisible()
    await expect(dialog.getByTestId('run-jobs')).toContainText('3DP-31B-598')
  })

  test('offers force only after the issues have been shown, and overrides with it', async ({
    page,
  }) => {
    await page.goto('/m/name-keychain')
    await expect(page.getByTestId('bbox-readout')).toBeVisible()
    await page.getByTestId('generate').click()
    await page.getByTestId('print').click()

    const dialog = page.getByRole('dialog', { name: 'Print' })
    // The preselected pipeline is ready, so there is nothing to override yet.
    await expect(dialog.getByRole('radio', { name: /Textured PEI · 0\.20 mm/ })).toBeChecked()
    await expect(page.getByTestId('force')).toBeHidden()

    await dialog.getByRole('radio', { name: /Draft · 0\.28 mm/ }).click()
    await expect(page.getByTestId('force')).toBeVisible()

    await page.getByTestId('force').check()
    await page.getByTestId('run-pipeline').click()

    await expect(dialog.getByText(/eligibility check overridden/)).toBeVisible()
  })

  test('builds a pipeline from presets when a new one is needed', async ({ page }) => {
    await page.goto('/m/name-keychain')
    await expect(page.getByTestId('bbox-readout')).toBeVisible()
    await page.getByTestId('generate').click()
    await page.getByTestId('print').click()
    await page.getByTestId('new-pipeline').click()

    const dialog = page.getByRole('dialog', { name: 'Print' })
    // Process and filament presets arrive only once a printer preset is named.
    await expect(dialog.getByLabel('Process preset')).toBeDisabled()
    await dialog.getByLabel('Printer preset').selectOption('cloud:GM041')
    await expect(dialog.getByLabel('Process preset')).toBeEnabled()

    // The nozzle diameter is part of the process preset's name — 0.4 here, so the
    // 0.2-nozzle preset is not on offer.
    await dialog.getByLabel('Process preset').selectOption({ label: '0.20mm Standard @BBL H2C' })
    await dialog.getByLabel('Filament for slot 1').selectOption('cloud:GFSA05_22')
    await dialog.getByLabel('Filament for slot 2').selectOption('local:2')
    await dialog.getByLabel('Bed type').selectOption('Textured PEI Plate')
    await dialog.getByRole('button', { name: 'Create pipeline' }).click()

    const created = dialog.getByRole('radio', {
      name: /Bambu Lab H2C 0\.4 nozzle · 0\.20mm Standard/,
    })
    await expect(created).toBeChecked()

    await page.getByLabel(/Always use this pipeline/).check()
    await page.getByTestId('run-pipeline').click()
    await expect(dialog.getByText(/Pipeline run/)).toBeVisible()
  })

  test('picks the filaments per slot and queues the print for one printer', async ({ page }) => {
    await page.goto('/m/name-keychain')
    await expect(page.getByTestId('bbox-readout')).toBeVisible()
    await page.getByTestId('generate').click()
    await expect(page.getByText(/^Saved /)).toBeVisible()
    await page.getByTestId('print').click()

    const dialog = page.getByRole('dialog', { name: 'Print' })
    // Both slots arrive pre-selected: the server auto-matches the plate's colours against
    // the inventory, so the common case is a read rather than four clicks.
    const slotOne = dialog.getByTestId('filament-slot-1')
    const slotTwo = dialog.getByTestId('filament-slot-2')
    await expect(slotOne.getByTestId('spool-21')).toBeChecked()
    await expect(slotTwo.getByTestId('spool-27')).toBeChecked()

    // The suggestion for slot 2 is an exact colour match that is on a shelf, so it comes
    // with a "load this in" advisory rather than a failure.
    await expect(dialog.getByTestId('filament-warnings-2')).toContainText('Load Elegoo')

    // Swap it for the pink already in the AMS-HT.
    await slotTwo.getByTestId('spool-22').check()
    await expect(dialog.getByTestId('filament-warnings-2')).toBeHidden()

    await dialog.getByTestId('use-exact-filaments').check()
    await page.getByTestId('run-pipeline').click()

    // Pinning the printer is what a pipeline run cannot express, so the backend slices
    // and queues instead — and there is no pipeline run to report on that route.
    const queued = dialog.getByTestId('queued-items')
    await expect(queued).toContainText('Sliced and queued for 3DP-31B-598')
    await expect(queued).toContainText('Queue #')
    await expect(dialog.getByText(/Pipeline run/)).toBeHidden()
  })
})
