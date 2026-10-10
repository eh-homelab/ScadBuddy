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

    // #782: the write and the render view are one group: the view's image shows with it closed.
    const steps = panel.getByRole('button', { name: /2 steps/ })
    await expect(steps).toHaveAttribute('aria-expanded', 'false')
    const image = panel.getByRole('img', { name: 'Look at the render (iso)' })
    await expect(image).toBeVisible()
    expect(await image.evaluate((img) => (img as unknown as { naturalWidth: number }).naturalWidth)).toBe(192)
    await steps.click()
    const write = panel.getByTestId('agent-tool').filter({ hasText: 'Set text_size → 14 mm' })
    await expect(write).toContainText('write')
    await expect(write).toContainText('Done')
    await write.getByText(/^Why\?/).click()
    await expect(write.getByRole('link', { name: 'OpenSCAD customizer parameters' })).toBeVisible()
    // Arguments and the raw result are behind Details.
    await expect(write.getByTestId('agent-tool-arguments')).toBeHidden()
    await write.getByRole('button', { name: 'Details: Set text_size → 14 mm' }).click()
    await expect(write.getByTestId('agent-tool-arguments')).toContainText('"text_size": 14')
    await expect(write.getByTestId('agent-tool-result')).toContainText('Set text_size to 14 mm')

    const card = panel.getByRole('region', { name: 'Needs your approval' })
    await expect(card).toContainText('Send name-keychain to Bambuddy project "Keychains", 2 copies?')
    await expect(panel.getByTestId('agent-status')).toHaveText('Waiting for approval')
    await expect(panel.getByText('Queued 2 copies in the Keychains project.')).toHaveCount(0)

    const send = panel.getByTestId('agent-tool').filter({ hasText: 'Print name-keychain × 2' })
    await expect(send.getByTestId('agent-tool-status')).toHaveText('Waiting for approval')
    await card.getByRole('button', { name: 'Approve' }).click()
    await expect(send.getByTestId('agent-tool-status')).toHaveText('Done')
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
    const send = panel.getByTestId('agent-tool').filter({ hasText: 'Print name-keychain × 2' })
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

    // An interrupted approval is cancelled, not denied: nobody decided it (#979).
    await expect(card).toContainText('Cancelled: interrupted by You.')
    await expect(card).not.toContainText('Denied')
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

  // #792: fork from a reply by keyboard, the parent link, the fork nested under its
  // parent in Sessions, and a rename.
  test('forks a chat from a reply, nests the fork under it, and renames it', async ({ page }) => {
    await page.goto('/m/name-keychain')
    await page.getByRole('button', { name: 'Assistant' }).click()
    const panel = page.getByRole('complementary', { name: 'Assistant' })
    await panel.getByRole('button', { name: 'Sessions (1)' }).click()
    await panel.getByRole('button', { name: /^Tune the gridfinity bin/ }).click()
    const log = panel.getByRole('log', { name: 'Conversation' })
    const reply = log.locator('[data-feed-item="assistant"]').filter({ hasText: 'Done: the bin is now 3 units' })

    // Shown on hover, and to the keyboard when it has focus.
    const forkHere = reply.getByRole('button', { name: 'Fork from here' })
    await forkHere.focus()
    await expect(forkHere).toHaveCSS('opacity', '1')
    await page.keyboard.press('Enter')
    await expect(panel.getByTestId('active-session-title')).toHaveText('Tune the gridfinity bin (fork)')
    await expect(log.getByText('Done: the bin is now 3 units (21 mm) tall.')).toBeVisible()
    await expect(panel.getByRole('textbox', { name: 'Message the assistant' })).toBeEnabled()

    await panel.getByRole('button', { name: 'Sessions (2)' }).click()
    const sessions = panel.getByRole('navigation', { name: 'Sessions' })
    const forks = sessions.getByRole('list', { name: 'Forks of Tune the gridfinity bin' })
    await forks.getByRole('button', { name: 'Rename Tune the gridfinity bin (fork)' }).click()
    const title = sessions.getByRole('textbox', { name: 'Chat title' })
    await title.fill('Bin, taller')
    await title.press('Enter')
    await expect(forks.getByRole('button', { name: /^Bin, taller/ })).toBeVisible()
    await expect(panel.getByTestId('active-session-title')).toHaveText('Bin, taller')

    await panel.getByRole('button', { name: 'Forked from Tune the gridfinity bin' }).click()
    await expect(panel.getByTestId('active-session-title')).toHaveText('Tune the gridfinity bin')
  })
})

test.describe('the assistant beside a dialog (#798)', () => {
  test.skip(!!process.env.E2E_BASE_URL, 'mock-agent-backed')

  test('stays visible and usable while the Print dialog is open', async ({ page }) => {
    // Narrow enough that a dialog centred in the whole window would run under the panel.
    await page.setViewportSize({ width: 1100, height: 800 })
    await page.goto('/m/name-keychain')
    await page.getByRole('button', { name: 'Assistant' }).click()
    const panel = page.getByRole('complementary', { name: 'Assistant' })
    const composer = panel.getByRole('textbox', { name: 'Message the assistant' })
    await expect(composer).toBeFocused()

    await expect(page.getByTestId('generate')).toBeEnabled()
    await page.getByTestId('generate').click()
    await expect(page.getByTestId('print')).toBeEnabled()
    await page.getByTestId('print').click()
    const dialog = page.getByRole('dialog', { name: 'Print' })
    await expect(dialog).toBeVisible()

    // Side by side, not on top of each other, and nothing covers the chat.
    const dialogBox = await dialog.boundingBox()
    const panelBox = await panel.boundingBox()
    if (!dialogBox || !panelBox) throw new Error('not laid out')
    expect(dialogBox.x + dialogBox.width).toBeLessThanOrEqual(panelBox.x)
    const composerBox = await composer.boundingBox()
    if (!composerBox) throw new Error('composer not laid out')
    const [x, y] = [composerBox.x + composerBox.width / 2, composerBox.y + composerBox.height / 2]
    expect(await page.evaluate(`document.elementFromPoint(${x}, ${y})?.id`)).toBe('assistant-composer')

    await composer.click()
    await composer.fill('Which spool is the grey one?')
    await expect(composer).toHaveValue('Which spool is the grey one?')
    await expect(dialog).toBeVisible()
  })

  // #1897 — a click on the toggle closed the dialog instead: the dialog's overlay covered
  // it. The toggle is above the overlay now.
  test('opens from its button while the Print dialog is open', async ({ page }) => {
    await page.setViewportSize({ width: 1100, height: 800 })
    await page.goto('/m/name-keychain')
    await expect(page.getByTestId('generate')).toBeEnabled()
    await page.getByTestId('generate').click()
    await expect(page.getByTestId('print')).toBeEnabled()
    await page.getByTestId('print').click()
    const dialog = page.getByRole('dialog', { name: 'Print' })
    await expect(dialog).toBeVisible()

    // A real click: Playwright refuses one on a button the overlay covers, and a click
    // on the overlay closes the dialog.
    await page.getByRole('button', { name: 'Assistant' }).click()
    const panel = page.getByRole('complementary', { name: 'Assistant' })
    const composer = panel.getByRole('textbox', { name: 'Message the assistant' })
    await expect(composer).toBeFocused()
    await expect(dialog).toBeVisible()

    // Then as when it was open first: side by side, and the chat takes typing.
    const dialogBox = await dialog.boundingBox()
    const panelBox = await panel.boundingBox()
    if (!dialogBox || !panelBox) throw new Error('not laid out')
    expect(dialogBox.x + dialogBox.width).toBeLessThanOrEqual(panelBox.x)
    await composer.fill('Which spool is the grey one?')
    await expect(composer).toHaveValue('Which spool is the grey one?')
    await expect(dialog).toBeVisible()

    // Clicked again beside the dialog, it brings the focus back to the chat rather than
    // closing it (AppShell's `open && besideDialog`): the click itself took the focus.
    await dialog.getByRole('button').first().focus()
    await page.getByRole('button', { name: 'Assistant', exact: true }).click()
    await expect(composer).toBeFocused()
    await expect(panel).toBeVisible()
    await expect(dialog).toBeVisible()
  })
})

// #795 — a fork's row, with Rename and Done beside it, fits the panel at phone width.
test.describe('the session switcher at 390 px (#795)', () => {
  test.skip(!!process.env.E2E_BASE_URL, 'mock-agent-backed')
  test.use({ viewport: { width: 390, height: 844 } })

  test('keeps a fork row and its actions on screen', async ({ page }) => {
    await page.goto('/m/name-keychain')
    await page.getByRole('button', { name: 'Assistant' }).click()
    const panel = page.getByRole('complementary', { name: 'Assistant' })
    await panel.getByRole('button', { name: 'Sessions (1)' }).click()
    await panel.getByRole('button', { name: /^Tune the gridfinity bin/ }).click()
    await panel.getByRole('button', { name: 'Fork', exact: true }).click()
    await expect(panel.getByTestId('active-session-title')).toHaveText('Tune the gridfinity bin (fork)')
    await panel.getByRole('button', { name: 'Sessions (2)' }).click()

    const sessions = panel.getByRole('navigation', { name: 'Sessions' })
    for (const name of ['Rename Tune the gridfinity bin (fork)', 'Mark Tune the gridfinity bin (fork) done']) {
      const box = (await sessions.getByRole('button', { name }).boundingBox())!
      expect(box.x + box.width, name).toBeLessThanOrEqual(390)
    }
    expect(await page.evaluate('document.documentElement.scrollWidth')).toBeLessThanOrEqual(390)
  })
})

// #1038 — at 768 px the header's buttons outgrew the 380 px panel once the session count
// reached two digits, and Close sat past the viewport's edge.
test.describe('assistant panel header at 768 px (#1038)', () => {
  test.skip(!!process.env.E2E_BASE_URL, 'mock-agent-backed')
  test.use({ viewport: { width: 768, height: 1024 } })

  test('keeps every button inside the panel however many sessions there are', async ({ page }) => {
    await page.goto('/m/name-keychain')
    await page.getByRole('button', { name: 'Assistant' }).click()
    const panel = page.getByRole('complementary', { name: 'Assistant' })
    const sessions = panel.getByRole('button', { name: /^Sessions \(/ })
    await expect(sessions).toBeVisible()
    // The mock agent seeds a handful of sessions; a long-lived install has hundreds. Grow
    // the label in place to that width rather than starting a hundred chats.
    await sessions.evaluate((el) => {
      el.textContent = 'Sessions (999)'
    })

    const edge = (await panel.boundingBox())!
    for (const button of await panel.locator('header').getByRole('button').all()) {
      const box = (await button.boundingBox())!
      expect(box.x + box.width, await button.innerText()).toBeLessThanOrEqual(edge.x + edge.width + 0.5)
    }
    expect(await page.evaluate('document.documentElement.scrollWidth')).toBeLessThanOrEqual(768)
  })
})
