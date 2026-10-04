import { expect, test } from '@playwright/test'

test.describe('assistant panel (#256)', () => {
  // Driven by the scripted mock agent (src/mocks/agent.ts), which only the mocked
  // build has. The real agent is real-agent.spec.ts.
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

  // #940: the agent asks a structured question: a draft to approve, or to edit.
  test('a draft to approve: Edit… returns the edited text, and the turn waits for it', async ({ page }) => {
    await page.goto('/m/name-keychain')
    await page.getByRole('button', { name: 'Assistant' }).click()
    const panel = page.getByRole('complementary', { name: 'Assistant' })
    const composer = panel.getByRole('textbox', { name: 'Message the assistant' })
    await composer.fill('Draft an issue about the thin name text')
    await composer.press('Enter')

    const card = panel.getByRole('region', { name: 'A question for you' })
    await expect(card.getByRole('heading', { name: 'Name text too thin' })).toBeVisible()
    await expect(panel.getByTestId('agent-status')).toHaveText('Waiting for input')
    const send = card.getByRole('button', { name: 'Send answer' })
    await expect(send).toBeDisabled()

    await card.getByRole('radio', { name: 'Edit…' }).check()
    const box = card.getByRole('textbox', { name: 'Your answer' })
    await expect(box).toHaveValue(/At \*\*10 mm\*\* the letters break off/)
    await box.fill('## Name text breaks off below 12 mm')
    await send.click()

    const log = panel.getByRole('log', { name: 'Conversation' })
    await expect(log.getByText('Updated the draft: "## Name text breaks off below 12 mm".')).toBeVisible()
    await expect(card.getByRole('status')).toHaveText('Answered by You: ## Name text breaks off below 12 mm')
    await expect(card.getByRole('radio')).toHaveCount(0)
    await expect(panel.getByTestId('agent-status')).toHaveText('Idle')
  })

  test('a draft to approve: Cancel declines it, the draft hides, and the turn ends', async ({ page }) => {
    await page.goto('/m/name-keychain')
    await page.getByRole('button', { name: 'Assistant' }).click()
    const panel = page.getByRole('complementary', { name: 'Assistant' })
    const composer = panel.getByRole('textbox', { name: 'Message the assistant' })
    await composer.fill('Draft an issue about the thin name text')
    await composer.press('Enter')

    const card = panel.getByRole('region', { name: 'A question for you' })
    await expect(card.getByTestId('agent-question-preview')).toBeVisible()
    await card.getByRole('radio', { name: /Cancel/ }).check()
    await expect(card.getByTestId('agent-question-preview')).toHaveCount(0)
    await card.getByRole('button', { name: 'Send answer' }).click()

    await expect(panel.getByRole('log', { name: 'Conversation' }).getByText("OK, I won't file it.")).toBeVisible()
    await expect(card.getByRole('status')).toHaveText('Answered by You: Cancel')
    await expect(panel.getByTestId('agent-status')).toHaveText('Idle')
  })

  // Spec §8.2: an outward step waits for a human. Deny must send nothing and say so;
  // the card keeps the decision, and its buttons do not come back (#259).
  test('Deny on the confirmation sends nothing and the turn ends', async ({ page }) => {
    await page.goto('/m/name-keychain')
    await page.getByRole('button', { name: 'Assistant' }).click()
    const panel = page.getByRole('complementary', { name: 'Assistant' })
    const composer = panel.getByRole('textbox', { name: 'Message the assistant' })
    await composer.fill('Make the name bigger and send two to Bambuddy')
    await composer.press('Enter')

    const card = panel.getByRole('region', { name: 'Needs your approval' })
    await expect(card).toContainText('Send name-keychain to Bambuddy project "Keychains", 2 copies?')
    await expect(card).toContainText('outward')
    await expect(panel.getByTestId('agent-status')).toHaveText('Waiting for approval')
    // The outward call is shown, but no result for it exists while it waits.
    const send = panel.getByTestId('agent-tool').filter({ hasText: 'print_output' })
    await expect(send).toContainText('outward')
    await expect(panel.getByText('Denied: nothing was sent.')).toHaveCount(0)

    await card.getByRole('button', { name: 'Deny' }).click()
    await expect(card).toContainText('Denied by You.')
    await expect(card.getByRole('button', { name: 'Approve' })).toHaveCount(0)
    await expect(card.getByRole('button', { name: 'Deny' })).toHaveCount(0)
    await expect(panel.getByText('Denied: nothing was sent.')).toBeVisible()
    const log = panel.getByRole('log', { name: 'Conversation' })
    await expect(log.getByText("OK, I didn't send it.")).toBeVisible()
    await expect(panel.getByTestId('agent-status')).toHaveText('Idle')
    await expect(panel.getByText('Queued 2 copies in the Keychains project.')).toHaveCount(0)
  })

  test('Stop while a confirmation waits withdraws it without sending', async ({ page }) => {
    await page.goto('/m/name-keychain')
    await page.getByRole('button', { name: 'Assistant' }).click()
    const panel = page.getByRole('complementary', { name: 'Assistant' })
    const composer = panel.getByRole('textbox', { name: 'Message the assistant' })
    await composer.fill('Make the name bigger and send two to Bambuddy')
    await composer.press('Enter')

    const card = panel.getByRole('region', { name: 'Needs your approval' })
    await expect(card.getByRole('button', { name: 'Approve' })).toBeVisible()
    await panel.getByRole('button', { name: 'Stop', exact: true }).click()

    // An interrupted approval resolves as not approved, with no decider.
    await expect(card).toContainText('Denied.')
    await expect(card.getByRole('button', { name: 'Approve' })).toHaveCount(0)
    await expect(panel.getByTestId('agent-status')).toHaveText('Idle')
    await expect(panel.getByText('Queued 2 copies in the Keychains project.')).toHaveCount(0)
  })

  test('comes out of the customizer’s full screen for Ctrl+`', async ({ page }) => {
    await page.goto('/m/name-keychain')
    await expect(page.getByTestId('bbox-readout')).toBeVisible()
    await page.getByRole('button', { name: 'Full screen', exact: true }).click()
    await expect(page.getByRole('button', { name: 'Exit full screen' })).toBeVisible()
    expect(await page.evaluate('document.fullscreenElement !== null')).toBe(true)

    // Full screen hides the panel with the rest of the page, so the shortcut leaves it.
    await page.keyboard.press('Control+Backquote')
    const panel = page.getByRole('complementary', { name: 'Assistant' })
    await expect(panel.getByRole('textbox', { name: 'Message the assistant' })).toBeFocused()
    await expect(page.getByRole('button', { name: 'Full screen', exact: true })).toBeVisible()
    expect(await page.evaluate('document.fullscreenElement')).toBeNull()
  })

  test('brings back a panel full screen had hidden, focused', async ({ page }) => {
    await page.goto('/m/name-keychain')
    await expect(page.getByTestId('bbox-readout')).toBeVisible()
    await page.getByRole('button', { name: 'Assistant' }).click()
    const panel = page.getByRole('complementary', { name: 'Assistant' })
    const composer = panel.getByRole('textbox', { name: 'Message the assistant' })
    await expect(composer).toBeFocused()

    // Open behind full screen, where the page around the view is inert.
    await page.getByRole('button', { name: 'Full screen', exact: true }).click()
    await expect(page.getByRole('button', { name: 'Exit full screen' })).toBeVisible()
    await page.keyboard.press('Control+Backquote')
    await expect(composer).toBeFocused()
    await expect(page.getByRole('button', { name: 'Full screen', exact: true })).toBeVisible()
    expect(await page.evaluate('document.fullscreenElement')).toBeNull()
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

  // #931: what a session touched, each entry a link to its page.
  test('Touched lists what a session changed and opens its page', async ({ page }) => {
    await page.goto('/')
    await page.getByRole('button', { name: 'Assistant' }).click()
    const panel = page.getByRole('complementary', { name: 'Assistant' })
    await panel.getByRole('button', { name: 'Sessions (1)' }).click()
    await panel.getByRole('button', { name: /Tune the gridfinity bin/ }).click()
    await expect(panel.getByText('Done: the bin is now 3 units (21 mm) tall.')).toBeVisible()

    await panel.getByRole('button', { name: 'Touched' }).click()
    const touched = panel.getByRole('region', { name: 'What this session touched' })
    await expect(touched.getByRole('group', { name: 'Presets' })).toContainText('preset-tall')
    await touched.getByRole('group', { name: 'Revisions' }).getByRole('link', { name: /3f9c2a1/ }).click()
    await expect(page).toHaveURL(/\/m\/gridfinity-bin\?version=3f9c2a1b7d4e$/)
  })
})
