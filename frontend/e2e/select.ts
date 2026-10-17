import type { Locator, Page } from '@playwright/test'

/**
 * #3672 — picks `name` in a Radix select: its trigger is a button (role combobox) and its
 * options a listbox portalled to the body while open. What `selectOption` did for a
 * native `<select>`.
 */
export async function choose(page: Page, trigger: Locator, name: string | RegExp): Promise<void> {
  await trigger.click()
  await page.getByRole('listbox').getByRole('option', { name, exact: typeof name === 'string' }).click()
}
