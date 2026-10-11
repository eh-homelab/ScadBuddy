import { expect, test, type Page } from '@playwright/test'

/**
 * #2166, #2169, #2164 — the maintainer's H2C (two 0.4 High Flow nozzles, the Filament
 * Track Switch) printing a two-colour library file in Mistletoe Green and Inland Black
 * PLA, with the black in an untagged tray Bambuddy has no spool for (`src/mocks/h2c.ts`).
 */
test.describe('print dialog on the H2C', () => {
  test.skip(!!process.env.E2E_BASE_URL, 'msw-backed; the real stack is covered by real-backend.spec.ts')

  async function openDialog(page: Page) {
    await page.goto('/library')
    await page.getByRole('button', { name: /^MakerWorld/ }).click()
    await page.getByTestId('library-print-2182').click()
    const dialog = page.getByRole('dialog', { name: 'Print' })
    await expect(dialog.getByTestId('nozzle-plan')).toBeVisible()
    return dialog
  }

  test('basic mode: one flow, the plan on both nozzles, no sides, and a background slice', async ({ page }) => {
    const dialog = await openDialog(page)
    const flow = dialog.getByRole('radiogroup', { name: 'Nozzle' })
    await expect(flow.getByRole('radio')).toHaveText(['High Flow', 'Standard'])
    await expect(dialog.getByTestId('flow-high_flow')).toHaveAttribute('aria-checked', 'true')
    await expect(dialog.getByTestId('nozzle-plan')).toContainText('Mistletoe Green → right')
    await expect(dialog.getByTestId('nozzle-plan')).toContainText('Inland Black → left')
    await expect(dialog.getByTestId('nozzle-plan')).toContainText('0.4 High Flow')
    // The switch feeds any spool to either nozzle: no L/R badges.
    await expect(dialog.locator('[data-testid^="side-"], [data-testid^="slot-side-"]')).toHaveCount(0)

    const pane = dialog.getByTestId('preview-pane')
    await expect(pane.getByTestId('slice-time')).toHaveText('47m', { timeout: 15_000 })
    await expect(pane.getByTestId('slice-changes')).toHaveText('1')
    await expect(pane.getByTestId('preview-side-1')).toHaveText('right nozzle')

    // Standard reslices: the old result is marked until the new one lands.
    await dialog.getByTestId('flow-standard').click()
    await expect(pane.getByTestId('slice-status')).toBeVisible()
    await expect(pane.getByTestId('slice-time')).toHaveText('1h 6m', { timeout: 15_000 })
    await expect(dialog.getByTestId('nozzle-plan')).toContainText('0.4 Standard')
  })

  test('advanced mode: a filament put on a side by hand is planned and resliced', async ({ page }) => {
    const dialog = await openDialog(page)
    await dialog.getByRole('switch', { name: 'Advanced' }).click()
    const step = dialog.getByTestId('nozzle-plan-step')
    await expect(step.getByLabel(/Slot 1: Mistletoe Green/)).toHaveValue('')
    await step.getByLabel(/Slot 1: Mistletoe Green/).selectOption('L')
    await step.getByLabel(/Slot 2: Inland Black/).selectOption('L')
    await expect(dialog.getByTestId('nozzle-plan')).toContainText('Mistletoe Green → left')
    await expect(dialog.getByTestId('slice-changes')).toHaveText('24', { timeout: 15_000 })
  })

  test('an untagged tray: no offers the tray itself, yes records the spool', async ({ page }) => {
    const dialog = await openDialog(page)
    const question = dialog.getByTestId('tray-question-3-3')
    await expect(question).toContainText("AMS-D slot 4 has black PLA, but Bambuddy doesn't know which spool it is.")
    await expect(question).toContainText('Inland Black')
    await expect(question).toContainText('about half full, 515 g')
    await expect(question).toContainText('kept in Shelf B')
    await expect(question).toContainText('spool #16')

    await question.getByTestId('tray-decline').click()
    await expect(question).toHaveCount(0)
    await dialog.getByTestId('change-slot-2').click()
    await expect(dialog.getByTestId('spool--52')).toBeVisible()
    await page.keyboard.press('Escape')

    // Remembered: the question is not asked again for the same tray.
    const again = await openDialog(page)
    await expect(again.getByTestId('tray-question-3-3')).toHaveCount(0)
  })

  test('yes records the spool and the question goes', async ({ page }) => {
    const dialog = await openDialog(page)
    const question = dialog.getByTestId('tray-question-3-3')
    const assigned = page.waitForRequest((request) => request.method() === 'POST' && request.url().endsWith('/trays/3/3/spool'))
    await question.getByRole('button', { name: 'Yes, this one' }).click()
    expect((await assigned).postDataJSON()).toEqual({ spool_id: 16 })
    await expect(question).toHaveCount(0)
  })

  test('fits a phone, with the preview below the choices', async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 })
    const dialog = await openDialog(page)
    const pane = dialog.getByTestId('preview-pane')
    await pane.scrollIntoViewIfNeeded()
    await expect(pane.getByTestId('slice-time')).toBeVisible({ timeout: 15_000 })
    const flow = await dialog.getByTestId('flow-high_flow').boundingBox()
    const box = await pane.boundingBox()
    expect(box && flow && box.y > flow.y).toBe(true)
    expect(await page.evaluate('document.documentElement.scrollWidth')).toBeLessThanOrEqual(390)
  })

  // #2169 — a plate clicked loads into the pane; one chosen to print does too.
  test('a plate clicked is the one the preview shows', async ({ page }) => {
    await page.goto('/library')
    await page.getByRole('button', { name: /^MakerWorld/ }).click()
    await page.getByTestId('library-print-67').click()
    const dialog = page.getByRole('dialog', { name: 'Print' })
    const label = dialog.getByTestId('preview-label')
    await expect(label).toHaveText('Showing Plate 1')
    const plates = dialog.getByTestId('plate-choice')
    const mesh = page.waitForRequest((request) => request.url().includes('/print/library/67/preview.glb?plate=2'))
    await plates.getByTestId('preview-plate-2').click()
    await mesh
    await expect(label).toHaveText('Showing Plate 2')
    await expect(plates.getByRole('radio', { name: /Plate 1/ })).toBeChecked()
    await plates.getByRole('radio', { name: 'All plates' }).check()
    await expect(label).toHaveText('Showing Plate 1')
    await plates.getByRole('radio', { name: /Plate 2/ }).check()
    await expect(label).toHaveText('Showing Plate 2')
  })
})
