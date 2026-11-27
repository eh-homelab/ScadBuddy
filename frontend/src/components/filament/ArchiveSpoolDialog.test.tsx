import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { delay, http, HttpResponse } from 'msw'
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

/** A preview that finds no empty weight for the spool, whatever the page believed. */
const noEmptyWeightPreview = () => HttpResponse.json({ closing: { reason: null, started_g: 1000, started_from: 'reading', ledger_consumed_g: 750, final_filament_g: null, scale_consumed_g: null, drift_g: null }, final_filament_g: null, empty_g: null, notices: [] })

/** The no-empty-weight preview, its first answer (the dialog's request at mount) held for 2 s, the rest at once. */
function slowFirstPreview() {
  let calls = 0
  return async () => {
    calls += 1
    if (calls === 1) await delay(2000)
    return noEmptyWeightPreview()
  }
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

  it('takes the empty weight the preview answers when the page knows none', async () => {
    // The page knows no insert, but the spool's effective empty weight (its cardboard spool's 180 g) is the preview's.
    const posts = recorded()
    const { dialog, onArchived } = await show(10, null)
    const reading = await within(dialog).findByLabelText('Scale reading, with the spool (g)')
    expect(within(dialog).getByText('The empty spool weighs 180 g; it is taken off the reading.')).toBeInTheDocument()
    await userEvent.click(within(dialog).getByRole('radio', { name: 'Finished' }))
    await userEvent.type(reading, '210')
    await userEvent.click(within(dialog).getByRole('button', { name: 'Archive' }))
    await waitFor(() => expect(onArchived).toHaveBeenCalledTimes(1))
    expect(posts).toEqual([{ archived: true, reason: 'finished', final_reading_g: 210 }])
  })

  it('says so when the preview changes what a typed number means (#7909)', async () => {
    // The page believes the spool is on a 180 g insert; the preview finds no empty weight, so the number is the filament
    // left. The note needs the number typed before the preview's first answer flips the label: the preview the dialog
    // asks at mount (300 ms after it opens) is held for 2 s, long enough for the typing on a loaded machine, and the hook
    // drops that answer anyway once the typed number asks again; later previews answer at once (the review of #7931).
    server.use(http.get('*/api/v1/filament/spools/:id/closing', slowFirstPreview()))
    const posts = recorded()
    const { dialog, onArchived } = await show(3, 180)
    const field = within(dialog).getByLabelText('Scale reading, with the spool (g)')
    await userEvent.type(field, '210')
    expect(await within(dialog).findByText('No empty weight is known for this spool after all: the number is read as the filament left, not as a scale reading.', undefined, { timeout: 3000 })).toBeInTheDocument()
    expect(within(dialog).getByLabelText('Filament left (g)')).toBe(field)
    await userEvent.click(within(dialog).getByRole('radio', { name: 'Damaged' }))
    await userEvent.click(within(dialog).getByRole('button', { name: 'Archive' }))
    await waitFor(() => expect(onArchived).toHaveBeenCalledTimes(1))
    expect(posts).toEqual([{ archived: true, reason: 'damaged', final_grams: 210 }])
  })

  it('drops the note once the number is replaced under the changed label (#7930)', async () => {
    server.use(http.get('*/api/v1/filament/spools/:id/closing', slowFirstPreview()))
    const { dialog } = await show(3, 180)
    const field = within(dialog).getByLabelText('Scale reading, with the spool (g)')
    await userEvent.type(field, '210')
    await within(dialog).findByText(/after all/, undefined, { timeout: 3000 })
    // A select-all and a keystroke replace the number in one change: it is typed under the label the field has now.
    await userEvent.tripleClick(field)
    await userEvent.keyboard('5')
    expect(field).toHaveValue('5')
    expect(within(dialog).queryByText(/after all/)).toBeNull()
  })

  it('drops the note when the number replaced starts like the old one (#7945)', async () => {
    server.use(http.get('*/api/v1/filament/spools/:id/closing', slowFirstPreview()))
    const { dialog } = await show(3, 180)
    const field = within(dialog).getByLabelText('Scale reading, with the spool (g)')
    await userEvent.type(field, '210')
    await within(dialog).findByText(/after all/, undefined, { timeout: 3000 })
    // The first keystroke of the replacement, "210" to "2", looks like a shortening by the text alone:
    // what was selected decides, not the text.
    await userEvent.tripleClick(field)
    await userEvent.keyboard('250')
    expect(field).toHaveValue('250')
    expect(within(dialog).queryByText(/after all/)).toBeNull()
  })

  it('keeps the note for a change after a select-all that changed nothing (#7945)', async () => {
    server.use(http.get('*/api/v1/filament/spools/:id/closing', slowFirstPreview()))
    const { dialog } = await show(3, 180)
    const field = within(dialog).getByLabelText<HTMLInputElement>('Scale reading, with the spool (g)')
    await userEvent.type(field, '210')
    await within(dialog).findByText(/after all/, undefined, { timeout: 3000 })
    // A select-all and a Shift press change nothing; the caret then moves, and an edit that fires no
    // keydown or paste (a drop, a menu's Undo) is an edit of the old number, not a replacement.
    await userEvent.tripleClick(field)
    await userEvent.keyboard('{Shift}')
    field.setSelectionRange(3, 3)
    fireEvent.select(field)
    fireEvent.change(field, { target: { value: '2100' } })
    expect(field).toHaveValue('2100')
    expect(within(dialog).getByText(/after all/)).toBeInTheDocument()
  })

  it('drops the note when a shorter number is pasted over the old one (#7945)', async () => {
    server.use(http.get('*/api/v1/filament/spools/:id/closing', slowFirstPreview()))
    const { dialog } = await show(3, 180)
    const field = within(dialog).getByLabelText('Scale reading, with the spool (g)')
    await userEvent.type(field, '210')
    await within(dialog).findByText(/after all/, undefined, { timeout: 3000 })
    await userEvent.tripleClick(field)
    await userEvent.paste('21')
    expect(field).toHaveValue('21')
    expect(within(dialog).queryByText(/after all/)).toBeNull()
  })

  it('says nothing about a number typed once the label had already changed (#7909)', async () => {
    server.use(http.get('*/api/v1/filament/spools/:id/closing', () => noEmptyWeightPreview()))
    const { dialog } = await show(3, 180)
    const field = await within(dialog).findByLabelText('Filament left (g)')
    await userEvent.type(field, '210')
    await waitFor(() => expect(within(dialog).getByText(/Filament at the end/)).toBeInTheDocument())
    expect(within(dialog).queryByText(/after all/)).toBeNull()
  })

  it('starts afresh each time it opens', async () => {
    const spool = await getSpool(spoolId(10))
    const noop = vi.fn()
    const view = renderPage(<ArchiveSpoolDialog spool={spool} emptyWeightG={180} open onOpenChange={noop} onArchived={noop} />)
    const dialog = await screen.findByRole('dialog', { name: 'Archive spool #10' })
    await userEvent.click(within(dialog).getByRole('radio', { name: 'Damaged' }))
    await userEvent.type(within(dialog).getByLabelText('Scale reading, with the spool (g)'), '210')
    expect(within(dialog).getByRole('button', { name: 'Archive' })).toBeEnabled()
    view.rerender(<ArchiveSpoolDialog spool={spool} emptyWeightG={180} open={false} onOpenChange={noop} onArchived={noop} />)
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    view.rerender(<ArchiveSpoolDialog spool={spool} emptyWeightG={180} open onOpenChange={noop} onArchived={noop} />)
    const again = await screen.findByRole('dialog', { name: 'Archive spool #10' })
    expect(within(again).getByRole('radio', { name: 'Damaged' })).not.toBeChecked()
    expect(within(again).getByLabelText('Scale reading, with the spool (g)')).toHaveValue('')
    expect(within(again).getByRole('button', { name: 'Archive' })).toBeDisabled()
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

  it('says the grams the archive books as non-print waste (#7167)', async () => {
    const { dialog } = await show(10, 180)
    await userEvent.click(within(dialog).getByRole('radio', { name: 'Damaged' }))
    expect(await within(dialog).findByText('250 g will be booked as non-print waste.')).toBeInTheDocument()
    await userEvent.type(within(dialog).getByLabelText('Scale reading, with the spool (g)'), '200')
    expect(await within(dialog).findByText('20 g will be booked as non-print waste.')).toBeInTheDocument()
    // Respooled: the filament moved to another reel, so nothing is wasted.
    await userEvent.click(within(dialog).getByRole('radio', { name: 'Respooled' }))
    await waitFor(() => expect(within(dialog).queryByText(/booked as non-print waste/)).toBeNull())
    await within(dialog).findByText(/Filament at the end: 20 g\./)
    // 0 g left books none.
    await userEvent.click(within(dialog).getByRole('radio', { name: 'Finished' }))
    const field = within(dialog).getByLabelText('Scale reading, with the spool (g)')
    await userEvent.clear(field)
    await userEvent.type(field, '180')
    await within(dialog).findByText(/Filament at the end: 0 g\./)
    expect(within(dialog).queryByText(/booked as non-print waste/)).toBeNull()
  })
})
