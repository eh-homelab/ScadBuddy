import { expect, test } from '@playwright/test'
import { choose } from './select'

// Orders phase 1 (#4249) over the mocks (src/mocks/features/orders/): a customer, an
// order, an item from Customize, a typed price, accept, fulfil, ship, close.

test.describe('orders', () => {
  test.skip(!!process.env.E2E_BASE_URL, 'msw-backed; the real stack is covered by real-backend.spec.ts')

  test('a customer, then an order all the way to closed', async ({ page }) => {
    await page.goto('/')
    await page.getByRole('link', { name: 'Orders' }).click()
    await expect(page.getByRole('heading', { name: 'Orders' })).toBeVisible()
    await expect(page.getByRole('table', { name: 'Orders' })).toBeVisible()

    // A customer of our own.
    await page.goto('/customers')
    await page.getByRole('button', { name: 'New customer' }).click()
    const drawer = page.getByRole('dialog', { name: 'New customer' })
    await drawer.getByLabel('Name').fill('Grace Hopper')
    await drawer.getByRole('button', { name: 'Create customer' }).click()
    await expect(page.getByRole('heading', { name: 'Grace Hopper' })).toBeVisible()

    // The draft order with its item: price it, accept it, fulfil, ship and close.
    await page.goto('/orders')
    await page.getByRole('link', { name: 'ORD-2026-3' }).click()
    await expect(page.getByRole('heading', { name: 'ORD-2026-3' })).toBeVisible()
    await page.getByRole('table', { name: 'Line items' }).getByRole('button', { name: 'Name Keychain' }).click()
    const item = page.getByRole('dialog', { name: 'Name Keychain' })
    await item.getByLabel(/Price per unit/).fill('12.50')
    await item.getByRole('button', { name: 'Record and accept' }).click()
    await expect(item.getByText('Accepted')).toBeVisible()
    await item.getByRole('button', { name: 'Close' }).click()
    await page.getByRole('button', { name: 'Accept order' }).click()
    await expect(page.getByTestId('order-totals')).toContainText('25.00')

    await page.getByRole('table', { name: 'Line items' }).getByRole('button', { name: 'Name Keychain' }).click()
    const again = page.getByRole('dialog', { name: 'Name Keychain' })
    await again.getByRole('button', { name: 'Record attempt' }).click()
    await again.getByRole('button', { name: 'Accept attempt 1' }).click()
    await expect(again.getByText('Fulfilled')).toBeVisible()
    await again.getByRole('button', { name: 'Close' }).click()
    await page.getByRole('button', { name: 'Add shipment' }).click()
    await expect(page.getByText('Shipped', { exact: true })).toBeVisible()
    await page.getByRole('button', { name: 'Close order' }).click()
    await expect(page.getByText('Closed', { exact: true })).toBeVisible()
  })

  test('prices an item in the items table', async ({ page }) => {
    await page.goto('/orders')
    await page.getByRole('link', { name: 'ORD-2026-3' }).click()
    const items = page.getByRole('table', { name: 'Line items' })
    const unit = items.getByRole('textbox', { name: 'Unit price for Name Keychain' })
    await unit.fill('12.50')
    await unit.press('Enter')
    await expect(items.getByRole('button', { name: 'Accept unit price' })).toBeDisabled()
    await expect(items.getByText('not accepted')).toHaveCount(0)
    await expect(page.getByTestId('order-totals')).toContainText('25.00')
    await page.getByRole('button', { name: 'Accept order' }).click()
    await expect(unit).toHaveCount(0)
    await expect(items.getByRole('cell', { name: '$12.50', exact: true })).toBeVisible()
  })

  // #4251: the customer made inside the New order dialog, with the order's due date.
  test('a new order for a customer made in the dialog', async ({ page }) => {
    await page.goto('/orders')
    await page.getByRole('button', { name: 'New order' }).click()
    const dialog = page.getByRole('dialog', { name: 'New order' })
    await choose(page, dialog.getByRole('combobox', { name: 'Customer for the new order' }), 'New customer…')
    await dialog.getByLabel('Customer name').fill('Grace Hopper')
    await dialog.getByLabel('Email').fill('grace@example.com')
    await dialog.getByLabel('Due').fill('2026-12-01')
    await dialog.getByRole('button', { name: 'Create order' }).click()
    await expect(page).toHaveURL(/\/orders\/o-/)
    // The mocks live in the page, so the customer is checked on the order, not after a reload.
    await expect(page.getByRole('link', { name: 'Grace Hopper' })).toBeVisible()
    await expect(page.getByLabel('Due')).toHaveValue('2026-12-01')
  })

  test('adds the generated output to an order from Customize', async ({ page }) => {
    await page.goto('/m/name-keychain')
    const generate = page.getByTestId('generate')
    await expect(generate).toBeEnabled()
    await generate.click()
    await expect(page.getByText(/^Saved /)).toBeVisible()
    await page.getByRole('button', { name: 'More to generate' }).click()
    await page.getByRole('menuitem', { name: /Add to order/ }).click()
    const dialog = page.getByRole('dialog', { name: 'Add to order' })
    await dialog.getByRole('combobox', { name: 'Customer' }).click()
    await page.getByRole('option', { name: 'Ada Lovelace' }).click()
    await expect(dialog.getByRole('combobox', { name: 'Order' })).toContainText('ORD-2026-3')
    await dialog.getByRole('button', { name: 'Add to order' }).click()
    await expect(page).toHaveURL(/\/orders\/0e5a3c1e-1111-4222-8333-944455556661$/)
    await expect(page.getByRole('table', { name: 'Line items' }).getByRole('row')).toHaveCount(3)
  })

  // #4250 — a quote from a slice: the print dialog in its quote mode, a price-book
  // filament per slot, and the quote with its lines on the item.
  test('quotes an item from a slice of its output', async ({ page }) => {
    await page.goto('/orders')
    await page.getByRole('link', { name: 'ORD-2026-3' }).click()
    await page.getByRole('table', { name: 'Line items' }).getByRole('button', { name: 'Name Keychain' }).click()
    const item = page.getByRole('dialog', { name: 'Name Keychain' })
    await item.getByTestId('quote-from-slice').click()
    const dialog = page.getByRole('dialog', { name: 'Quote from a slice' })
    const slots = dialog.getByRole('combobox', { name: /^Filament for slot/ })
    await expect(slots.first()).toBeVisible()
    const count = await slots.count()
    for (let index = 0; index < count; index++) {
      await choose(page, slots.nth(index), index % 2 === 0 ? /^Bambu PLA/ : /^Bambu PETG/)
    }
    await expect(dialog.getByRole('button', { name: 'Print' })).toHaveCount(0)
    await dialog.getByTestId('run-quote').click()
    await expect(dialog).toBeHidden()
    await expect(item.getByText(/^From a slice ·/)).toBeVisible()
    const lines = item.getByRole('list', { name: 'Quote lines' })
    await expect(lines.getByRole('listitem')).toHaveCount(count)
    await expect(lines).toContainText('Filament')
  })

  // #4693 — a link to a line item opens the order with that item marked and open, and
  // opening a row puts it in the URL.
  test('opens a line item from a link to it', async ({ page }) => {
    await page.goto('/orders/0e5a3c1e-1111-4222-8333-944455556661?item=7b1c2d3e-4f50-4617-8829-aabbccddee01')
    await expect(page.getByRole('dialog', { name: 'Name Keychain' })).toBeVisible()
    await expect(page.locator('#item-7b1c2d3e-4f50-4617-8829-aabbccddee01')).toHaveAttribute('aria-current', 'true')
    await page.getByRole('dialog', { name: 'Name Keychain' }).getByRole('button', { name: 'Close' }).click()
    await expect(page).toHaveURL(/\/orders\/0e5a3c1e-1111-4222-8333-944455556661$/)
    await page.getByRole('table', { name: 'Line items' }).getByRole('button', { name: 'Name Keychain' }).click()
    await expect(page).toHaveURL(/\?item=7b1c2d3e-4f50-4617-8829-aabbccddee01$/)
  })

  test('estimates an output from Add to order', async ({ page }) => {
    await page.goto('/m/name-keychain')
    const generate = page.getByTestId('generate')
    await expect(generate).toBeEnabled()
    await generate.click()
    await expect(page.getByText(/^Saved /)).toBeVisible()
    await page.getByRole('button', { name: 'More to generate' }).click()
    await page.getByRole('menuitem', { name: /Add to order/ }).click()
    const dialog = page.getByRole('dialog', { name: 'Add to order' })
    await choose(page, dialog.getByRole('combobox', { name: 'Estimate with' }), /Sunflower Yellow/)
    await expect(dialog.getByTestId('order-estimate')).toContainText(/An estimate: about [\d.]+ g/)
  })

  // #4251 — "Add item" on the order page: an output of a template, then a template's
  // current version with its defaults, without leaving the order.
  test('adds items from the order page', async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 })
    await page.goto('/orders')
    await page.getByRole('link', { name: 'ORD-2026-3' }).click()
    await expect(page.getByRole('heading', { name: 'ORD-2026-3' })).toBeVisible()
    const rows = page.getByRole('table', { name: 'Line items' }).getByRole('row')
    await expect(rows).toHaveCount(2)

    await page.getByRole('button', { name: 'Add item' }).click()
    let dialog = page.getByRole('dialog', { name: 'Add item' })
    await dialog.getByLabel('Template').fill('keychain')
    await dialog.getByRole('button', { name: /Name Keychain/ }).click()
    await dialog.getByRole('radio', { name: 'Nova' }).click()
    await dialog.getByLabel('Quantity').fill('2')
    expect(await page.evaluate('document.documentElement.scrollWidth')).toBeLessThanOrEqual(390)
    await dialog.getByRole('button', { name: 'Add item' }).click()
    await expect(dialog).toBeHidden()
    await expect(rows).toHaveCount(3)

    await page.getByRole('button', { name: 'Add item' }).click()
    dialog = page.getByRole('dialog', { name: 'Add item' })
    await dialog.getByLabel('Template').fill('maze')
    await dialog.getByRole('button', { name: /Ball Maze/ }).click()
    await expect(dialog.getByRole('radio', { name: 'Current version, default settings' })).toBeChecked()
    await dialog.getByRole('button', { name: 'Add item' }).click()
    await expect(dialog).toBeHidden()
    await expect(rows).toHaveCount(4)
    await expect(page.getByRole('table', { name: 'Line items' }).getByRole('button', { name: 'Ball Maze' })).toBeVisible()
  })

  test('on a phone the orders page fits', async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 })
    await page.goto('/orders')
    await expect(page.getByRole('heading', { name: 'Orders' })).toBeVisible()
    expect(await page.evaluate('document.documentElement.scrollWidth')).toBeLessThanOrEqual(390)
  })
})
