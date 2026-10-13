import { expect, test } from '@playwright/test'

// #1920 — the composer's "/" skill menu, against the msw plugin list (src/mocks/aiPlugins.ts),
// at a phone's width.
test.describe('the "/" skill menu at 390 px (#1920)', () => {
  test.skip(!!process.env.E2E_BASE_URL, 'mock-agent-backed')
  test.use({ viewport: { width: 390, height: 844 } })

  test('filters, moves with the keys, inserts a skill without sending, and fits the screen', async ({ page }) => {
    await page.goto('/m/name-keychain')
    await page.getByRole('button', { name: 'Assistant' }).click()
    const panel = page.getByRole('complementary', { name: 'Assistant' })
    const composer = panel.getByRole('textbox', { name: 'Message the assistant' })
    await expect(composer).toBeFocused()

    await composer.pressSequentially('/')
    const list = panel.getByRole('listbox', { name: 'Skills' })
    await expect(list.getByRole('option')).toHaveText(['/scadbuddy:authoring', '/scadbuddy:customize', '/scadbuddy:print'])
    await expect(composer).toHaveAttribute('aria-controls', (await list.getAttribute('id'))!)

    for (const option of await list.getByRole('option').all()) {
      const box = (await option.boundingBox())!
      expect(box.x, 'option starts on screen').toBeGreaterThanOrEqual(0)
      expect(box.x + box.width, 'option ends on screen').toBeLessThanOrEqual(390)
      expect(box.height, 'option is a 24 px tap target').toBeGreaterThanOrEqual(24)
    }
    expect(await page.evaluate('document.documentElement.scrollWidth')).toBeLessThanOrEqual(390)

    await composer.pressSequentially('c')
    await expect(list.getByRole('option')).toHaveText(['/scadbuddy:customize'])
    await composer.press('Backspace')
    await composer.press('ArrowDown')
    await expect(list.getByRole('option', { name: '/scadbuddy:customize' })).toHaveAttribute('aria-selected', 'true')
    await expect(panel.getByRole('status', { name: 'Skill suggestions' })).toHaveText('/scadbuddy:customize, 2 of 3')

    await composer.press('Enter')
    await expect(composer).toHaveValue('/scadbuddy:customize ')
    await expect(list).toBeHidden()
    await expect(composer).toBeFocused()
    await expect(panel.getByRole('log', { name: 'Conversation' }).getByText('/scadbuddy:customize')).toHaveCount(0)

    await composer.press('Control+a')
    await composer.pressSequentially('/pr')
    await expect(list.getByRole('option')).toHaveText(['/scadbuddy:print'])
    await composer.press('Escape')
    await expect(list).toBeHidden()
    await expect(composer).toHaveValue('/pr')
    await expect(panel).toBeVisible()
  })
})
