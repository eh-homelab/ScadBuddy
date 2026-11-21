import { expect, test } from '@playwright/test'
import { spoolId } from '../src/mocks/features/filament/seed'

// #6268 — the AMS weights sync, a place's sensors and their history, and "Needs drying",
// against the msw API (`src/mocks/features/filament/`).
test.skip(!!process.env.E2E_BASE_URL, 'msw-backed; the real stack is covered by real-backend.spec.ts')

test('Sync weights from AMS says what it did and what it skipped', async ({ page }) => {
  await page.goto('/filament/spools')
  await expect(page.getByRole('table', { name: 'Spools' })).toBeVisible()
  await page.getByRole('button', { name: 'Sync weights from AMS' }).click()
  const toasts = page.getByTestId('toasts')
  await expect(toasts.getByText('Synced 3 spools', { exact: true })).toBeVisible()
  const detail = toasts.getByText('Skipped 1 locked, 2 on a printing printer.', { exact: true })
  await expect(detail).toBeVisible()
  expect(await detail.evaluate((node) => node.scrollHeight <= node.clientHeight + 1)).toBe(true)
  await expect(page.getByRole('button', { name: 'Sync weights from AMS' })).toBeEnabled()
  if (process.env.E2E_SHOTS) await page.screenshot({ path: `${process.env.E2E_SHOTS}/spools-sync.png` })
})

test("a place's sensor shows its value, the limit it crossed, and a day of history", async ({ page }) => {
  // The mock's readings end at the sensor's `last_at`; the component keeps the last 24 h from now.
  await page.clock.setFixedTime(new Date('2026-11-18T09:45:00Z'))
  await page.goto('/filament/locations')
  const sensor = page.getByRole('listitem', { name: 'Shelf A humidity' })
  await expect(sensor).toContainText('62 %')
  await expect(sensor).toContainText('Above the limit')
  // #6646: the alert went out through Bambuddy.
  await expect(sensor).toContainText('Notified through Bambuddy (alert started)')
  await sensor.getByRole('button', { name: 'Show history of Shelf A humidity' }).click()
  await expect(sensor.getByRole('img', { name: /Shelf A humidity: .* %, 96 readings/ })).toBeVisible()
  await expect(sensor.locator('[data-limit="above"]')).toHaveCount(1)
  if (process.env.E2E_SHOTS) await page.screenshot({ path: `${process.env.E2E_SHOTS}/locations-sensors.png`, fullPage: true })
})

test('adds a Home Assistant sensor from the entity list', async ({ page }) => {
  await page.goto('/filament/locations')
  await page.getByRole('button', { name: 'Add a sensor to Workshop' }).click()
  await page.getByLabel('Search Home Assistant').fill('garage')
  await page.getByRole('button', { name: /Garage humidity/ }).click()
  await page.getByRole('button', { name: 'Save sensor' }).click()
  await expect(page.getByRole('listitem', { name: 'Garage humidity' })).toBeVisible()
})

test('the spool that has been out of dry storage too long is badged, in the list and on its page', async ({ page }) => {
  await page.goto('/filament/spools')
  const row = page.getByRole('table', { name: 'Spools' }).getByRole('row').filter({ has: page.getByRole('link', { name: '#2', exact: true }) })
  await expect(row.getByText('Needs drying')).toBeVisible()
  await page.goto(`/filament/spools/${spoolId(2)}`)
  await expect(page.getByText('61.5 hours outside dry storage')).toBeVisible()
  await expect(page.getByRole('heading', { level: 1 }).locator('..').getByText('Needs drying')).toBeVisible()
  if (process.env.E2E_SHOTS) await page.screenshot({ path: `${process.env.E2E_SHOTS}/spool-needs-drying.png` })
})
