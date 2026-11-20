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
  await expect(filament.getByText('No pass runs: no data type is set to send to Bambuddy.')).toBeVisible()
  await suppliers.click()
  await page.getByRole('option', { name: 'ScadBuddy → Bambuddy' }).click()
  await filament.getByRole('button', { name: 'Save Filament' }).click()
  await expect(filament.getByText('No pass runs: no data type is set to send to Bambuddy.')).toHaveCount(0)

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

  await filament.getByRole('combobox', { name: 'Tags' }).click()
  await page.getByRole('option', { name: 'Both ways (newer change wins)' }).click()
  await filament.getByRole('button', { name: 'Save Filament' }).click()
  await filament.getByRole('button', { name: 'Sync now' }).click()
  await expect(filament.getByText(/Sync started/)).toBeVisible()
  await expect(failed.getByRole('listitem').first()).toContainText('3 attempts')
})
