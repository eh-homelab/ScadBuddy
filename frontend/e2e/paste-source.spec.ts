import { expect, test, type Page } from '@playwright/test'

const SOURCE = `/* [Text] */
name = "Nova";
text_size = 14; // [6:0.5:28]
cube([text_size, 10, 2]);
`

const BROKEN = `size = 10;
cube([size, size, size)
`

test.describe('pasted source', () => {
  test.skip(
    !!process.env.E2E_BASE_URL,
    'msw-backed; the real stack is covered by real-backend.spec.ts',
  )

  /** Monaco types through a hidden textarea; the text goes in as one insertion. */
  async function typeSource(page: Page, text: string) {
    const editor = page.getByRole('code').or(page.locator('.monaco-editor').first())
    await expect(editor.first()).toBeVisible()
    await page.locator('.monaco-editor .view-lines').first().click()
    await page.keyboard.press('ControlOrMeta+a')
    await page.keyboard.insertText(text)
  }

  test('pastes a new model, sees it parse and opens the customizer', async ({ page }) => {
    await page.goto('/')
    await page.getByRole('button', { name: 'Paste source' }).click()

    await page.getByRole('textbox', { name: 'Name', exact: true }).fill('Pasted Keychain')
    await typeSource(page, SOURCE)

    // The Monarch language is live: `cube` is tokenized as a builtin, not an identifier.
    await expect(page.locator('.monaco-editor .mtk8, .monaco-editor .mtk9').first()).toBeVisible()
    await expect(page.getByText(/^Parses cleanly/)).toBeVisible()

    await page.getByRole('button', { name: 'Save and customize' }).click()
    await expect(page).toHaveURL(/\/m\/pasted-keychain$/)
    await expect(page.getByTestId('bbox-readout')).toBeVisible()

    // Pasted with no thumbnail: back on the catalogue, its card shows the default
    // render the backend made in the background.
    await page.getByRole('main').getByRole('link', { name: 'Models' }).click()
    const card = page.getByRole('listitem').filter({ hasText: 'Pasted Keychain' })
    await expect(card.getByRole('img', { name: 'Pasted Keychain' })).toHaveAttribute(
      'src',
      /\/thumbnail\?v=[^"]*\.preview\.\.[0-9a-f]{16}$/,
    )
  })

  test('the editor is no keyboard trap, saves on Ctrl+S and guards unsaved edits (#997)', async ({ page }) => {
    await page.goto('/new')
    await page.getByRole('textbox', { name: 'Name', exact: true }).fill('Keyboard Cube')
    await typeSource(page, SOURCE)
    const editor = page.getByRole('textbox', { name: 'OpenSCAD source' })
    await expect(editor).toBeFocused()

    // Tab is still indentation...
    await page.keyboard.press('Tab')
    await expect(editor).toBeFocused()
    // ...and Escape is the way out, either way.
    await expect(page.getByText('Esc, then Tab, to leave the editor')).toBeVisible()
    await page.keyboard.press('Escape')
    await page.keyboard.press('Tab')
    await expect(editor).not.toBeFocused()
    await editor.focus()
    await page.keyboard.press('Escape')
    await page.keyboard.press('Shift+Tab')
    await expect(editor).not.toBeFocused()

    // Unsaved: leaving asks, and Stay keeps the edits.
    await page.getByRole('main').getByRole('link', { name: 'Models' }).click()
    const dialog = page.getByRole('dialog', { name: 'Leave without saving?' })
    await dialog.getByRole('button', { name: 'Stay' }).click()
    await expect(page).toHaveURL(/\/new$/)

    await expect(page.getByText(/^Parses cleanly/)).toBeVisible()
    await editor.focus()
    await page.keyboard.press('ControlOrMeta+s')
    await expect(page).toHaveURL(/\/m\/keyboard-cube$/)
    await expect(dialog).toBeHidden()
  })

  test('Escape with several cursors collapses them, and leaves Tab indenting (#997)', async ({ page }) => {
    await page.goto('/new')
    await typeSource(page, SOURCE)
    const editor = page.getByRole('textbox', { name: 'OpenSCAD source' })
    const lines = page.locator('.monaco-editor .view-line')
    await lines.nth(1).click()
    await lines.nth(3).click({ modifiers: ['Alt'] })
    const cursors = page.locator('.monaco-editor .cursors-layer .cursor')
    await expect(cursors).toHaveCount(2)

    await page.keyboard.press('Escape')
    await expect(cursors).toHaveCount(1)
    // The Escape was Monaco's, not the way out: Tab still indents.
    await page.keyboard.press('Tab')
    await expect(editor).toBeFocused()
  })

  test('squiggles the failing line and only saves when forced', async ({ page }) => {
    await page.goto('/new')
    await page.getByRole('textbox', { name: 'Name', exact: true }).fill('Half Cube')
    await typeSource(page, BROKEN)

    const report = page.getByTestId('check-report')
    await expect(report).toContainText('Line 2')
    // The diagnostic reached Monaco as a marker, not just the list below it.
    await expect(page.locator('.monaco-editor .squiggly-error').first()).toBeVisible()

    await page.getByRole('button', { name: 'Save and customize' }).click()
    await expect(page).toHaveURL(/\/new$/)

    await page.getByRole('button', { name: 'Save anyway' }).click()
    await expect(page).toHaveURL(/\/m\/half-cube$/)
    // Forced source has no derivable schema, so the customizer says so rather than
    // erroring out — the save was allowed, not pretended to have worked.
    // Not `getByRole('alert')`: Monaco leaves its own live regions in the document.
    await expect(page.getByText(/could not build a customizer schema/)).toBeVisible()

    // The editor model is disposed with the page, so a new paste starts blank rather
    // than resurrecting the last one from the reused `models/new` URI.
    await page.goto('/new')
    await expect(page.locator('.monaco-editor .view-lines')).not.toContainText('size = 10')
  })

  test('edits the source of an existing model', async ({ page }) => {
    await page.goto('/m/name-keychain')
    await page.getByRole('link', { name: 'Edit source' }).click()

    await expect(page.locator('.monaco-editor .view-lines')).toContainText('Name on the tag')

    await typeSource(page, SOURCE)
    await page.getByRole('button', { name: 'Save source' }).click()
    await expect(page).toHaveURL(/\/m\/name-keychain$/)

    await page.getByRole('link', { name: 'Edit source' }).click()
    await expect(page.locator('.monaco-editor .view-lines')).toContainText('Nova')
  })
})
