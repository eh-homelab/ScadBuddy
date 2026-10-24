import { expect, test } from '@playwright/test'

// #4520 — Rename on a History card puts the focus in the name field, in a real browser
// (jsdom never ran the menu's close, which took the focus back and dropped it on <body>).
test.describe('renaming an output (#4520)', () => {
  test.skip(!!process.env.E2E_BASE_URL, 'msw-backed; the real stack is covered by real-backend.spec.ts')

  for (const width of [1280, 390]) {
    test(`Rename leaves the focus in the name field at ${width} px`, async ({ page }) => {
      await page.setViewportSize({ width, height: 844 })
      await page.goto('/m/name-keychain/history')
      await page.getByRole('button', { name: 'More for Reagan' }).click()
      await page.getByRole('menuitem', { name: 'Rename Reagan' }).click()
      const field = page.getByRole('textbox', { name: 'Name for Reagan' })
      await expect(field).toBeFocused()
      // Still there once the menu has finished closing, and typing goes into it.
      await expect(page.getByRole('menu')).toBeHidden()
      await expect(field).toBeFocused()
      await page.keyboard.type(' 2')
      await expect(field).toHaveValue('Reagan 2')
    })
  }
})
