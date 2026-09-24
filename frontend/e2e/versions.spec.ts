import { expect, test } from '@playwright/test'

test.describe('model versions', () => {
  // msw-backed, like the other UI specs: the real repository is covered by the
  // backend's own tests, which run a real git in a tmp dir.
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
    await expect(versions.locator('li').first()).toContainText('Restore name-keychain to')
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
