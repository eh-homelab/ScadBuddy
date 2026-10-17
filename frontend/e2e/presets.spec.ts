import { expect, test } from '@playwright/test'
import { choose } from './select'

test.describe('template presets', () => {
  test.skip(
    !!process.env.E2E_BASE_URL,
    'msw-backed; the real stack is covered by real-backend.spec.ts',
  )

  test('applies a preset, changes one value and saves the result as a new preset', async ({
    page,
  }) => {
    await page.goto('/m/name-keychain')
    const preset = page.getByRole('combobox', { name: 'Preset' })
    const name = page.getByRole('textbox', { name: 'Name on the tag' })

    await choose(page, preset, 'Mum')
    await expect(name).toHaveValue('Mum')

    await name.fill('Dad')
    await expect(page.getByTestId('preset-modified')).toHaveText('Changed from Mum')

    await page.getByRole('button', { name: 'Save as preset…' }).click()
    const dialog = page.getByRole('dialog', { name: 'Save as preset' })
    await dialog.getByLabel('Preset name').fill('Dad')
    await dialog.getByRole('button', { name: 'Save' }).click()

    await expect(dialog).toBeHidden()
    await expect(preset).toHaveText('Dad')
    await expect(page.getByTestId('preset-modified')).toHaveCount(0)
  })

  test('a pick over unsaved edits asks once, and Cancel gives focus back (#1457)', async ({
    page,
  }) => {
    // #3672 — a Radix select opens on ArrowDown rather than changing its value per press
    // as Chromium's native one did, so the pick is made from the list.
    await page.goto('/m/name-keychain')
    const preset = page.getByRole('combobox', { name: 'Preset' })
    // Hidden from the accessibility tree while the question is open over it.
    const name = page.getByRole('textbox', { name: 'Name on the tag', includeHidden: true })
    // The list loads before the select takes keys (it is disabled until then).
    await expect(preset).toBeEnabled({ timeout: 20_000 })
    await name.fill('Emmalina')

    await preset.focus()
    await page.keyboard.press('ArrowDown')
    await page.getByRole('listbox').getByRole('option', { name: 'Tiny', exact: true }).click()

    const dialog = page.getByRole('alertdialog')
    await expect(dialog).toHaveCount(1)
    await expect(dialog).toHaveAccessibleName('Apply preset Tiny?')
    await expect(name).toHaveValue('Emmalina')
    await expect(page.getByRole('combobox', { name: 'Preset', includeHidden: true })).toHaveText('Choose a preset…')

    await dialog.getByRole('button', { name: 'Cancel' }).click()
    await expect(dialog).toBeHidden()
    await expect(preset).toBeFocused()
    await expect(preset).toHaveText('Choose a preset…')
    await expect(name).toHaveValue('Emmalina')
  })

  test('Save as preset takes typing at once, keeps Tab inside, and gives focus back (#351)', async ({ page }) => {
    await page.goto('/m/name-keychain')
    const saveAs = page.getByRole('button', { name: 'Save as preset…' })
    await saveAs.focus()
    await page.keyboard.press('Enter')
    const dialog = page.getByRole('dialog', { name: 'Save as preset' })
    const field = dialog.getByLabel('Preset name')
    await expect(field).toBeFocused()
    await page.keyboard.type('Emma')
    await expect(field).toHaveValue(/Emma$/)

    for (let press = 0; press < 12; press += 1) {
      await page.keyboard.press('Tab')
      expect(await dialog.evaluate((panel) => panel.contains(panel.ownerDocument.activeElement))).toBe(true)
    }
    await page.keyboard.press('Escape')
    await expect(dialog).toBeHidden()
    await expect(saveAs).toBeFocused()
  })
})

test.describe('the "Changed from" note (#352)', () => {
  test.skip(
    !!process.env.E2E_BASE_URL,
    'msw-backed; the real stack is covered by real-backend.spec.ts',
  )

  for (const width of [1440, 1024, 390]) {
    test(`stays one line inside the picker at ${width}px`, async ({ page }) => {
      await page.setViewportSize({ width, height: 900 })
      await page.goto('/m/name-keychain')
      await choose(page, page.getByRole('combobox', { name: 'Preset' }), 'Old engraving')
      await page.getByRole('textbox', { name: 'Name on the tag' }).fill('Someone else')

      const note = page.getByTestId('preset-modified')
      await expect(note).toHaveText('Changed from Old engraving')
      const box = await note.boundingBox()
      const picker = await page.getByTestId('preset-picker').boundingBox()
      const update = await page.getByRole('button', { name: 'Update' }).boundingBox()
      if (!box || !picker || !update) throw new Error('not laid out')
      // One line, not a word per line beside the buttons.
      expect(box.height).toBeLessThan(24)
      // Inside the panel: a long name pushed its first letters off the left edge.
      expect(box.x).toBeGreaterThanOrEqual(picker.x)
      expect(box.x + box.width).toBeLessThanOrEqual(picker.x + picker.width)
      // Above the buttons, on its own line.
      expect(box.y + box.height).toBeLessThanOrEqual(update.y)
    })
  }
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
    const preset = page.getByRole('combobox', { name: 'Preset' })
    await choose(page, preset, 'Tiny')
    await expect(page.getByRole('button', { name: /^Update preset / })).toHaveCount(0)

    await page.getByRole('button', { name: 'More for preset Tiny' }).click()
    await page.getByRole('menuitem', { name: 'Duplicate preset Tiny' }).click()
    const dialog = page.getByRole('dialog', { name: 'Duplicate Tiny' })
    await expect(dialog.getByLabel('Preset name')).toHaveValue('Tiny copy')
    await dialog.getByRole('button', { name: 'Duplicate' }).click()

    await expect(dialog).toBeHidden()
    await expect(preset).toHaveText('Tiny copy')
    await page.getByRole('textbox', { name: 'Name on the tag' }).fill('Bo')
    await page.getByRole('button', { name: /^Update preset / }).click()
    await expect(page.getByTestId('preset-modified')).toHaveCount(0)
  })
})

test.describe('a preset\'s details (#327)', () => {
  test.skip(
    !!process.env.E2E_BASE_URL,
    'msw-backed; the real stack is covered by real-backend.spec.ts',
  )

  test('shows a shipped preset\'s details, and a saved one\'s can be edited', async ({ page }) => {
    await page.goto('/m/name-keychain')
    const preset = page.getByRole('combobox', { name: 'Preset' })
    const details = page.getByTestId('preset-details')

    await choose(page, preset, 'Tiny')
    await expect(details.getByRole('listitem')).toHaveText(['small', 'zip pull'])
    await expect(details.locator('strong')).toHaveText('zip pull')

    await choose(page, preset, 'Old engraving')
    await expect(details).toHaveCount(0)
    await page.getByRole('button', { name: 'More for preset Old engraving' }).click()
    await page.getByRole('menuitem', { name: 'Edit details of preset Old engraving' }).click()
    const dialog = page.getByRole('dialog', { name: 'Edit details of Old engraving' })
    await dialog.getByLabel('Preset name').fill('Engraved')
    await dialog.getByLabel('Description (optional, Markdown)').fill('Deep *engraving*.')
    await dialog.getByLabel('Tags (optional, comma-separated)').fill('engraved, Engraved, deep')
    await dialog.getByRole('button', { name: 'Save' }).click()

    await expect(dialog).toBeHidden()
    await expect(preset).toHaveText('Engraved')
    await expect(details.getByRole('listitem')).toHaveText(['engraved', 'deep'])
    await expect(details.locator('em')).toHaveText('engraving')
  })
})
