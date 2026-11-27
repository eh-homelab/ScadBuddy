import { expect, test } from '@playwright/test'

// #7143 — the print dialog's verdict names a tray no spool is linked to, and its button opens the
// shell's unknown-spool dialog on top of the print dialog (two modal dialogs, one inside the other).
// The mocks put the tray before the page loads (`unknownTrays.ts` `UNKNOWN_TRAY_SEED_KEY`) and the
// print check names it (`handlers.ts`).
test.skip(!!process.env.E2E_BASE_URL, 'msw-backed; the real stack has no seeded unknown tray')

test('the unknown-spool dialog stacks on the print dialog: focus, Escape and clicks', async ({ page }) => {
  await page.addInitScript(() => sessionStorage.setItem('scadbuddy.mock.unknown-tray', '1'))
  await page.goto('/m/name-keychain')
  await expect(page.getByTestId('bbox-readout')).toBeVisible()
  await page.getByTestId('generate').click()
  await expect(page.getByText(/^Saved /)).toBeVisible()
  await page.getByTestId('print').click()
  const print = page.getByRole('dialog', { name: 'Print', exact: true })
  await expect(print).toBeVisible()

  await print.getByRole('button', { name: 'Link the spool in AMS-A slot 2' }).click()
  const unknown = page.getByRole('dialog', { name: /^Unknown spool in .* AMS-A slot 2$/ })
  await expect(unknown).toBeVisible()

  // On top, and holding the focus.
  const topmost = await page.evaluate(`(() => {
    const node = [...document.querySelectorAll('[role="dialog"]')].find((one) => (one.textContent || '').startsWith('Unknown spool in'))
    if (!node) return false
    const box = node.getBoundingClientRect()
    const hit = document.elementFromPoint(box.x + box.width / 2, box.y + 20)
    return hit !== null && node.contains(hit)
  })()`)
  expect(topmost).toBe(true)
  await expect(unknown.locator(':focus')).toHaveCount(1)

  // A click inside the top dialog leaves both open.
  await unknown.getByLabel('Net weight (g)').click()
  await unknown.getByRole('heading').first().click()
  await expect(unknown).toBeVisible()
  await expect(print).toBeVisible()

  // Escape closes the top dialog only.
  await page.keyboard.press('Escape')
  await expect(unknown).toBeHidden()
  await expect(print).toBeVisible()
})
