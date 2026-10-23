import { expect, test, type Page } from '@playwright/test'

/**
 * #3818 (plan 2026-10-18 Task 11, AC3 and AC6) — the hot path with no Bambuddy, msw-mocked
 * (`src/mocks/features/hotPath.ts`): render, the print dialog's native slice with its
 * progress, the trays with Bambuddy's records over them, a print to a LAN-linked H2C and
 * its card on the Prints page. The mock answers every Bambuddy-only route 409, so any 409
 * is a component still asking Bambuddy.
 */
test.describe('native hot path (msw)', () => {
  test.skip(!!process.env.E2E_BASE_URL, 'msw-backed; the real stack is real-hot-path.spec.ts')
  test.beforeEach(async ({ page }) => {
    await page.addInitScript(() => localStorage.setItem('sb-mock-hot-path', '1'))
  })

  function bambuddyCalls(page: Page): string[] {
    const calls: string[] = []
    page.on('response', (response) => {
      if (response.status() === 409) calls.push(response.url())
    })
    return calls
  }

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

  test('renders, slices with progress, prints on a LAN printer and follows it, with no Bambuddy', async ({
    page,
  }) => {
    const bambuddy = bambuddyCalls(page)
    const dialog = await openDialog(page)

    // The native slicer says how far it has got (slicer Task 14), then what it came to.
    const progress = dialog.getByTestId('slice-progress')
    await expect(progress).toHaveAttribute('value', '20')
    await expect(progress).toHaveAttribute('value', '60')
    await expect(dialog.getByTestId('slice-time')).toHaveText('30m')

    // The trays, with Bambuddy's record of the spool in the tagged one over it (D1).
    await expect(dialog.getByTestId('filament-slot-1')).toContainText('PLA blue in AMS-A slot 1')
    await expect(dialog.getByTestId('filament-slot-1')).toContainText('81%')
    await expect(dialog.getByTestId('filament-slot-1')).toContainText('Bambuddy: Bambu Lab Blue · 812 g')
    // Projects are Bambuddy's (D6): none is named, asked for or offered.
    await expect(dialog.getByText(/project/i)).toHaveCount(0)
    await expect(dialog.getByText('Filed under:')).toContainText('the template’s folder')

    // The untagged tray's question links Bambuddy's unlinked record through ScadBuddy's route.
    const question = dialog.getByTestId('tray-question-0-1')
    await expect(question).toContainText('AMS-A slot 2 has pink PLA, but no spool record is linked to it.')
    const link = page.waitForRequest(
      (request) => request.method() === 'PUT' && /\/printers\/[^/]+\/trays\/0\/1\/spool$/.test(request.url()),
    )
    await question.getByRole('button', { name: 'Yes, that one' }).click()
    expect((await link).postDataJSON()).toEqual({ provider: 'bambuddy', record_id: '27' })
    await expect(question).toHaveCount(0)
    await expect(dialog.getByTestId('filament-slot-2')).toContainText('Bambuddy: Elegoo Pink · 640 g')

    await dialog.getByRole('button', { name: 'Print', exact: true }).click()
    await expect(dialog.getByTestId('queued-items')).toContainText('Sliced and sent to H2C over the LAN')
    await expect(dialog.getByRole('button', { name: 'Open in queue' })).toHaveCount(0)
    await dialog.getByRole('button', { name: 'Done' }).click()

    // In-app, so the mock keeps the run: a full navigation would start it again.
    await page.getByRole('navigation', { name: 'Main' }).getByRole('link', { name: 'Prints' }).click()
    const now = page.getByTestId('now-printing')
    await expect(now.getByText('H2C', { exact: true })).toBeVisible()
    await expect(now).toContainText('LAN')
    // The LAN watch's reports follow it on the `printers` topic: 30 %, 80 %, then done.
    await expect(now).toContainText('30%')
    await expect(now).toContainText('80%')
    await expect(now).toContainText(/finished/i, { timeout: 15_000 })
    expect(bambuddy).toEqual([])
  })

  test('a stale overlay shows the trays with a note, and still prints', async ({ page }) => {
    await page.addInitScript(() => localStorage.setItem('sb-mock-hot-path-stale', '1'))
    const bambuddy = bambuddyCalls(page)
    const dialog = await openDialog(page)

    await expect(dialog.getByTestId('overlay-notes')).toContainText(
      "Bambuddy's spool records are from 13:40 UTC; showing the trays only.",
    )
    await expect(dialog.getByTestId('filament-slot-1')).toContainText('PLA blue in AMS-A slot 1')
    await expect(dialog.getByTestId('filament-slot-1')).not.toContainText('Bambuddy:')
    // Nothing to link while the records are stale: no question.
    await expect(dialog.getByTestId('tray-question-0-1')).toHaveCount(0)

    // Three reads two seconds apart (`RESLICE_POLL_MS`) before it lands.
    await expect(dialog.getByTestId('slice-time')).toHaveText('30m', { timeout: 15_000 })
    await dialog.getByRole('button', { name: 'Print', exact: true }).click()
    await expect(dialog.getByTestId('queued-items')).toContainText('Sliced and sent to H2C over the LAN')
    expect(bambuddy).toEqual([])
  })
})
