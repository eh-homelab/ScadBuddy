import { expect, test } from '@playwright/test'

const BUILTIN = 'builtin%3Akeychain-template'

test.describe('duplicating a template (#159)', () => {
  test.skip(
    !!process.env.E2E_BASE_URL,
    'msw-backed; the real stack is covered by real-backend.spec.ts',
  )

  test('duplicates a built-in to edit and lands on an editable copy', async ({ page }) => {
    await page.goto(`/m/${BUILTIN}/source`)
    await expect(page.getByRole('heading', { name: 'View source' })).toBeVisible()
    await expect(page.getByRole('button', { name: 'Save source' })).toHaveCount(0)

    await page.getByRole('button', { name: 'Duplicate to edit' }).click()
    const dialog = page.getByRole('dialog', { name: 'Duplicate Keychain Template' })
    await expect(dialog.getByLabel('Name')).toHaveValue('Keychain Template copy')
    await dialog.getByRole('button', { name: 'Duplicate' }).click()

    await expect(page).toHaveURL(/\/m\/keychain-template-copy\/source$/)
    await expect(page.getByRole('heading', { name: 'Edit source' })).toBeVisible()
    await expect(page.getByRole('button', { name: 'Save source' })).toBeVisible()

    // The copy says where it came from, and the built-in is still read-only.
    // In-app, not `goto`: a reload starts the msw state over, and the copy with it.
    await page.getByRole('link', { name: 'Keychain Template copy', exact: true }).click()
    await expect(page).toHaveURL(/\/m\/keychain-template-copy$/)
    const from = page.getByTestId('duplicated-from')
    await expect(from).toContainText('Duplicated from builtin:keychain-template')
    await from.getByRole('link').click()
    await expect(page).toHaveURL(new RegExp(`/m/${BUILTIN}$`))
    await expect(page.getByTestId('builtin-badge')).toBeVisible()
  })

  test('duplicates from a catalogue card, and a taken name is refused in place', async ({
    page,
  }) => {
    await page.goto('/')
    const card = page.getByRole('listitem').filter({ has: page.getByRole('heading', { name: 'Name Keychain' }) })
    await card.getByRole('button', { name: 'Duplicate' }).click()

    const dialog = page.getByRole('dialog', { name: 'Duplicate Name Keychain' })
    await dialog.getByLabel('Name').fill('Gridfinity Bin')
    await dialog.getByRole('button', { name: 'Duplicate' }).click()
    await expect(dialog.getByRole('alert')).toContainText("'gridfinity-bin' already exists")

    await dialog.getByLabel('Name').fill('Keychain for Nova')
    await dialog.getByRole('button', { name: 'Duplicate' }).click()
    await expect(page).toHaveURL(/\/m\/keychain-for-nova$/)
    await expect(page.getByRole('heading', { name: 'Keychain for Nova' })).toBeVisible()
  })
})
