import { expect, test } from '@playwright/test'

test.describe('library', () => {
  test.skip(
    !!process.env.E2E_BASE_URL,
    'msw-backed; the real stack is covered by real-backend.spec.ts',
  )

  test('prints a library 3MF and reopens on its last choices', async ({ page }) => {
    await page.goto('/')
    await page.getByRole('link', { name: 'Library' }).click()
    await expect(page.getByTestId('library-file-89')).toBeVisible()
    await expect(page.getByTestId('library-file-104')).toHaveCount(0)

    await page.getByTestId('library-print-89').click()
    let dialog = page.getByRole('dialog', { name: 'Print' })
    await dialog.getByRole('switch', { name: 'Advanced' }).click()
    await dialog.getByRole('radio', { name: /0\.2 mm/ }).check()
    await dialog.getByRole('button', { name: 'Print', exact: true }).click()
    await expect(dialog.getByTestId('queued-items')).toContainText('Queue #')
    await expect(dialog.getByRole('button', { name: 'Open in queue' })).toBeVisible()
    await dialog.getByRole('button', { name: 'Done' }).click()

    await page.getByTestId('library-print-89').click()
    dialog = page.getByRole('dialog', { name: 'Print' })
    // Simple mode again (#768): the remembered size is sent unseen, and shown in Advanced.
    await dialog.getByRole('switch', { name: 'Advanced' }).click()
    await expect(dialog.getByRole('radio', { name: /0\.2 mm/ })).toBeChecked()
  })

  test('Advanced lists a sliced file without Print, and is remembered', async ({ page }) => {
    await page.goto('/library')
    await page.getByRole('switch', { name: 'Advanced' }).click()
    const sliced = page.getByTestId('library-file-104')
    await expect(sliced).toContainText('Print it from Bambuddy')
    await expect(sliced.getByRole('button', { name: 'Print' })).toHaveCount(0)

    await page.reload()
    await expect(page.getByTestId('library-file-104')).toBeVisible()
  })

  test('fits a phone width with no horizontal scroll', async ({ page }) => {
    await page.setViewportSize({ width: 375, height: 800 })
    await page.goto('/library')
    await expect(page.getByTestId('library-file-89')).toBeVisible()
    const scrollWidth = await page.evaluate('document.documentElement.scrollWidth')
    expect(scrollWidth).toBeLessThanOrEqual(375)
  })

  // #935 — two uploads of one output share a long generated name, cut at the end to the
  // common prefix, and the cards showed nothing else: no date, no size.
  test('tells identically named files apart at phone width', async ({ page }) => {
    await page.setViewportSize({ width: 375, height: 800 })
    await page.goto('/library')
    const first = page.getByTestId('library-file-89')
    const second = page.getByTestId('library-file-91')
    await expect(first).toBeVisible()
    await expect(second).toBeVisible()
    expect(await first.innerText()).not.toEqual(await second.innerText())
    // The name is cut in the middle, so the end (and the extension) stays readable.
    await expect(first.getByText('64efc.3mf', { exact: false })).toBeVisible()
    await expect(first.locator('time')).toHaveAttribute('datetime', '2026-09-26T17:05:45Z')
    await expect(first).toContainText('110 kB')
  })

  // #2165 — the folders are a tree, and the URL follows the page.
  test('walks the folder tree by mouse and keyboard, with Back and Forward', async ({ page }) => {
    await page.goto('/library')
    const tree = page.getByRole('tree', { name: 'Library folders' })
    const spec = tree.getByRole('treeitem', { name: 'Spec' })
    await expect(spec).toHaveAttribute('aria-expanded', 'false')
    await page.getByTestId('library-folder-toggle-10').click()
    await expect(spec).toHaveAttribute('aria-expanded', 'true')
    await tree.getByRole('treeitem', { name: 'MakerWorld' }).nth(1).click()
    await expect(page).toHaveURL(/\/library\/Spec\/MakerWorld$/)

    await page.keyboard.press('ArrowRight')
    await page.keyboard.press('ArrowRight')
    await expect(page.getByTestId('library-folder-12')).toBeFocused()
    await page.keyboard.press('Enter')
    await expect(page).toHaveURL(/\/library\/Spec\/MakerWorld\/Work$/)
    await expect(page.getByTestId('library-file-120')).toBeVisible()

    await page.goBack()
    await expect(page).toHaveURL(/\/library\/Spec\/MakerWorld$/)
    await expect(page.getByTestId('library-folder-11')).toHaveAttribute('aria-selected', 'true')
    await page.goForward()
    await expect(page.getByTestId('library-file-120')).toBeVisible()
  })

  test('deep links open a folder, a file and its print dialog', async ({ page }) => {
    await page.goto('/library/Spec/MakerWorld/Work')
    await expect(page.getByTestId('library-folder-12')).toHaveAttribute('aria-selected', 'true')
    await expect(page.getByTestId('library-file-121')).toBeVisible()

    await page.goto('/library?file=67')
    await expect(page).toHaveURL(/\/library\/MakerWorld\?file=67$/)
    await expect(page.getByTestId('library-file-67')).toHaveAttribute('aria-current', 'true')

    await page.getByTestId('library-print-67').click()
    await expect(page).toHaveURL(/print=67/)
    await expect(page.getByRole('dialog', { name: 'Print' })).toBeVisible()
    await page.goBack()
    await expect(page.getByRole('dialog', { name: 'Print' })).toHaveCount(0)
    await page.goForward()
    await expect(page.getByRole('dialog', { name: 'Print' })).toBeVisible()

    await page.goto('/library?print=89')
    await expect(page.getByRole('dialog', { name: 'Print' })).toBeVisible()
  })

  // #2167 — deletes go to Bambuddy's trash, through a confirmation, with Undo.
  test('deletes one file and undoes it, then several, reporting the one skipped', async ({ page }) => {
    await page.goto('/library/Spec/MakerWorld/Work')
    await page.getByRole('button', { name: 'Delete drawer-label.3mf' }).click()
    let dialog = page.getByRole('dialog', { name: 'Delete drawer-label.3mf?' })
    await expect(dialog).toContainText("It goes to Bambuddy's trash.")
    await dialog.getByRole('button', { name: 'Delete file' }).click()
    const toast = page.getByTestId('library-deleted')
    await expect(toast).toContainText("Moved 1 file to Bambuddy's trash.")
    await expect(page.getByTestId('library-file-120')).toHaveCount(0)
    await toast.getByRole('button', { name: 'Undo' }).click()
    await expect(toast).toContainText('Restored 1 file.')
    await expect(page.getByTestId('library-file-120')).toBeVisible()

    for (const name of ['drawer-label.3mf', 'alex-headphone-hook.3mf', 'nas-share-bracket.3mf']) {
      await page.getByRole('checkbox', { name: `Select ${name}` }).check()
    }
    await page.getByRole('button', { name: 'Delete selected (3)' }).click()
    dialog = page.getByRole('dialog', { name: 'Delete 3 files?' })
    await expect(dialog.getByTestId('library-delete-external')).toContainText('cannot be restored')
    await dialog.getByRole('button', { name: 'Delete 3 files' }).click()
    await expect(toast).toContainText('Removed 1 external file for good.')
    await expect(toast.getByTestId('library-skipped')).toContainText('alex-headphone-hook.3mf')
    await expect(page.getByTestId('library-file-121')).toBeVisible()
    await expect(page.getByTestId('library-file-122')).toHaveCount(0)
  })
})
