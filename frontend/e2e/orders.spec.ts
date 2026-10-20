import { expect, test } from '@playwright/test'

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

  test('on a phone the orders page fits', async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 })
    await page.goto('/orders')
    await expect(page.getByRole('heading', { name: 'Orders' })).toBeVisible()
    expect(await page.evaluate('document.documentElement.scrollWidth')).toBeLessThanOrEqual(390)
  })
})
