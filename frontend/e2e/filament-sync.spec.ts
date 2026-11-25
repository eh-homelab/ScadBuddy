import { expect, test } from '@playwright/test'

// #6265 — the Bambuddy sync's directions in Settings → Filament against the msw API
// (`src/mocks/features/settings.ts` keeps the saved `filament_sync` across a reload, as the
// server's row would; `src/mocks/features/filament/sync.ts` is the status).
test.skip(!!process.env.E2E_BASE_URL, 'msw-backed; the real stack is covered by real-backend.spec.ts')

test('a saved sync direction is remembered after a reload', async ({ page }) => {
  await page.goto('/settings/printing')
  const filament = page.getByRole('region', { name: 'Filament' })
  const suppliers = filament.getByRole('combobox', { name: 'Suppliers and prices' })
  await expect(suppliers).toHaveText('Off')
  // The seed sets nothing to push; once the save lands, the status is read again and says nothing.
  await expect(filament.getByText('No pass runs: no data type but tags is set to send to Bambuddy (tags go with the refresh).')).toBeVisible()
  await suppliers.click()
  await page.getByRole('option', { name: 'ScadBuddy → Bambuddy' }).click()
  await filament.getByRole('button', { name: 'Save Filament' }).click()
  await expect(filament.getByText('No pass runs: no data type but tags is set to send to Bambuddy (tags go with the refresh).')).toHaveCount(0)

  await page.reload()
  await expect(filament.getByRole('combobox', { name: 'Suppliers and prices' })).toHaveText('ScadBuddy → Bambuddy')
  await expect(filament.getByRole('combobox', { name: 'Weights' })).toHaveText('Bambuddy → ScadBuddy')
})

test('sync now starts a pass and lists the item Bambuddy refused', async ({ page }) => {
  await page.goto('/settings/printing')
  const filament = page.getByRole('region', { name: 'Filament' })
  const failed = filament.getByRole('list', { name: 'Items not synced' })
  await expect(failed.getByRole('listitem').first()).toContainText('Bambuddy refused it (HTTP 422).')
  await filament.getByRole('button', { name: 'Sync now' }).click()
  await expect(filament.getByRole('alert')).toContainText('Nothing is set to go to Bambuddy.')

  // Tags alone are not enough: they go with the refresh, not the sync pass.
  await filament.getByRole('combobox', { name: 'Suppliers and prices' }).click()
  await page.getByRole('option', { name: 'ScadBuddy → Bambuddy' }).click()
  await filament.getByRole('button', { name: 'Save Filament' }).click()
  await filament.getByRole('button', { name: 'Sync now' }).click()
  await expect(filament.getByText(/Sync started/)).toBeVisible()
  // The page reads the status every 3 s; the mock's pass lands on the second read.
  await expect(filament.getByText('Sync finished.')).toBeVisible({ timeout: 15_000 })
  await expect(failed.getByRole('listitem').first()).toContainText('3 attempts')
})

// #6845 — every comparable linked spool changed in Bambuddy since the last pull: no keep, and the dialog says why.
test('the reset dialog says why there is no keep when every comparable linked spool changed since the last pull', async ({ page }) => {
  await page.addInitScript(() => {
    sessionStorage.setItem('scadbuddy.mock.filament-sync-link-other', JSON.stringify({ changed: true }))
  })
  await page.goto('/settings/printing')
  const filament = page.getByRole('region', { name: 'Filament' })
  await filament.getByRole('button', { name: 'Reset Bambuddy links' }).click()
  const dialog = page.getByRole('alertdialog', { name: 'Reset the Bambuddy links?' })
  await expect(dialog).toContainText(
    'Every linked spool (or, with no spool linked, filament preset) ScadBuddy could compare (a time on both sides) changed in Bambuddy since ScadBuddy last saw it, so ScadBuddy cannot prove this is the same Bambuddy.',
  )
  await expect(dialog.getByRole('button', { name: 'Keep links' })).toHaveCount(0)
  await expect(dialog.getByRole('button', { name: 'Reset links' })).toBeVisible()
})
