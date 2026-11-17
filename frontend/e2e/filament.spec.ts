import { expect, test } from '@playwright/test'
import { spoolId } from '../src/mocks/features/filament/seed'

// #6262 — the Filament section against the msw API (`src/mocks/features/filament/`): the
// spools list and its filter, the CSV import, the spool page's colour presets and "Reset to
// type", the restore of an archived spool with its tag, and the spool page at a phone's width.
test.skip(!!process.env.E2E_BASE_URL, 'msw-backed; the real stack is covered by real-backend.spec.ts')

const spoolUrl = (number: number) => `/filament/spools/${spoolId(number)}`

test('/filament opens the Spools tab, and the low stock tile filters it', async ({ page }) => {
  await page.goto('/filament')
  await expect(page).toHaveURL(/\/filament\/spools/)
  await expect(page.getByRole('heading', { level: 1 })).toBeVisible()
  const table = page.getByRole('table', { name: 'Spools' })
  await expect(table.getByRole('link', { name: '#1', exact: true })).toBeVisible()
  await expect(table.getByRole('link', { name: '#4', exact: true })).toBeVisible()

  await page.getByRole('group', { name: 'Inventory' }).getByRole('button', { name: /Low stock/ }).click()
  await expect(page.getByRole('list', { name: 'Filters' })).toContainText('Low stock')
  await expect(table.getByRole('link', { name: '#4', exact: true })).toBeVisible()
  await expect(table.getByRole('link', { name: '#1', exact: true })).toHaveCount(0)
})

test('imports a CSV: one bad line keeps Import off until the file is fixed', async ({ page }) => {
  await page.goto('/filament/spools')
  await page.getByRole('button', { name: 'Import CSV' }).click()
  const dialog = page.getByRole('dialog', { name: 'Import spools' })
  const file = dialog.locator('input[type="file"]')

  await file.setInputFiles({
    name: 'spools.csv',
    mimeType: 'text/csv',
    buffer: Buffer.from('type,note\nBL-PLA-BASIC-JADE-WHITE,from a csv\nNO-SUCH-TYPE,from a csv\n'),
  })
  const preview = dialog.getByRole('table', { name: 'Import preview' })
  await expect(preview.getByRole('row', { name: 'Line 3' })).toContainText("no spool type with the code 'NO-SUCH-TYPE'")
  await expect(dialog).toContainText('One line has an error')
  await expect(dialog.getByRole('button', { name: /^Import/ })).toBeDisabled()

  await file.setInputFiles({
    name: 'spools.csv',
    mimeType: 'text/csv',
    buffer: Buffer.from('type,note\nBL-PLA-BASIC-JADE-WHITE,from a csv\nBL-PLA-BASIC-BLACK,from a csv\n'),
  })
  const write = dialog.getByRole('button', { name: 'Import 2 spools' })
  await expect(write).toBeEnabled()
  await write.click()
  await expect(dialog).toHaveCount(0)
  await expect(page.getByText('Imported: 2 added, 0 changed.')).toBeVisible()
})

test('sets a translucent spool to Clear, saves it, and resets its colour to the type', async ({ page }) => {
  await page.goto(spoolUrl(2))
  await expect(page.getByRole('heading', { level: 1, name: 'Spool #2' })).toBeVisible()
  const colour = page.getByRole('group', { name: 'Colour' })
  await expect(colour.getByRole('button', { name: 'Translucent' })).toHaveAttribute('aria-pressed', 'true')

  await colour.getByRole('button', { name: 'Clear' }).click()
  await expect(colour.getByRole('button', { name: 'Clear' })).toHaveAttribute('aria-pressed', 'true')
  await page.getByRole('button', { name: 'Save spool' }).click()
  await expect(page.getByRole('status').filter({ hasText: 'Saved' })).toBeVisible()
  await expect(page.getByText('Set on this spool')).toBeVisible()
  await expect(colour.getByRole('button', { name: 'Clear' })).toHaveAttribute('aria-pressed', 'true')

  await page.getByRole('button', { name: 'Reset Colour to type' }).click()
  await expect(page.getByText('Set on this spool')).toHaveCount(0)
  await expect(colour.getByRole('button', { name: 'Translucent' })).toHaveAttribute('aria-pressed', 'true')
  await expect(colour.getByRole('button', { name: 'Clear' })).toHaveAttribute('aria-pressed', 'false')
})

test('restores the archived spool and moves its old tag back from the spool that holds it', async ({ page }) => {
  await page.goto(spoolUrl(19))
  await expect(page.getByRole('heading', { level: 1, name: 'Spool #19' })).toBeVisible()
  await page.getByRole('button', { name: 'Restore' }).click()
  await expect(page.getByRole('button', { name: 'Restore' })).toHaveCount(0)

  const tag = page.getByTestId('tag-panel')
  await expect(tag).toContainText('No tag is on this spool.')
  await tag.getByRole('button', { name: 'Tag is on spool #3 — Move it here' }).click()
  await expect(tag.getByRole('button', { name: 'Take off' })).toBeVisible()
  await expect(tag).toContainText('04A1B2C3D4E5F6')
  await expect(tag).not.toContainText('No tag is on this spool.')
})

test.describe('the spool page at 390 px', () => {
  test.use({ viewport: { width: 390, height: 844 } })

  test('has no horizontal scroll, and the section select jumps to its section', async ({ page }) => {
    await page.goto(spoolUrl(2))
    await expect(page.getByRole('heading', { level: 1, name: 'Spool #2' })).toBeVisible()
    const widths = async () =>
      (await page.evaluate('[document.documentElement.scrollWidth, document.documentElement.clientWidth]')) as [number, number]
    const [scroll, client] = await widths()
    expect(scroll).toBeLessThanOrEqual(client)

    await page.getByRole('combobox', { name: 'Go to section' }).click()
    await page.getByRole('option', { name: 'Colour and cost' }).click()
    await expect(page).toHaveURL(/#colour-and-cost$/)
    await expect(page.locator('#colour-and-cost')).toBeInViewport()
    const [after, clientAfter] = await widths()
    expect(after).toBeLessThanOrEqual(clientAfter)
  })
})
