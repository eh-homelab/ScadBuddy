import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { getSpool } from '../../api/filament'
import { spoolId } from '../../mocks/features/filament/seed'
import { resetMockState } from '../../mocks/handlers'
import { server } from '../../mocks/server'
import { renderPage } from '../../test/utils'
import { ArchiveSpoolDialog } from './ArchiveSpoolDialog'

/** The bodies of every archive request, read as they leave. */
function recorded() {
  const bodies: unknown[] = []
  server.events.on('request:start', ({ request }) => {
    if (request.method === 'POST' && /\/filament\/spools\/[^/]+\/archive$/.test(new URL(request.url).pathname)) void request.clone().json().then((body) => bodies.push(body), () => {})
  })
  return bodies
}

/** Spool #10 is on a cardboard spool (180 g empty) with 250 g left; spool #3 has no insert. */
async function show(number: number, emptyWeightG: number | null) {
  const spool = await getSpool(spoolId(number))
  const onArchived = vi.fn()
  const onOpenChange = vi.fn()
  renderPage(<ArchiveSpoolDialog spool={spool} emptyWeightG={emptyWeightG} open onOpenChange={onOpenChange} onArchived={onArchived} />)
  const dialog = await screen.findByRole('dialog', { name: `Archive spool #${number}` })
  return { spool, dialog, onArchived, onOpenChange }
}

describe('ArchiveSpoolDialog (#7684)', () => {
  beforeEach(() => resetMockState())

  it('needs a reason, previews the closing and archives with the reading', async () => {
    const posts = recorded()
    const { dialog, onArchived } = await show(10, 180)
    const archive = within(dialog).getByRole('button', { name: 'Archive' })
    expect(archive).toBeDisabled()
    await userEvent.click(within(dialog).getByRole('radio', { name: 'Finished' }))
    await userEvent.type(within(dialog).getByLabelText('Scale reading, with the spool (g)'), '210')
    // 210 g on the scale less the 180 g spool: 30 g of filament, over the 5 g a finished spool may keep.
    expect(await within(dialog).findByText('30 g of filament are still on it.')).toBeInTheDocument()
    expect(within(dialog).getByText(/Filament at the end: 30 g\./)).toBeInTheDocument()
    await userEvent.click(archive)
    await waitFor(() => expect(onArchived).toHaveBeenCalledTimes(1))
    expect(posts).toEqual([{ archived: true, reason: 'finished', final_reading_g: 210 }])
    expect(onArchived.mock.calls[0]?.[1]).toBeNull()
    expect((await getSpool(spoolId(10))).archived_at).not.toBeNull()
  })

  it('offers to continue a respooled spool', async () => {
    const { dialog, onArchived } = await show(10, 180)
    expect(within(dialog).queryByRole('button', { name: 'Archive and continue as a new spool' })).toBeNull()
    await userEvent.click(within(dialog).getByRole('radio', { name: 'Respooled' }))
    await userEvent.click(within(dialog).getByRole('button', { name: 'Archive and continue as a new spool' }))
    await waitFor(() => expect(onArchived).toHaveBeenCalledTimes(1))
    // No final weighing: the new spool starts with what the ledger had left.
    expect(onArchived.mock.calls[0]?.[1]).toBe(250)
    expect(onArchived.mock.calls[0]?.[0].archived_at).not.toBeNull()
  })

  it('archives on the ledger alone without a reading, with the filament field when no empty weight is known', async () => {
    const posts = recorded()
    const { dialog, onArchived } = await show(3, null)
    expect(within(dialog).getByLabelText('Filament left (g)')).toBeInTheDocument()
    await userEvent.click(within(dialog).getByRole('radio', { name: 'Damaged' }))
    await userEvent.click(within(dialog).getByRole('button', { name: 'Archive' }))
    await waitFor(() => expect(onArchived).toHaveBeenCalledTimes(1))
    expect(posts).toEqual([{ archived: true, reason: 'damaged' }])
  })

  it('holds the buttons on a reading that is not a number, and shows a refused archive', async () => {
    server.use(
      http.post('*/api/v1/filament/spools/:id/archive', () =>
        HttpResponse.json({ type: 'about:blank', title: 'Unprocessable', status: 422, detail: 'the reading is below the empty weight', code: 'reading_below_empty' }, { status: 422, headers: { 'Content-Type': 'application/problem+json' } }),
      ),
    )
    const { dialog, onArchived } = await show(10, 180)
    await userEvent.click(within(dialog).getByRole('radio', { name: 'Finished' }))
    const field = within(dialog).getByLabelText('Scale reading, with the spool (g)')
    await userEvent.type(field, 'abc')
    expect(field).toHaveAttribute('aria-invalid', 'true')
    expect(within(dialog).getByRole('button', { name: 'Archive' })).toBeDisabled()
    await userEvent.clear(field)
    await userEvent.type(field, '100')
    await userEvent.click(within(dialog).getByRole('button', { name: 'Archive' }))
    expect(await within(dialog).findByRole('alert')).toHaveTextContent('the reading is below the empty weight')
    expect(onArchived).not.toHaveBeenCalled()
  })
})
