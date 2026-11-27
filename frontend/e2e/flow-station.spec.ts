import { expect, test } from '@playwright/test'

// #8062 — a station: the open tasks of one place, and a scan that answers one of them. The
// msw seed (`src/mocks/features/tasks.ts`) holds a scan task at the Workshop that expects S-9.

const WORKSHOP = '62620003-0000-4000-8000-000000000001'

test.describe('flow station (#8062)', () => {
  test.skip(!!process.env.E2E_BASE_URL, 'msw-backed; the real stack is covered by its own specs')

  test('a scan at the station fills the task, and the answer closes it', async ({ page }) => {
    await page.goto(`/station/${WORKSHOP}`)
    const task = page.getByRole('region', { name: 'Shelve the new spool' })
    await expect(task).toBeVisible()
    await expect(task).toContainText('Scan the gold spool')

    const scan = page.getByRole('textbox', { name: 'Scan a code' })
    await expect(scan).toBeFocused()
    await scan.fill('S-9')
    await scan.press('Enter')
    await expect(task.getByRole('textbox', { name: /^Spool/ })).toHaveValue('S-9')

    await task.getByRole('button', { name: 'Answer' }).click()
    await expect(page.getByRole('region', { name: 'Shelve the new spool' })).toHaveCount(0)
    await expect(page.getByText('No open tasks here.')).toBeVisible()
  })

  test('a code no task waits for says so and keeps the task', async ({ page }) => {
    await page.goto(`/station/${WORKSHOP}`)
    const scan = page.getByRole('textbox', { name: 'Scan a code' })
    await scan.fill('S-10')
    await scan.press('Enter')
    await expect(page.getByRole('alert').filter({ hasText: 'No task here waits for S-10.' })).toBeVisible()
    await expect(page.getByRole('region', { name: 'Shelve the new spool' })).toBeVisible()
  })
})
