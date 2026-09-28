import { expect, test } from '@playwright/test'

test.describe('assistant panel (#256)', () => {
  // Driven by the scripted mock agent (src/mocks/agent.ts), which only the mocked
  // build has; against a real stack the agent service (#255) is not deployed yet.
  test.skip(!!process.env.E2E_BASE_URL, 'mock-agent-backed')

  test('streams a reply, shows a tool call, and waits for approval of an outward step', async ({
    page,
  }) => {
    await page.goto('/m/name-keychain')

    await page.getByRole('button', { name: 'Assistant' }).click()
    const panel = page.getByRole('complementary', { name: 'Assistant' })
    const composer = panel.getByRole('textbox', { name: 'Message the assistant' })
    await expect(composer).toBeFocused()

    await composer.fill('Make the name bigger and send two to Bambuddy')
    await composer.press('Enter')

    const log = panel.getByRole('log', { name: 'Conversation' })
    await expect(log.getByText('Make the name bigger and send two to Bambuddy')).toBeVisible()
    await expect(log.getByText(/so it reads from across the room/)).toBeVisible()

    const write = panel.getByTestId('agent-tool').first()
    await expect(write).toContainText('set_parameters')
    await expect(write).toContainText('write')
    await expect(write).toContainText('Set text_size to 14 mm')
    await write.getByText(/^Why\?/).click()
    await expect(write.getByRole('link', { name: 'OpenSCAD customizer parameters' })).toBeVisible()

    const card = panel.getByRole('region', { name: 'Needs your approval' })
    await expect(card).toContainText('Send name-keychain to Bambuddy project "Keychains", 2 copies?')
    await expect(panel.getByTestId('agent-status')).toHaveText('Waiting for approval')
    await expect(panel.getByText('Queued 2 copies in the Keychains project.')).toHaveCount(0)

    await card.getByRole('button', { name: 'Approve' }).click()
    await expect(panel.getByText('Queued 2 copies in the Keychains project.')).toBeVisible()
    await expect(log.getByText('Sent. Two copies are in the queue.')).toBeVisible()
    await expect(card).toContainText('Approved by You.')
    await expect(panel.getByTestId('agent-status')).toHaveText('Idle')
  })

  test('toggles with Ctrl+` and keeps the transcript while closed', async ({ page }) => {
    await page.goto('/')
    // The app mounts after the msw worker starts; the shortcut listener with it.
    await expect(page.getByRole('button', { name: 'Assistant' })).toBeVisible()
    await page.keyboard.press('Control+Backquote')
    const panel = page.getByRole('complementary', { name: 'Assistant' })
    await expect(panel).toBeVisible()
    await panel.getByRole('button', { name: 'Find a model for a name tag' }).click()
    await expect(panel.getByText(/so it reads from across the room/)).toBeVisible()

    await page.keyboard.press('Control+Backquote')
    await expect(panel).toBeHidden()
    await page.keyboard.press('Control+Backquote')
    await expect(panel.getByText(/so it reads from across the room/)).toBeVisible()
  })
})
