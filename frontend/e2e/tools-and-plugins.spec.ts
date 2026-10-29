import { expect, test, type Page } from '@playwright/test'
import { bridgeCall } from './bridge'

/**
 * #2171 — Settings → Tools & plugins, against the msw capabilities tree
 * (`src/mocks/features/capabilities.ts`): one access select per tool, a lowering asks one
 * line, Reset returns the default, and a tool's details show its parameters and raw JSON.
 * Every access control is the agent's too (#4208).
 */

const DEFAULT = 'outward (asks first) (default)'

async function scadbuddyTools(page: Page) {
  await page.goto('/settings/tools')
  const source = page.getByRole('region', { name: 'Source ScadBuddy tools' })
  await source.getByRole('button', { name: 'ScadBuddy tools', exact: true }).click()
  return source
}

test.describe('Tools & plugins (#2171)', () => {
  test.skip(!!process.env.E2E_BASE_URL, 'msw-backed; the agent service is not in the real stack yet')

  test('lowers a built-in outward tool after one confirmation and resets it', async ({ page }) => {
    const source = await scadbuddyTools(page)
    await source.getByRole('textbox', { name: 'Filter ScadBuddy tools' }).fill('send')
    await expect(source.getByRole('combobox', { name: /^Access of / })).toHaveCount(1)

    const select = source.getByRole('combobox', { name: 'Access of send_to_printer' })
    await expect(select).toHaveText(DEFAULT)
    await select.click()
    await page.getByRole('listbox').getByRole('option', { name: 'write', exact: true }).click()
    const lower = page.getByRole('alertdialog', { name: 'Lower Send to printer?' })
    await expect(lower).toContainText('It will run without asking first.')
    await lower.getByRole('button', { name: 'Lower to write' }).click()
    await expect(lower).toHaveCount(0)
    await expect(select).toHaveText('write')
    await expect(source).toContainText(/\d+ tools, 1 changed/)

    // Stored, not only shown: a reload reads it back.
    await page.reload()
    await source.getByRole('button', { name: 'ScadBuddy tools', exact: true }).click()
    await expect(select).toHaveText('write')

    await source.getByRole('button', { name: 'Reset send_to_printer' }).click()
    await expect(select).toHaveText(DEFAULT)
    await expect(source.getByRole('button', { name: 'Reset send_to_printer' })).toHaveCount(0)

    await page.reload()
    await source.getByRole('button', { name: 'ScadBuddy tools', exact: true }).click()
    await expect(select).toHaveText(DEFAULT)
    await expect(source).not.toContainText(/changed/)
  })

  test('cancelling a lowering sends nothing', async ({ page }) => {
    const source = await scadbuddyTools(page)
    const select = source.getByRole('combobox', { name: 'Access of delete_model' })
    await select.click()
    await page.getByRole('listbox').getByRole('option', { name: 'off', exact: true }).click()
    const lower = page.getByRole('alertdialog', { name: 'Lower Delete a model?' })
    await expect(lower).toContainText('The assistant will not be able to use it.')
    await page.keyboard.press('Escape')
    await expect(lower).toHaveCount(0)
    await expect(select).toHaveText(DEFAULT)
    await expect(source.getByRole('button', { name: 'Reset delete_model' })).toHaveCount(0)
  })

  test("opens a tool's details and its raw JSON", async ({ page }) => {
    const source = await scadbuddyTools(page)
    await source.getByRole('button', { name: 'Details of send_to_printer' }).click()
    const params = source.getByRole('table', { name: 'Parameters of send_to_printer' })
    await expect(params).toContainText('query')
    await expect(params).toContainText('required')
    await expect(params).toContainText('What to look for.')
    // A built-in tool's annotations are ScadBuddy's own, not a server's claims.
    await expect(source.getByText('readOnlyHint: false')).toBeVisible()
    await expect(source.getByText('Claims made by the server, not checked by ScadBuddy.')).toHaveCount(0)

    await source.getByRole('button', { name: 'View JSON' }).click()
    const raw = source.getByTestId('raw-json')
    await expect(raw).toContainText('"name": "send_to_printer"')
    await expect(raw).toContainText('"inputSchema"')
  })

  test("the agent's bridge may set an access too (#4208)", async ({ page }) => {
    await page.goto('/settings/tools')
    await page.waitForFunction(() => Boolean((globalThis as { __scadbuddyBridge?: unknown }).__scadbuddyBridge))
    const source = page.getByRole('region', { name: 'Source ScadBuddy tools' })
    await expect(source).toBeVisible()

    for (const [role, name] of [
      ['button', 'ScadBuddy tools'],
      ['combobox', 'Access of send_to_printer'],
      ['option', 'write'],
      ['button', 'Lower to write'],
    ] as const) {
      // Each control renders after the one before: a click on the next waits for it.
      await expect(page.getByRole(role, { name, exact: true }).first()).toBeVisible()
      expect(await bridgeCall(page, 'click', { role, name }), `${role} ${name}`).toMatchObject({ ok: true })
    }
    await expect(source.getByRole('button', { name: 'Reset send_to_printer' })).toBeVisible()
    await expect(source.getByRole('combobox', { name: 'Access of send_to_printer' })).toHaveText('write')

    await page.reload()
    await source.getByRole('button', { name: 'ScadBuddy tools', exact: true }).click()
    await expect(source.getByRole('combobox', { name: 'Access of send_to_printer' })).toHaveText('write')
  })
})
