import { expect, test } from '@playwright/test'
import { choose } from './select'

// #3823 (plan 2026-10-16, Task 10) — the printer registry against the msw API
// (`src/mocks/features/printers.ts`): link a discovered Bambuddy printer, link a printer
// over the LAN, and see both on the Prints page, each labelled with the link that answers.

test.describe('printers', () => {
  test.skip(!!process.env.E2E_BASE_URL, 'msw-backed; the real stack is covered by real-backend.spec.ts')

  test('links a discovered printer and a LAN printer, and Now printing labels each path', async ({ page }) => {
    await page.goto('/settings/printing#printers')
    const section = page.getByRole('region', { name: 'Printers' })

    // A row by serial only: link it to the Bambuddy printer 3DP-77A-114.
    const garage = section.getByRole('listitem', { name: 'Printer Garage' })
    await expect(garage.getByRole('button', { name: 'Link Bambuddy printer Spare' })).toBeDisabled()
    await garage.getByRole('button', { name: 'Link Bambuddy printer 3DP-77A-114' }).click()
    await expect(garage.getByText('Bambuddy printer 2')).toBeVisible()

    // A new printer, reached only over the LAN.
    const add = section.getByRole('form', { name: 'Add a printer' })
    await add.getByLabel('Serial').fill('01P00A0000000BB')
    await add.getByLabel('Name').fill('Attic')
    await add.getByRole('button', { name: 'Add printer' }).click()
    const attic = section.getByRole('listitem', { name: 'Printer Attic' })
    const lan = attic.getByRole('form', { name: 'LAN link for Attic' })
    await lan.getByLabel('Host or IP address').fill('10.0.0.9')
    await lan.getByLabel('Access code').fill('12345678')
    await lan.getByRole('button', { name: 'Test and link' }).click()
    await expect(attic.getByText('LAN 10.0.0.9')).toBeVisible()
    // The code is nowhere on the page once linked: not in the text, not in any field.
    await expect(page.getByText('12345678')).toHaveCount(0)
    const values = await page.locator('input').evaluateAll((inputs) => inputs.map((input) => (input as { value: string }).value))
    expect(values).not.toContain('12345678')
    const path = attic.getByRole('combobox', { name: 'How Attic prints' })
    await choose(page, path, 'Over the LAN')
    await expect(path).toHaveText('Over the LAN')

    // In the app, not a reload: the mock backend's state lives in the page.
    await page.getByRole('navigation', { name: 'Main' }).getByRole('link', { name: 'Prints' }).click()
    await expect(page.getByTestId('printer-now-1').getByText('Bambuddy', { exact: true })).toBeVisible()
    // The printer just linked to Garage: still answered through Bambuddy (Garage has no LAN link).
    await expect(page.getByTestId('printer-now-2').getByText('Bambuddy', { exact: true })).toBeVisible()
    const atticCard = page.locator('[data-testid^="printer-now-"]', { hasText: 'Attic' })
    await expect(atticCard.getByText('LAN', { exact: true })).toBeVisible()
    await expect(atticCard.getByRole('button', { name: /Camera/ })).toHaveCount(0)
  })
})
