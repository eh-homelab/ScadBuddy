import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { UnknownSpoolTray } from '../../api/unknownTrays'
import { listUnknownTrays } from '../../api/unknownTrays'
import { spoolId } from '../../mocks/features/filament/seed'
import { seedUnknownTray } from '../../mocks/features/filament/unknownTrays'
import { resetMockState } from '../../mocks/handlers'
import { server } from '../../mocks/server'
import { NeedsYouContext, type NeedsYou } from '../../lib/needsYou'
import { renderPage } from '../../test/utils'
import { UnknownSpoolDialog } from './UnknownSpoolDialog'

const RESOLVE = '/api/v1/filament/unknown-trays/:id/resolve'

/** The bodies of every resolve request, read as they leave. */
function recorded() {
  const bodies: unknown[] = []
  server.events.on('request:start', ({ request }) => {
    if (request.method === 'POST' && /\/unknown-trays\/[^/]+\/resolve$/.test(new URL(request.url).pathname)) void request.clone().json().then((body) => bodies.push(body), () => {})
  })
  return bodies
}

const problem = (status: number, code: string | null, detail: string) =>
  HttpResponse.json({ title: detail, status, detail, ...(code ? { code } : {}) }, { status, headers: { 'Content-Type': 'application/problem+json' } })

async function show(change: Partial<UnknownSpoolTray> = {}) {
  seedUnknownTray()
  const listed = (await listUnknownTrays()).trays[0]!
  const tray = { ...listed, ...change }
  const refresh = vi.fn()
  const onResolved = vi.fn()
  const onOpenChange = vi.fn()
  const needs: NeedsYou = { items: [], more: 0, refresh, openSession: null }
  renderPage(
    <NeedsYouContext.Provider value={needs}>
      <UnknownSpoolDialog tray={tray} open onOpenChange={onOpenChange} onResolved={onResolved} />
    </NeedsYouContext.Provider>,
  )
  const dialog = await screen.findByRole('dialog', { name: 'Unknown spool in 3DP-31B-598 AMS-A slot 2' })
  return { tray, dialog, refresh, onResolved, onOpenChange }
}

describe('UnknownSpoolDialog (#7143)', () => {
  beforeEach(() => resetMockState())

  it('links the picked candidate with the typed weight, and sends seen_at as it was listed', async () => {
    const posts = recorded()
    const { tray, dialog, onResolved } = await show()
    // The seeded value has microseconds, which a Date round trip would cut to milliseconds.
    expect(new Date(tray.seen_at).toISOString()).not.toBe(tray.seen_at)
    await userEvent.click(within(dialog).getByRole('radio', { name: /S-9\b/ }))
    await userEvent.type(within(dialog).getByLabelText('Net weight (g)'), '980')
    await userEvent.click(within(dialog).getByRole('button', { name: 'Link spool' }))
    await waitFor(() => expect(onResolved).toHaveBeenCalledTimes(1))
    expect(posts).toEqual([{ spool_id: spoolId(9), net_weight_g: 980, seen_at: tray.seen_at }])
    expect((posts[0] as { seen_at: string }).seen_at).toBe('2026-11-26T09:30:00.123456Z')
    expect(onResolved.mock.calls[0]?.[0]).toMatchObject({ id: spoolId(9), number: 9, code: 'S-9' })
  })

  it('takes a scanned label URL as a code', async () => {
    const posts = recorded()
    const { dialog, onResolved } = await show()
    const code = within(dialog).getByLabelText('Spool code')
    await userEvent.type(code, 'https://x/x/S-1')
    expect(code).toHaveValue('S-1')
    await userEvent.type(within(dialog).getByLabelText('Net weight (g)'), '640')
    await userEvent.click(within(dialog).getByRole('button', { name: 'Link spool' }))
    await waitFor(() => expect(onResolved).toHaveBeenCalledTimes(1))
    expect(posts).toHaveLength(1)
    expect(posts[0]).toMatchObject({ code: 'S-1', net_weight_g: 640 })
  })

  it('takes a pasted label URL as a code', async () => {
    const { dialog } = await show()
    const code = within(dialog).getByLabelText('Spool code')
    await userEvent.click(code)
    await userEvent.paste('https://sb.example/x/s-142')
    expect(code).toHaveValue('S-142')
  })

  it('fills the weight from the remain percent when a candidate is picked and the field is empty', async () => {
    const { dialog } = await show({ remain_pct: 50 })
    // Spool #6 has a label weight of 1000 g.
    await userEvent.click(within(dialog).getByRole('radio', { name: /S-6\b/ }))
    expect(within(dialog).getByLabelText('Net weight (g)')).toHaveValue('500')
    // A weight the person typed stays.
    await userEvent.clear(within(dialog).getByLabelText('Net weight (g)'))
    await userEvent.type(within(dialog).getByLabelText('Net weight (g)'), '480')
    await userEvent.click(within(dialog).getByRole('radio', { name: /S-9\b/ }))
    expect(within(dialog).getByLabelText('Net weight (g)')).toHaveValue('480')
  })

  it('says when the tray changed, and reads the list again', async () => {
    server.use(http.post(RESOLVE, () => problem(409, 'tray_changed', 'the tray changed')))
    const { dialog, refresh } = await show()
    await userEvent.click(within(dialog).getByRole('radio', { name: /S-9\b/ }))
    await userEvent.type(within(dialog).getByLabelText('Net weight (g)'), '980')
    await userEvent.click(within(dialog).getByRole('button', { name: 'Link spool' }))
    expect(await within(dialog).findByRole('alert')).toHaveTextContent('This tray changed since the dialog opened. Close it and open it again.')
    expect(refresh).toHaveBeenCalled()
  })

  it('says when no spool has the code', async () => {
    const { dialog, onResolved } = await show()
    await userEvent.type(within(dialog).getByLabelText('Spool code'), 'S-999')
    await userEvent.type(within(dialog).getByLabelText('Net weight (g)'), '500')
    await userEvent.click(within(dialog).getByRole('button', { name: 'Link spool' }))
    expect(await within(dialog).findByRole('alert')).toHaveTextContent('No spool has the code S-999.')
    expect(onResolved).not.toHaveBeenCalled()
  })

  it('says when the server is not ready to link, and when it has no such route', async () => {
    server.use(http.post(RESOLVE, () => problem(503, 'resolve_pending', 'not served yet')))
    const { dialog } = await show()
    await userEvent.type(within(dialog).getByLabelText('Spool code'), 'S-1')
    await userEvent.type(within(dialog).getByLabelText('Net weight (g)'), '500')
    await userEvent.click(within(dialog).getByRole('button', { name: 'Link spool' }))
    expect(await within(dialog).findByRole('alert')).toHaveTextContent('ScadBuddy could not finish linking the spool. Try again in a moment.')
    server.use(http.post(RESOLVE, () => problem(404, null, 'Not Found')))
    await userEvent.click(within(dialog).getByRole('button', { name: 'Link spool' }))
    await waitFor(() => expect(within(dialog).getByRole('alert')).toHaveTextContent('This server cannot link spools yet.'))
  })

  it('needs a weight', async () => {
    const { dialog } = await show()
    const link = within(dialog).getByRole('button', { name: 'Link spool' })
    await userEvent.type(within(dialog).getByLabelText('Spool code'), 'S-1')
    expect(link).toBeDisabled()
    await userEvent.type(within(dialog).getByLabelText('Net weight (g)'), '500')
    expect(link).toBeEnabled()
  })

  it('links to the invoices page and dismisses a tray that is gone', async () => {
    const { dialog, onOpenChange, refresh } = await show()
    expect(within(dialog).getByRole('link', { name: 'Add from an invoice' })).toHaveAttribute('href', '/filament/invoices')
    expect(within(dialog).getByText('Only for a slot that is no longer there; if a spool is still loaded, it comes back on the next report.')).toBeInTheDocument()
    await userEvent.click(within(dialog).getByRole('button', { name: 'Dismiss' }))
    await waitFor(() => expect(onOpenChange).toHaveBeenCalledWith(false))
    expect(refresh).toHaveBeenCalled()
    expect((await listUnknownTrays()).trays).toEqual([])
  })
})
