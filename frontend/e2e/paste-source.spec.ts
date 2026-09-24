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

    await page.getByLabel('Name').fill('Pasted Keychain')
    await typeSource(page, SOURCE)

    // The Monarch language is live: `cube` is tokenized as a builtin, not an identifier.
    await expect(page.locator('.monaco-editor .mtk8, .monaco-editor .mtk9').first()).toBeVisible()
    await expect(page.getByText(/^Parses cleanly/)).toBeVisible()

    await page.getByRole('button', { name: 'Save and customize' }).click()
    await expect(page).toHaveURL(/\/m\/pasted-keychain$/)
    await expect(page.getByTestId('bbox-readout')).toBeVisible()
  })

  test('squiggles the failing line and only saves when forced', async ({ page }) => {
    await page.goto('/new')
    await page.getByLabel('Name').fill('Half Cube')
    await typeSource(page, BROKEN)

    const report = page.getByTestId('check-report')
    await expect(report).toContainText('Line 2')
    // The diagnostic reached Monaco as a marker, not just the list below it.
    await expect(page.locator('.monaco-editor .squiggly-error').first()).toBeVisible()

    await page.getByRole('button', { name: 'Save and customize' }).click()
    await expect(page).toHaveURL(/\/new$/)

    await page.getByRole('button', { name: 'Save anyway' }).click()
    await expect(page).toHaveURL(/\/m\/half-cube$/)

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
