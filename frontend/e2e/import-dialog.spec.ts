import { expect, test } from '@playwright/test'

// Implicit submission in a real browser (#2558): the import dialog's form has its
// default button `hidden` (display: none), and the visible Import button sits in the
// dialog's footer, outside the form (#2114). jsdom models Enter submitting through
// such a button; this checks Chromium does, once.

test.describe('import from URL', () => {
  test.skip(
    !!process.env.E2E_BASE_URL,
    'msw-backed; the real stack is covered by real-backend.spec.ts',
  )

  test('Enter in the URL field imports once, even pressed again while it is in flight', async ({ page }) => {
    const posts: string[] = []
    page.on('request', (request) => {
      if (request.method() === 'POST' && request.url().endsWith('/api/v1/models/import')) posts.push(request.url())
    })
    await page.goto('/')
    await page.getByRole('button', { name: 'Import from URL' }).click()
    const dialog = page.getByRole('dialog', { name: 'Import from URL' })
    const field = dialog.getByLabel('URL')
    await field.fill('https://example.com/enter-import.scad')
    await field.press('Enter')
    await field.press('Enter')

    await expect(dialog).toBeHidden()
    await expect(page.getByRole('main').getByText('enter-import').first()).toBeVisible()
    expect(posts).toHaveLength(1)
  })
})
