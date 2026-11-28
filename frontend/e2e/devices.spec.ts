import { expect, test } from '@playwright/test'

// #7905 — the Devices page and "Use my phone" on the Scan page, over the mocked core routes
// (`src/mocks/features/devices.ts`): the mock's online phone plays an `nfc.read` script on the
// mock realtime socket, and the page shows each event as the socket gives it.
test.skip(!!process.env.E2E_BASE_URL, 'msw-backed; the real stack has no paired phone')

test('the Devices page lists the phones and makes a pairing link', async ({ page }) => {
  await page.goto('/devices')
  await expect(page.getByRole('heading', { name: 'Devices', level: 1 })).toBeVisible()
  const list = page.getByRole('list', { name: 'Paired devices' })
  await expect(list.getByText('Online', { exact: true })).toBeVisible()
  await expect(list.getByText(/^Offline, last seen /)).toBeVisible()
  await page.getByRole('button', { name: 'Add a device' }).click()
  const pairing = page.getByRole('region', { name: 'Pair a device' })
  await expect(pairing.getByRole('img', { name: 'Pairing QR code' })).toBeVisible()
  await expect(pairing.getByRole('link', { name: 'Open the pairing link' })).toHaveAttribute('href', /^scadbuddy:\/\/pair\?url=/)
})

test('"Use my phone" on the Scan page shows a tag read as it happens', async ({ page }) => {
  await page.goto('/scan')
  const block = page.getByRole('region', { name: 'Use my phone' })
  await block.getByRole('button', { name: 'Read a tag' }).click()
  const steps = block.getByRole('list', { name: 'What the phone did' })
  await expect(steps.getByRole('listitem').first()).toHaveText('Found tag 9CC69CE3, 48 blocks.')
  await expect(steps.getByRole('listitem').last()).toHaveText('Done.', { timeout: 10_000 })
  await expect(steps.getByRole('listitem')).toHaveCount(7)
  await expect(block.getByRole('progressbar', { name: 'Blocks read' })).toHaveAttribute('aria-valuenow', '48')
})
