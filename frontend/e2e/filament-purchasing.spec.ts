import { expect, test, type Page } from '@playwright/test'

// #6266 — suppliers, price sheets and invoices against the msw API
// (`src/mocks/features/filamentPurchasing.ts`): a supplier with a priced product, a pasted PDF
// typed in as two lines with receipt-wide tax and shipping, the confirm, and the spool it made.
test.skip(!!process.env.E2E_BASE_URL, 'msw-backed; the real stack is covered by real-backend.spec.ts')

const pdf = { name: 'acme-receipt.pdf', mimeType: 'application/pdf', buffer: Buffer.from('%PDF-1.4\n% an invoice\n') }

async function choose(page: Page, trigger: string, option: string) {
  await page.getByRole('combobox', { name: trigger, exact: true }).click()
  await page.getByRole('option', { name: option, exact: true }).click()
}

async function startDraft(page: Page) {
  // In-app navigation: a reload would reset the msw worker's in-memory data, and Acme with it.
  await page.getByRole('link', { name: 'Suppliers', exact: true }).first().click()
  await page.getByRole('navigation', { name: 'Filament' }).getByRole('link', { name: 'Invoices' }).click()
  await choose(page, 'Supplier', 'Acme')
  // A real paste: a ClipboardEvent whose DataTransfer carries the PDF, fired at the drop zone.
  // (A string: this file is type-checked without the DOM lib.)
  const file = JSON.stringify({ text: pdf.buffer.toString(), name: pdf.name, type: pdf.mimeType })
  await page.evaluate(`(() => {
    const zone = document.querySelector('[aria-label="Add an invoice file"]')
    const file = ${file}
    const transfer = new DataTransfer()
    transfer.items.add(new File([file.text], file.name, { type: file.type }))
    zone.dispatchEvent(new ClipboardEvent('paste', { clipboardData: transfer, bubbles: true, cancelable: true }))
  })()`)
  await expect(page).toHaveURL(/\/filament\/invoices\/[0-9a-f-]+$/)
  await expect(page.getByText('Draft', { exact: true }).first()).toBeVisible()
}

async function typeLine(page: Page, n: number, line: { description: string; sku?: string; quantity: string; unitPrice: string }) {
  await page.getByLabel(`Description, line ${n}`).fill(line.description)
  if (line.sku) await page.getByLabel(`SKU, line ${n}`).fill(line.sku)
  await page.getByLabel(`Quantity, line ${n}`).fill(line.quantity)
  await page.getByLabel(`Unit price, line ${n}`).fill(line.unitPrice)
  await page.getByLabel(`Unit price, line ${n}`).blur()
}

async function header(page: Page, figures: { number: string; subtotal: string; shipping: string; tax: string; total: string }) {
  for (const [label, value] of [
    ['Number', figures.number],
    ['Subtotal', figures.subtotal],
    ['Shipping', figures.shipping],
    ['Tax', figures.tax],
    ['Total', figures.total],
  ] as const) {
    await page.getByLabel(label, { exact: true }).fill(value)
    await page.getByLabel(label, { exact: true }).blur()
  }
}

async function addSupplierAndProduct(page: Page) {
  await page.goto('/filament/suppliers')
  await page.getByRole('button', { name: 'Add supplier' }).click()
  const drawer = page.getByRole('dialog', { name: 'Add supplier' })
  await drawer.getByLabel('Name').fill('Acme')
  await drawer.getByRole('button', { name: 'Add supplier' }).click()
  await page.getByRole('table', { name: 'Suppliers' }).getByRole('link', { name: 'Acme' }).click()

  await page.getByRole('button', { name: 'Add product' }).click()
  const product = page.getByRole('dialog', { name: 'Add product' })
  await product.getByRole('combobox', { name: 'Type' }).click()
  await page.getByRole('option', { name: 'BL-PLA-BASIC-JADE-WHITE', exact: true }).click()
  await product.getByLabel('SKU').fill('ACME-JADE')
  await product.getByRole('button', { name: 'Add product' }).click()

  const products = page.getByRole('table', { name: 'Products' })
  await products.getByRole('button', { name: 'Add price for ACME-JADE' }).click()
  await page.getByLabel('Price with tax for ACME-JADE').fill('20.00')
  await page.getByRole('form', { name: 'Add price for ACME-JADE' }).getByRole('button', { name: 'Add price' }).click()
  await expect(products.locator('tr[data-product]', { hasText: 'ACME-JADE' })).toContainText('20.00')
}

test('a supplier, a price, an invoice and its spools', async ({ page }) => {
  await addSupplierAndProduct(page)
  await startDraft(page)

  await page.getByRole('button', { name: 'Add line' }).click()
  await typeLine(page, 1, { description: 'Jade White PLA', sku: 'ACME-JADE', quantity: '2', unitPrice: '20.00' })
  await page.getByRole('button', { name: 'Add line' }).click()
  await typeLine(page, 2, { description: 'Hot Pink PLA', quantity: '1', unitPrice: '10.00' })
  await choose(page, 'Type, line 2', 'BL-PLA-BASIC-HOT-PINK')
  await header(page, { number: 'AC-1001', subtotal: '50.00', shipping: '5.00', tax: '4.00', total: '59.00' })

  await expect(page.getByRole('status').filter({ hasText: 'Reconciles with the lines' })).toBeVisible()
  await expect(page.getByText('Difference').locator('xpath=following-sibling::dd[1]')).toHaveText('$0.00')
  const confirm = page.getByRole('button', { name: 'Confirm', exact: true })
  await expect(confirm).toBeEnabled()
  await confirm.click()
  await page.getByRole('alertdialog').getByRole('button', { name: 'Make the spools' }).click()

  await expect(page.getByText('Confirmed', { exact: true }).first()).toBeVisible()
  await expect(page.getByLabel('Description, line 1')).toHaveCount(0)
  const lines = page.getByRole('table', { name: 'Invoice lines' })
  await expect(lines).toContainText('Jade White PLA')
  await lines.getByRole('link', { name: /^Spool #/ }).first().click()

  await expect(page).toHaveURL(/\/filament\/spools\/[0-9a-f-]+$/)
  const link = page.getByRole('link', { name: /^Invoice AC-1001/ })
  await expect(link).toBeVisible()
  // Line 1: 2 x 20.00 = 40.00, plus 3.20 of the receipt's 4.00 tax and 4.00 of its 5.00 shipping: 47.20, so 23.60 a spool.
  await expect(link.locator('xpath=..')).toContainText('line 1, $23.60 each')
})

// #6267 — "Import with the assistant": the start route (mocked) links a session to the draft and
// the assistant panel opens on it; a second visit to the draft shows it running, with Open.
test('an invoice is imported with the assistant, and a second visit shows the import running', async ({ page }) => {
  await addSupplierAndProduct(page)
  await startDraft(page)

  await page.getByRole('button', { name: 'Import with the assistant' }).click()
  const panel = page.getByRole('complementary', { name: 'Assistant' })
  await expect(panel.getByTestId('active-session-title')).toHaveText('Import invoice acme-receipt.pdf')
  await expect(page.getByText('Import running')).toBeVisible()
  await expect(page.getByRole('button', { name: 'Import with the assistant' })).toHaveCount(0)

  // In-app: a reload would reset the msw worker's data. Away to the Invoices tab and back to the Acme draft.
  await page.getByRole('link', { name: 'Invoices', exact: true }).click()
  // The row links its supplier too (#6869): open the invoice's own link.
  await page.locator('tr[data-invoice]', { hasText: 'Acme' }).getByRole('link').filter({ hasNotText: 'Acme' }).click()
  await expect(page).toHaveURL(/\/filament\/invoices\/[0-9a-f-]+$/)
  await expect(page.getByText('Import running')).toBeVisible()
  await expect(page.getByRole('button', { name: 'Import with the assistant' })).toHaveCount(0)
  await page.getByRole('button', { name: 'Open', exact: true }).click()
  await expect(panel.getByTestId('active-session-title')).toHaveText('Import invoice acme-receipt.pdf')
})

test('a total that does not add up cannot be confirmed', async ({ page }) => {
  await addSupplierAndProduct(page)
  await startDraft(page)

  await page.getByRole('button', { name: 'Add line' }).click()
  await typeLine(page, 1, { description: 'Jade White PLA', sku: 'ACME-JADE', quantity: '2', unitPrice: '20.00' })
  await header(page, { number: 'AC-1002', subtotal: '40.00', shipping: '0', tax: '0', total: '45.00' })

  await expect(page.getByText('The total does not match the lines')).toBeVisible()
  await expect(page.getByText('$5.00 apart')).toBeVisible()
  await expect(page.getByRole('button', { name: 'Confirm', exact: true })).toBeDisabled()
})
