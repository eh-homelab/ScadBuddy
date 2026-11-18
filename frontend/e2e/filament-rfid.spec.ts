import { expect, test } from '@playwright/test'
import { spoolId } from '../src/mocks/features/filament/seed'

// #6264 — the RFID parts of the Filament section against the msw API
// (`src/mocks/features/filament/rfid.ts`): a Bambu tag bound with override, an NTAG write that
// confirms, and the label of a spool.
test.skip(!!process.env.E2E_BASE_URL, 'msw-backed; the real stack is covered by real-backend.spec.ts')

const spoolUrl = (number: number) => `/filament/spools/${spoolId(number)}`

test('binds a Bambu tag with override and shows the override mark and the weight lock', async ({ page }) => {
  await page.goto(spoolUrl(2))
  const panel = page.getByTestId('tag-panel')
  await panel.getByLabel('Tag UID').fill('04AABBCC')
  const box = panel.getByRole('checkbox', { name: 'This Bambu tag belongs to a different filament (override)' })
  await expect(box).toBeDisabled()
  await panel.getByLabel('Tray UUID').fill('0123456789ABCDEF0123456789ABCDEF')
  await box.click()
  await panel.getByRole('button', { name: 'Bind tag' }).click()
  await expect(panel.getByText('Override', { exact: true })).toBeVisible()
  await expect(panel.getByText('Weight locked: the AMS percentage never changes grams left')).toBeVisible()
})

test('writes an NTAG and says it is written and bound', async ({ page }) => {
  await page.goto(spoolUrl(7))
  const panel = page.getByTestId('ntag-panel')
  await expect(panel.getByRole('combobox', { name: 'Reader' })).toHaveText(/spoolbuddy-bench/)
  await panel.getByRole('button', { name: 'Write NTAG' }).click()
  await expect(panel.getByText('Hold a blank tag to the reader')).toBeVisible()
  await expect(panel.getByText('Written and bound')).toBeVisible({ timeout: 10_000 })
})

test('the labels page shows a #N label with a code', async ({ page }) => {
  await page.goto(`/filament/labels?ids=${[1, 2].map(spoolId).join(',')}`)
  const label = page.getByTestId('label').first()
  await expect(label).toContainText('#1')
  await expect(label.locator('svg').first()).toBeVisible()
})
