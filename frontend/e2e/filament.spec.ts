import { expect, test } from '@playwright/test'
import { spoolId, T } from '../src/mocks/features/filament/seed'
import { SUPPLIER } from '../src/mocks/features/filamentPurchasingSeed'

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

test.describe('suppliers (#6266)', () => {
  test('the Suppliers tab opens a supplier whose price sheet shows the rise', async ({ page }) => {
    await page.goto('/filament/suppliers')
    await page.getByRole('table', { name: 'Suppliers' }).getByRole('link', { name: 'Bambu Lab' }).click()
    await expect(page).toHaveURL(new RegExp(`/filament/suppliers/${SUPPLIER.BAMBU}$`))
    await expect(page.getByRole('heading', { level: 1, name: 'Bambu Lab' })).toBeVisible()
    await expect(page.getByRole('table', { name: 'Price sheet' }).getByText('+8.2 %')).toBeVisible()
  })

  test.describe('at 390 px', () => {
    test.use({ viewport: { width: 390, height: 844 } })

    test('the supplier page has no horizontal scroll', async ({ page }) => {
      await page.goto(`/filament/suppliers/${SUPPLIER.BAMBU}`)
      await expect(page.getByRole('heading', { level: 1, name: 'Bambu Lab' })).toBeVisible()
      const [scroll, client] = (await page.evaluate('[document.documentElement.scrollWidth, document.documentElement.clientWidth]')) as [number, number]
      expect(scroll).toBeLessThanOrEqual(client)
    })
  })
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

// #6264 — the print rules of /filament/labels, through Chromium's own PDF: a roll prints one
// page per label, a sheet packs three labels on one page.
const pages = (pdf: Buffer) => (pdf.toString('latin1').match(/\/Type\s*\/Page[^s]/g) ?? []).length

// #6625 — a type's 3DFP id, then "Fetch from 3DFP": the mock has no worker, so the run ends
// `imported` on the panel's next read, 10 s after the start, with one preset per nozzle.
test('sets a 3DFP id, fetches the presets, and shows the run imported with four nozzle rows', async ({ page }) => {
  await page.goto(`/filament/types/${T.JADE}`)
  const fetchButton = page.getByRole('button', { name: 'Fetch from 3DFP' })
  await expect(fetchButton).toBeDisabled()
  await page.getByLabel('3DFP id').fill('1234')
  await expect(page.getByRole('link', { name: 'Open on 3DFP' })).toHaveAttribute('href', 'https://3dfilamentprofiles.com/filament/details/1234')
  await page.getByRole('button', { name: 'Save type' }).click()
  await expect(page.getByRole('status').filter({ hasText: 'Saved' })).toBeVisible()
  await expect(fetchButton).toBeEnabled()

  await fetchButton.click()
  const last = page.getByRole('status', { name: 'Last fetch' })
  await expect(last).toContainText('Running')
  await expect(last).toContainText('Imported', { timeout: 20_000 })
  const presets = page.getByRole('table', { name: 'Slicer presets' })
  await expect(presets.getByRole('row', { name: /^Preset H2C / })).toHaveCount(4)
  for (const nozzle of ['0.2', '0.4', '0.6', '0.8']) await expect(presets.getByRole('row', { name: `Preset H2C ${nozzle}`, exact: true })).toBeVisible()
})

test('the labels page prints one roll page per label, and a sheet on one page', async ({ page }) => {
  await page.goto(`/filament/labels?ids=${[1, 2, 3].map(spoolId).join(',')}`)
  await expect(page.getByTestId('label')).toHaveCount(3)
  expect(pages(await page.pdf({ preferCSSPageSize: true }))).toBe(3)

  await page.getByRole('radio', { name: 'Sheet' }).click()
  expect(pages(await page.pdf({ preferCSSPageSize: true }))).toBe(1)
})

// #6701 — "Create types" on the Needs-type filter, at a phone's width: the proposed type is
// renamed and created, the spool leaves the filter, and the page never scrolls sideways.
test.describe('the Create types panel at 390 px', () => {
  test.use({ viewport: { width: 390, height: 844 } })

  test('creates a proposed type and types its spool', async ({ page }) => {
    await page.goto('/filament/spools?needs_type=true')
    const panel = page.getByRole('region', { name: 'Create types' })
    const row = panel.getByRole('listitem', { name: 'PLA-TEAL' })
    await expect(row).toContainText('1 spool: #6')
    await expect(panel.getByRole('list', { name: 'Not enough to propose a type' })).toContainText('#7 needs a label weight')
    const [scroll, client] = (await page.evaluate('[document.documentElement.scrollWidth, document.documentElement.clientWidth]')) as [number, number]
    expect(scroll).toBeLessThanOrEqual(client)

    await row.getByRole('textbox', { name: 'Type name' }).fill('Teal PLA')
    await row.getByRole('button', { name: 'Create' }).click()
    await expect(page.getByText('Created Teal PLA for 1 spool.')).toBeVisible()
    await expect(panel.getByRole('listitem', { name: 'PLA-TEAL' })).toHaveCount(0)
    await expect(page.getByRole('link', { name: '#6', exact: true })).toHaveCount(0)
    await expect(page.getByRole('link', { name: '#7', exact: true }).first()).toBeVisible()
  })
})

// #6704 — the spool page scrolls in its own container only; the document must not scroll too.
for (const size of [
  { width: 1440, height: 900 },
  { width: 390, height: 844 },
]) {
  test(`the spool page scrolls once, not the document, at ${size.width}x${size.height}`, async ({ page }) => {
    await page.setViewportSize(size)
    await page.goto(spoolUrl(1))
    await expect(page.getByRole('heading', { level: 1 })).toBeVisible()
    const sizes = await page.evaluate<{ scrollHeight: number; clientHeight: number }>(
      '({ scrollHeight: document.scrollingElement.scrollHeight, clientHeight: document.scrollingElement.clientHeight })',
    )
    expect(sizes.scrollHeight).toBeLessThanOrEqual(sizes.clientHeight)
  })
}
