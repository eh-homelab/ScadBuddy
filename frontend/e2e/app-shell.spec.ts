import { expect, test } from '@playwright/test'
import { bambuddyFrame } from './bambuddyFrame'

// #3653 — the shell on shadcn's sidebar: beside the page from `md` up, a sheet below it,
// icons only inside Bambuddy's frame.
test.skip(!!process.env.E2E_BASE_URL, 'msw-backed; the real stack is covered by real-backend.spec.ts')

test('the sidebar folds to icons and its links still go to their pages', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 })
  await page.goto('/')
  const nav = page.getByRole('navigation', { name: 'Main' })
  await expect(nav.getByRole('link', { name: 'Models' })).toHaveAttribute('aria-current', 'page')
  await page.getByRole('button', { name: 'Toggle navigation' }).click()
  await expect(page.locator('[data-slot="sidebar"]')).toHaveAttribute('data-state', 'collapsed')
  await nav.getByRole('link', { name: 'Settings' }).hover()
  await expect(page.getByRole('tooltip')).toHaveText('Settings')
  await nav.getByRole('link', { name: 'Settings' }).click()
  await expect(page).toHaveURL(/\/settings$/)
  await expect(nav.getByRole('link', { name: 'Settings' })).toHaveAttribute('aria-current', 'page')
  // Kept for the next page load.
  await page.reload()
  await expect(page.locator('[data-slot="sidebar"]')).toHaveAttribute('data-state', 'collapsed')
})

test.describe('at a phone’s width', () => {
  test.use({ viewport: { width: 390, height: 844 } })

  test('the navigation is a sheet, closed by the page it goes to', async ({ page }) => {
    await page.goto('/')
    await expect(page.getByRole('navigation', { name: 'Main' })).toHaveCount(0)
    await page.getByRole('button', { name: 'Toggle navigation' }).click()
    const sheet = page.getByRole('dialog')
    await sheet.getByRole('navigation', { name: 'Main' }).getByRole('link', { name: 'Prints' }).click()
    await expect(page).toHaveURL(/\/prints$/)
    await expect(page.getByRole('dialog')).toHaveCount(0)
    expect(await page.evaluate('document.documentElement.scrollWidth')).toBeLessThanOrEqual(390)
  })
})

test('inside Bambuddy’s frame the sidebar starts as icons, and works', async ({ page, baseURL }) => {
  await page.setViewportSize({ width: 1280, height: 800 })
  const frame = await bambuddyFrame(page, baseURL, '/')
  await expect(frame.locator('header[data-embedded="true"]')).toBeVisible()
  await expect(frame.locator('[data-slot="sidebar"]')).toHaveAttribute('data-state', 'collapsed')
  await frame.getByRole('navigation', { name: 'Main' }).getByRole('link', { name: 'Library' }).click()
  await expect(frame.getByRole('navigation', { name: 'Main' }).getByRole('link', { name: 'Library' })).toHaveAttribute(
    'aria-current',
    'page',
  )
})
