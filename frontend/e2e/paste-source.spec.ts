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

  /** CodeMirror owns a contenteditable, so the text goes in as an insertion, not keys. */
  async function typeSource(page: Page, text: string) {
    const editor = page.getByRole('textbox', { name: 'OpenSCAD source' })
    await editor.click()
    await page.keyboard.press('ControlOrMeta+a')
    await page.keyboard.insertText(text)
  }

  test('pastes a new model, checks it and opens the customizer', async ({ page }) => {
    await page.goto('/')
    await page.getByRole('button', { name: 'Paste source' }).click()

    await page.getByLabel('Name').fill('Pasted Keychain')
    await typeSource(page, SOURCE)
    // The mode is live: `cube` is a builtin, the annotation is a comment.
    await expect(page.locator('.cm-gutterElement').filter({ hasText: '4' })).toBeVisible()

    await page.getByRole('button', { name: 'Check' }).click()
    await expect(page.getByText('Parses cleanly.')).toBeVisible()

    await page.getByRole('button', { name: 'Save and customize' }).click()
    await expect(page).toHaveURL(/\/m\/pasted-keychain$/)
    await expect(page.getByTestId('bbox-readout')).toBeVisible()
  })

  test('marks the failing line and only saves when forced', async ({ page }) => {
    await page.goto('/new')
    await page.getByLabel('Name').fill('Half Cube')
    await typeSource(page, BROKEN)

    await page.getByRole('button', { name: 'Save and customize' }).click()
    const report = page.getByTestId('check-report')
    await expect(report).toContainText('Line 2')
    await expect(page.locator('.cm-sb-error-line')).toHaveCount(1)
    await expect(page).toHaveURL(/\/new$/)

    await page.getByRole('button', { name: 'Save anyway' }).click()
    await expect(page).toHaveURL(/\/m\/half-cube$/)
  })

  test('edits the source of an existing model', async ({ page }) => {
    await page.goto('/m/name-keychain')
    await page.getByRole('link', { name: 'Edit source' }).click()

    const editor = page.getByRole('textbox', { name: 'OpenSCAD source' })
    await expect(editor).toContainText('Name on the tag')

    await typeSource(page, SOURCE)
    await page.getByRole('button', { name: 'Save source' }).click()
    await expect(page).toHaveURL(/\/m\/name-keychain$/)

    await page.getByRole('link', { name: 'Edit source' }).click()
    await expect(page.getByRole('textbox', { name: 'OpenSCAD source' })).toContainText('Nova')
  })
})
