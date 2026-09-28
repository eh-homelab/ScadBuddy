import { expect, test } from '@playwright/test'

test.describe('template presets', () => {
  test.skip(
    !!process.env.E2E_BASE_URL,
    'msw-backed; the real stack is covered by real-backend.spec.ts',
  )

  test('applies a preset, changes one value and saves the result as a new preset', async ({
    page,
  }) => {
    await page.goto('/m/name-keychain')
    const preset = page.getByLabel('Preset')
    const name = page.getByRole('textbox', { name: 'Name on the tag' })

    await preset.selectOption({ label: 'Mum' })
    await expect(name).toHaveValue('Mum')

    await name.fill('Dad')
    await expect(page.getByTestId('preset-modified')).toHaveText('Changed from Mum')

    await page.getByRole('button', { name: 'Save as preset…' }).click()
    const dialog = page.getByRole('dialog', { name: 'Save as preset' })
    await dialog.getByLabel('Preset name').fill('Dad')
    await dialog.getByRole('button', { name: 'Save' }).click()

    await expect(dialog).toBeHidden()
    await expect(preset.locator('option:checked')).toHaveText('Dad')
    await expect(page.getByTestId('preset-modified')).toHaveCount(0)
  })
})

test.describe('duplicating a preset', () => {
  test.skip(
    !!process.env.E2E_BASE_URL,
    'msw-backed; the real stack is covered by real-backend.spec.ts',
  )

  test('copies a read-only preset the template ships, and the copy can be updated', async ({
    page,
  }) => {
    await page.goto('/m/name-keychain')
    const preset = page.getByLabel('Preset')
    await preset.selectOption({ label: 'Tiny' })
    await expect(page.getByRole('button', { name: 'Update' })).toHaveCount(0)

    await page.getByRole('button', { name: 'Duplicate preset Tiny' }).click()
    const dialog = page.getByRole('dialog', { name: 'Duplicate Tiny' })
    await expect(dialog.getByLabel('Preset name')).toHaveValue('Tiny copy')
    await dialog.getByRole('button', { name: 'Duplicate' }).click()

    await expect(dialog).toBeHidden()
    await expect(preset.locator('option:checked')).toHaveText('Tiny copy')
    await page.getByRole('textbox', { name: 'Name on the tag' }).fill('Bo')
    await page.getByRole('button', { name: 'Update' }).click()
    await expect(page.getByTestId('preset-modified')).toHaveCount(0)
  })
})
