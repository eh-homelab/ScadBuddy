import { expect, test, type Page } from '@playwright/test'

/**
 * #254 — the whole customize-to-print flow driven only through the in-browser agent
 * bridge, as an agent would: no clicks, no typing. The page is only *read* here, to
 * check that what the bridge did is what a user would see.
 *
 * `window.__scadbuddyBridge` exists in the dev server and in this msw-mocked bundle
 * (`VITE_MOCK_API=1`); a production build does not expose it (`src/agent/index.ts`).
 */

type Outcome =
  | { ok: true; result: unknown }
  | { ok: false; error: { code: string; message: string } }

interface Bridge {
  call: (name: string, args?: unknown) => Promise<Outcome>
  listTools: (options?: { all?: boolean }) => Promise<{ name: string; live: boolean; risk: string }[]>
}

// `page.evaluate` serialises its callback, so the lookup is spelled out inside each one
// rather than shared through a helper that would not exist in the page.
async function call(page: Page, name: string, args: unknown = {}): Promise<Outcome> {
  return page.evaluate(
    ([tool, input]) =>
      (globalThis as unknown as { __scadbuddyBridge: Bridge }).__scadbuddyBridge.call(tool as string, input),
    [name, args] as const,
  )
}

async function liveTools(page: Page): Promise<string[]> {
  return page.evaluate(async () =>
    (await (globalThis as unknown as { __scadbuddyBridge: Bridge }).__scadbuddyBridge.listTools()).map(
      (tool) => tool.name,
    ),
  )
}

async function result<T>(page: Page, name: string, args: unknown = {}): Promise<T> {
  const outcome = await call(page, name, args)
  expect(outcome, `${name} ${JSON.stringify(args)}`).toMatchObject({ ok: true })
  return (outcome as { result: T }).result
}

test.describe('agent bridge', () => {
  test.skip(!!process.env.E2E_BASE_URL, 'msw-backed; the real stack has no mocked bundle')

  test('customizes, generates and opens the send dialog, but cannot send', async ({ page }) => {
    await page.goto('/')
    await page.waitForFunction(() => Boolean((globalThis as { __scadbuddyBridge?: unknown }).__scadbuddyBridge))

    // The catalogue's tools are live; the customizer's answer "unavailable", not nothing.
    await expect.poll(() => liveTools(page)).toContain('search')
    const early = await call(page, 'get_params')
    expect(early).toMatchObject({ ok: false, error: { code: 'unavailable' } })

    const found = await result<{ slug: string }[]>(page, 'search', { query: 'keychain' })
    expect(found.map((model) => model.slug)).toContain('name-keychain')
    await result(page, 'open_model', { slug: 'name-keychain' })

    await expect.poll(() => liveTools(page)).toContain('set_param')
    const first = await result<{ status: string }>(page, 'render', { timeout_ms: 20_000 })
    expect(first.status).toBe('done')

    await result(page, 'set_param', { name: 'name', value: 'Nova' })
    // What the user sees: the field changed, and the agent's touch is shown.
    await expect(page.getByRole('textbox', { name: 'Name on the tag' })).toHaveValue('Nova')
    await expect(page.locator('[data-param="name"]')).toHaveClass(/sb-agent-touch/)

    const rendered = await result<{ status: string; bbox_mm: { size: number[] } }>(page, 'render', {
      timeout_ms: 20_000,
    })
    expect(rendered.status).toBe('done')
    expect(rendered.bbox_mm.size[0]).toBeCloseTo(46.7, 1)
    await expect(page.getByTestId('bbox-readout')).toContainText('46.7')

    const bad = await call(page, 'set_param', { name: 'text_size', value: 99 })
    expect(bad).toMatchObject({ ok: false, error: { code: 'invalid_args' } })

    await result(page, 'generate', { timeout_ms: 20_000 })
    await expect(page.getByText(/^Saved /)).toBeVisible()

    await result(page, 'open_print_dialog', { kind: 'send' })
    const dialog = page.getByRole('dialog', { name: 'Send to Bambuddy' })
    await expect(dialog).toBeVisible()

    // The confirmation is the user's alone.
    const send = await call(page, 'click', { role: 'button', name: 'Send' })
    expect(send).toMatchObject({ ok: false, error: { code: 'refused' } })
    const snapshot = await result<{ dialogs: string[]; elements: { name: string; userOnly?: boolean }[] }>(
      page,
      'snapshot',
    )
    expect(snapshot.dialogs).toEqual(['Send to Bambuddy'])
    expect(snapshot.elements).toContainEqual(expect.objectContaining({ name: 'Send', userOnly: true }))
    await expect(dialog.getByRole('button', { name: 'Send' })).toBeEnabled()
    await expect(dialog.getByText(/Sent|Queued/)).toHaveCount(0)

    // The fallback can still dismiss it.
    await result(page, 'click', { role: 'button', name: 'Cancel' })
    await expect(dialog).toBeHidden()
  })

  test('navigates to settings and edits the form without saving it', async ({ page }) => {
    await page.goto('/')
    await page.waitForFunction(() => Boolean((globalThis as { __scadbuddyBridge?: unknown }).__scadbuddyBridge))

    await result(page, 'navigate', { route: '/settings' })
    await expect.poll(() => liveTools(page)).toContain('set_field')

    const form = await result<{ values: Record<string, string>; unsaved: boolean }>(page, 'get_form')
    expect(form.unsaved).toBe(false)

    await result(page, 'set_field', { field: 'display_unit', value: 'in' })
    await expect(page.getByLabel('Show dimensions in')).toHaveValue('in')
    expect((await result<{ unsaved: boolean }>(page, 'get_form')).unsaved).toBe(true)

    const test = await call(page, 'test_connection')
    expect(test).toMatchObject({ ok: false, error: { code: 'refused' } })
    const save = await call(page, 'click', { role: 'button', name: 'Save Preview' })
    expect(save).toMatchObject({ ok: false, error: { code: 'refused' } })
  })
})
