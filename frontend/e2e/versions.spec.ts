import { expect, test } from '@playwright/test'

test.describe('model versions', () => {
  // msw-backed. The real repository is covered twice over: the backend's own
  // tests drive a real git in a tmp dir, and real-backend.spec.ts's
  // "versions a model, renders an old revision and restores it" drives the real
  // stack -- which is the only place `git` being present in the IMAGE is proved.
  test.skip(
    !!process.env.E2E_BASE_URL,
    'msw-backed; the real stack is covered by real-backend.spec.ts',
  )

  test('opens the history, diffs a revision and restores it', async ({ page }) => {
    await page.goto('/m/name-keychain')
    await page.getByRole('link', { name: 'Versions' }).click()

    const versions = page.getByTestId('versions')
    await expect(versions.locator('li')).toHaveCount(3)
    await expect(versions.locator('li').first()).toContainText('Edit name-keychain source')
    await expect(versions.locator('li').first()).toContainText('current')

    // The newest revision opens diffed against its parent.
    await expect(page.getByTestId('diff')).toContainText('+text_depth = 1.6;')

    const oldest = versions.locator('li').last()
    await oldest.getByRole('button', { name: 'Restore this version' }).click()

    await expect(versions.locator('li')).toHaveCount(4)
    const head = versions.locator('li').first()
    await expect(head).toContainText('Restore name-keychain to')
    // The panel follows the restore rather than staying on whatever was selected
    // before it — a restore only ADDS a commit, so nothing re-homes it on its own.
    await expect(head.getByRole('button', { pressed: true })).toBeVisible()
  })

  test('customizes an old revision without restoring it', async ({ page }) => {
    await page.goto('/m/name-keychain/versions')
    const versions = page.getByTestId('versions')
    await expect(versions.locator('li')).toHaveCount(3)

    await versions.locator('li').last().getByRole('button', { name: 'Customize this version' }).click()

    await expect(page).toHaveURL(/\/m\/name-keychain\?version=/)
    await expect(page.getByTestId('version-badge')).toContainText('revision')
    await expect(page.getByTestId('bbox-readout')).toBeVisible()

    // Leaving the revision behind puts the customizer back on the current source.
    await page.getByRole('button', { name: 'Back to current' }).click()
    await expect(page.getByTestId('version-badge')).toHaveCount(0)
  })

  test('diffs a revision against one that is not its parent', async ({ page }) => {
    await page.goto('/m/name-keychain/versions')
    await expect(page.getByTestId('diff')).toBeVisible()

    await page.getByLabel('Compare with').selectOption({ index: 2 })

    await expect(page.getByTestId('diff')).toContainText('+text_depth = 1.6;')
    await expect(page.getByTestId('diff')).toContainText('Two-colour keychain with raised text.')
  })
})
