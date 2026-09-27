import { expect, test, type Page } from '@playwright/test'

/** The upstream's change: its text size. */
const THEIRS = `/* [Text] */
name = "Reagan";
text_size = 16; // [6:0.5:28]
cube([text_size, 10, 2]);
`

/** The copy's own change to the same line, which makes the merge conflict. */
const OURS = `/* [Text] */
name = "Reagan";
text_size = 12; // [6:0.5:28]
cube([text_size, 10, 2]);
`

const RESOLVED = `/* [Text] */
name = "Reagan";
text_size = 15; // [6:0.5:28]
cube([text_size, 10, 2]);
`

/** Monaco types through a hidden textarea; the text goes in as one insertion. */
async function typeSource(page: Page, text: string) {
  await page.locator('.monaco-editor .view-lines').first().click()
  await page.keyboard.press('ControlOrMeta+a')
  await page.keyboard.insertText(text)
}

/** Duplicates Name Keychain as "Keychain for Nova" and lands on the copy. */
async function duplicate(page: Page) {
  await page.goto('/m/name-keychain')
  await page.getByRole('button', { name: 'Duplicate' }).click()
  const dialog = page.getByRole('dialog', { name: 'Duplicate Name Keychain' })
  await dialog.getByLabel('Name').fill('Keychain for Nova')
  await dialog.getByRole('button', { name: 'Duplicate' }).click()
  await expect(page).toHaveURL(/\/m\/keychain-for-nova$/)
  await expect(page.getByTestId('update-badge')).toHaveCount(0)
}

/** Saves `text` as the source of the model open in the customizer. */
async function editSource(page: Page, text: string) {
  await page.getByRole('link', { name: 'Edit source' }).click()
  await expect(page.locator('.monaco-editor .view-lines')).toBeVisible()
  await typeSource(page, text)
  await expect(page.getByText(/^Parses cleanly/)).toBeVisible()
  await page.getByRole('button', { name: 'Save source' }).click()
  await expect(page).toHaveURL(/\/m\/[^/]+$/)
}

/** From the copy to its upstream by the "Duplicated from" link, edited, and back. */
async function moveUpstream(page: Page) {
  await page.getByTestId('duplicated-from').getByRole('link').click()
  await expect(page).toHaveURL(/\/m\/name-keychain$/)
  await editSource(page, THEIRS)
  // In-app, not `goto`: a reload starts the msw state over.
  await page.getByRole('link', { name: 'Models' }).first().click()
  const card = page
    .getByRole('listitem')
    .filter({ has: page.getByRole('heading', { name: 'Keychain for Nova' }) })
  await expect(card.getByTestId('update-badge')).toHaveText('Update available')
  await card.getByRole('heading', { name: 'Keychain for Nova' }).click()
  await expect(page).toHaveURL(/\/m\/keychain-for-nova$/)
}

test.describe('taking upstream updates (#160)', () => {
  test.skip(
    !!process.env.E2E_BASE_URL,
    'msw-backed; the real stack is covered by real-backend.spec.ts',
  )

  test('previews a clean update and takes it', async ({ page }) => {
    await duplicate(page)
    await moveUpstream(page)

    await page.getByRole('button', { name: 'Update available' }).click()
    const dialog = page.getByRole('dialog', { name: 'Update available' })
    await expect(dialog.getByTestId('diff')).toContainText('+text_size = 16;')
    await expect(dialog.getByTestId('merge-verdict')).toContainText('Merges cleanly')
    await dialog.getByRole('button', { name: 'Take update' }).click()

    await expect(dialog).toHaveCount(0)
    await expect(page.getByTestId('update-badge')).toHaveCount(0)
  })

  test('resolves a conflicted update in the editor', async ({ page }) => {
    await duplicate(page)
    await editSource(page, OURS)
    await moveUpstream(page)

    await page.getByRole('button', { name: 'Update available' }).click()
    const dialog = page.getByRole('dialog', { name: 'Update available' })
    await expect(dialog.getByTestId('merge-verdict')).toContainText('1 conflict')
    await dialog.getByRole('button', { name: 'Take update' }).click()

    await expect(page).toHaveURL(/\/m\/keychain-for-nova\/source\?merge$/)
    await expect(page.getByRole('heading', { name: 'Resolve update' })).toBeVisible()
    await expect(page.locator('.monaco-editor .view-lines')).toContainText('<<<<<<<')
    await expect(page.getByTestId('merge-banner')).toContainText('1 conflict left.')

    await typeSource(page, RESOLVED)
    await expect(page.getByTestId('merge-banner')).toContainText('No conflicts left.')
    await expect(page.getByText(/^Parses cleanly/)).toBeVisible()
    await page.getByRole('button', { name: 'Save resolution' }).click()

    await expect(page).toHaveURL(/\/m\/keychain-for-nova$/)
    await expect(page.getByTestId('update-badge')).toHaveCount(0)
  })

  test('says an upstream is gone and detaches from it', async ({ page }) => {
    await duplicate(page)
    await page.getByTestId('duplicated-from').getByRole('link').click()
    await expect(page).toHaveURL(/\/m\/name-keychain$/)

    await page.getByRole('button', { name: 'Delete' }).click()
    await page.getByRole('button', { name: 'Delete model' }).click()
    await expect(page.getByText('1 template is a duplicate of this one')).toBeVisible()
    await page.getByRole('button', { name: 'Delete anyway' }).click()

    const card = page
      .getByRole('listitem')
      .filter({ has: page.getByRole('heading', { name: 'Keychain for Nova' }) })
    await expect(card.getByTestId('upstream-gone')).toBeVisible()
    await card.getByRole('heading', { name: 'Keychain for Nova' }).click()

    await page.getByRole('button', { name: 'Upstream gone' }).click()
    await page.getByRole('dialog', { name: 'Upstream gone' }).getByRole('button', { name: 'Detach' }).click()
    await expect(page.getByTestId('upstream-gone')).toHaveCount(0)
    await expect(page.getByTestId('duplicated-from')).toHaveCount(0)
  })
})
