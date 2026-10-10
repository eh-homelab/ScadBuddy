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

  // #1723 — at phone width, Advanced on and then off again must leave every control
  // reachable: nothing past the screen's edge, and Print on screen, with the choices kept.
  test('stays usable at phone width with Advanced on, and after it is switched off', async ({ page }) => {
    const dialog = await openDialog(page)
    await page.setViewportSize({ width: 390, height: 844 })
    const fits = async (state: string) => {
      const scrollWidth = Number(await page.evaluate('document.documentElement.scrollWidth'))
      expect(scrollWidth, `${state}: nothing scrolls sideways`).toBeLessThanOrEqual(390)
      const panel = (await dialog.boundingBox())!
      expect(panel.y, `${state}: the title is on screen`).toBeGreaterThanOrEqual(0)
      expect(panel.y + panel.height, `${state}: the dialog ends on screen`).toBeLessThanOrEqual(844)
      await expect(dialog.getByRole('button', { name: 'Print', exact: true })).toBeInViewport({ ratio: 1 })
    }
    await fits('Advanced off')
    // Headless Chromium has no toolbars, so dvh and vh measure the same here: pin the unit.
    const cap = await dialog.evaluate((panel) => panel.className)
    expect(cap, 'bounded by the visible height').toContain('100dvh')
    const chosen = dialog.getByTestId('filament-slot-1').getByRole('radio', { checked: true })
    const spool = await chosen.getAttribute('value')
    expect(spool, 'a spool is chosen for slot 1').not.toBeNull()

    await dialog.getByRole('switch', { name: 'Advanced' }).click()
    await expect(dialog.getByRole('group', { name: 'Nozzles' })).toBeVisible()
    await fits('Advanced on')

    await dialog.getByRole('switch', { name: 'Advanced' }).click()
    await expect(dialog.getByRole('group', { name: 'Nozzles' })).toHaveCount(0)
    await fits('Advanced off again')
    await expect(dialog.getByTestId('filament-slot-1').getByRole('radio', { checked: true })).toHaveAttribute(
      'value',
      spool!,
    )
  })

  // #1723 — the plate in 3D: Colors by default, Layers on the switch, over the dialog; on a
  // phone it covers the screen, and closing it leaves every choice as it was.
  test('previews the plate in 3D over the dialog at phone width, and keeps the choices', async ({ page }) => {
    const dialog = await openDialog(page)
    await page.setViewportSize({ width: 390, height: 844 })
    const spool = await dialog.getByTestId('filament-slot-1').getByRole('radio', { checked: true }).getAttribute('value')
    expect(spool, 'a spool is chosen for slot 1').not.toBeNull()

    await dialog.getByTestId('plate-preview-open').click()
    const preview = page.getByRole('dialog', { name: /in 3D$/ })
    await expect(preview.getByRole('radio', { name: 'Colors' })).toHaveAttribute('aria-checked', 'true')
    const canvas = preview.getByTestId('plate-scene').locator('canvas')
    await expect(canvas).toBeVisible()
    const box = (await preview.boundingBox())!
    expect(box.x, 'the preview fits the screen').toBeGreaterThanOrEqual(0)
    expect(box.x + box.width).toBeLessThanOrEqual(390)
    expect(box.y + box.height).toBeLessThanOrEqual(844)

    await preview.getByRole('radio', { name: 'Layers' }).click()
    const slider = preview.getByRole('slider', { name: 'Layer height' })
    await expect(slider).toBeEnabled()
    await slider.fill('2')
    await expect(preview.getByText(/^Up to 2\.00 of /)).toBeVisible()

    await preview.getByRole('button', { name: 'Close' }).click()
    await expect(preview).toHaveCount(0)
    await expect(dialog.getByTestId('filament-slot-1').getByRole('radio', { checked: true })).toHaveAttribute(
      'value',
      spool!,
    )
    await expect(dialog.getByRole('button', { name: 'Print', exact: true })).toBeInViewport({ ratio: 1 })
  })

  // #944 — at phone width every slot's fieldset and spool rows ran past the screen's
  // right edge, cutting off the grams, the "rests on" badge and the "prints in" colour.
  test('keeps the slots and spool rows inside the screen at phone width', async ({ page }) => {
    // Opened at desktop width: reaching the Print button on a phone is #362's problem.
    const dialog = await openDialog(page)
    await page.setViewportSize({ width: 390, height: 844 })
    const right = 390
    for (const slot of await dialog.locator('[data-testid^="filament-slot-"]').all()) {
      const box = (await slot.boundingBox())!
      expect(box.x + box.width, 'the slot fits the screen').toBeLessThanOrEqual(right)
    }
    const printsIn = dialog.getByTestId('slot-prints-in-1')
    const printsInBox = (await printsIn.boundingBox())!
    expect(printsInBox.x + printsInBox.width, '"prints in" fits').toBeLessThanOrEqual(right)
    const rows = dialog.getByTestId('filament-slot-1').getByRole('listitem')
    for (const row of await rows.all()) {
      for (const part of await row.locator('label > *').all()) {
        const box = await part.boundingBox()
        if (!box) continue
        expect(box.x + box.width, `${await part.textContent()} fits`).toBeLessThanOrEqual(right)
      }
    }
  })
})
