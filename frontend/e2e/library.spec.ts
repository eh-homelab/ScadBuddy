import { expect, test } from '@playwright/test'

test.describe('library', () => {
  test.skip(
    !!process.env.E2E_BASE_URL,
    'msw-backed; the real stack is covered by real-backend.spec.ts',
  )

  test('prints a library 3MF and reopens on its last choices', async ({ page }) => {
    await page.goto('/')
    await page.getByRole('link', { name: 'Library' }).click()
    await expect(page.getByTestId('library-file-89')).toBeVisible()
    await expect(page.getByTestId('library-file-104')).toHaveCount(0)

    await page.getByTestId('library-print-89').click()
    let dialog = page.getByRole('dialog', { name: 'Print' })
    await dialog.getByRole('switch', { name: 'Advanced' }).click()
    await dialog.getByRole('radio', { name: /0\.2 mm/ }).check()
    await dialog.getByRole('button', { name: 'Print', exact: true }).click()
    await expect(dialog.getByTestId('queued-items')).toContainText('Queue #')
    await expect(dialog.getByRole('button', { name: 'Open in queue' })).toBeVisible()
    await dialog.getByRole('button', { name: 'Done' }).click()

    await page.getByTestId('library-print-89').click()
    dialog = page.getByRole('dialog', { name: 'Print' })
    // Simple mode again (#768): the remembered size is sent unseen, and shown in Advanced.
    await dialog.getByRole('switch', { name: 'Advanced' }).click()
    await expect(dialog.getByRole('radio', { name: /0\.2 mm/ })).toBeChecked()
  })

  test('Advanced lists a sliced file without Print, and is remembered', async ({ page }) => {
    await page.goto('/library')
    await page.getByRole('switch', { name: 'Advanced' }).click()
    const sliced = page.getByTestId('library-file-104')
    await expect(sliced).toContainText('Print it from Bambuddy')
    await expect(sliced.getByRole('button', { name: 'Print' })).toHaveCount(0)

    await page.reload()
    await expect(page.getByTestId('library-file-104')).toBeVisible()
  })

  test('fits a phone width with no horizontal scroll', async ({ page }) => {
    await page.setViewportSize({ width: 375, height: 800 })
    await page.goto('/library')
    await expect(page.getByTestId('library-file-89')).toBeVisible()
    const scrollWidth = await page.evaluate('document.documentElement.scrollWidth')
    expect(scrollWidth).toBeLessThanOrEqual(375)
  })

  // #935 — two uploads of one output share a long generated name, cut at the end to the
  // common prefix, and the cards showed nothing else: no date, no size.
  test('tells identically named files apart at phone width', async ({ page }) => {
    await page.setViewportSize({ width: 375, height: 800 })
    await page.goto('/library')
    const first = page.getByTestId('library-file-89')
    const second = page.getByTestId('library-file-91')
    await expect(first).toBeVisible()
    await expect(second).toBeVisible()
    expect(await first.innerText()).not.toEqual(await second.innerText())
    // The name is cut in the middle, so the end (and the extension) stays readable.
    await expect(first.getByText('64efc.3mf', { exact: false })).toBeVisible()
    await expect(first.locator('time')).toHaveAttribute('datetime', '2026-09-26T17:05:45Z')
    await expect(first).toContainText('110 kB')
  })
})
