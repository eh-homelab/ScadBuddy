import { expect, test } from '@playwright/test'

// #7167 — a spool came out of its tray, and its home dry box waits for the person to say which
// spool goes in. The mocks park S-11 before the page loads (`filament/index.ts`
// `HOME_RETURN_SEED_KEY`); the Needs-you row opens the shell's dialog, where nothing is chosen.
test.skip(!!process.env.E2E_BASE_URL, 'msw-backed; the real stack has no seeded home return')

test('the home-return prompt: name the spool, and the box spool is replaced', async ({ page }) => {
  await page.addInitScript(() => sessionStorage.setItem('scadbuddy.mock.home-return', '1'))
  await page.goto('/activity#needs-you')
  const row = page.getByTestId('needs-you-item').filter({ hasText: 'S-11 came out of its tray' })
  await row.getByRole('button', { name: 'Choose the spool…' }).click()

  const dialog = page.getByRole('dialog', { name: 'S-11 came out of 3DP-31B-598 H2C AMS-A slot 1. Which spool goes into Dry box 2?' })
  await expect(dialog).toBeVisible()
  await expect(dialog.getByRole('button', { name: 'Put in box' })).toBeDisabled()

  await dialog.getByRole('radio', { name: 'Pick by hand' }).click()
  await dialog.getByRole('searchbox', { name: 'Find a spool' }).fill('S-11')
  await dialog.getByRole('radiogroup', { name: 'Spools' }).getByRole('radio', { name: /^S-11\b/ }).click()
  await expect(dialog.getByText('S-7 comes out of Dry box 2 for good: it is archived at 0 g, and its 300 g are booked as non-print waste.')).toBeVisible()
  await dialog.getByRole('button', { name: 'Put in box' }).click()

  await expect(dialog).toBeHidden()
  await expect(row).toHaveCount(0)
})
