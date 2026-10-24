import { act, fireEvent, render, renderHook, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '../../api/client'
import type { SpoolOption, UnknownTray } from '../../api/types'
import { howFull, linkKey, linkOf, useDeclinedTrays } from '../../lib/trays'
import { TrayQuestion } from './TrayQuestion'

const tray: UnknownTray = {
  ams_id: 3,
  tray_id: 3,
  label: 'AMS-D slot 4',
  material: 'PLA',
  colour: '#27272C',
  colour_word: 'black',
  fingerprint: 'PLA|#27272C|11',
  spool_id: -52,
  candidates: [16, 18],
}
const black: SpoolOption = {
  spool_id: 16,
  material: 'PLA',
  brand: 'Inland',
  color_name: 'Black',
  colour: '#1A1A1A',
  remaining_g: 515,
  label_weight_g: 1000,
  storage_location: 'Shelf B',
}
const empty: SpoolOption = { ...black, spool_id: 18, remaining_g: 0, storage_location: null }

describe('TrayQuestion (#2164)', () => {
  beforeEach(() => localStorage.clear())

  it('asks plainly about the one likely spool, with what a person can check by hand', () => {
    render(<TrayQuestion printerId={1} tray={{ ...tray, candidates: [16] }} spools={[black]} onAssigned={vi.fn()} onDecline={vi.fn()} />)
    expect(screen.getByText(/AMS-D slot 4 has black PLA, but Bambuddy doesn't know which spool it is/)).toBeInTheDocument()
    expect(screen.getByText('Is the Inland Black spool (about half full, 515 g) in AMS-D slot 4?')).toBeInTheDocument()
    expect(screen.getByText(/kept in Shelf B/)).toBeInTheDocument()
    expect(screen.getByText(/spool #16/)).toBeInTheDocument()
  })

  it('records the spool on yes, and re-reads', async () => {
    const assign = vi.spyOn(api, 'assignTraySpool').mockResolvedValue({ spool_id: 16, printer_id: 1, ams_id: 3, tray_id: 3 })
    const onAssigned = vi.fn()
    render(<TrayQuestion printerId={1} tray={tray} spools={[black, empty]} onAssigned={onAssigned} onDecline={vi.fn()} />)
    fireEvent.click(screen.getByRole('button', { name: 'Yes, this one' }))
    await waitFor(() => expect(onAssigned).toHaveBeenCalled())
    expect(assign).toHaveBeenCalledWith(1, 3, 3, 16)
  })

  it('remembers a no until the tray changes', () => {
    const { result, rerender } = renderHook(({ trays }) => useDeclinedTrays(1, trays), { initialProps: { trays: [tray] } })
    expect(result.current.open).toEqual([tray])
    act(() => result.current.decline(tray))
    expect(result.current.open).toEqual([])
    expect(result.current.offered.has(-52)).toBe(true)

    const reloaded = { ...tray, fingerprint: 'PLA|#FFFFFF|11' }
    rerender({ trays: [reloaded] })
    expect(result.current.open).toEqual([reloaded])
  })

  it('says how full a spool is in words', () => {
    expect(howFull(black)).toBe('about half full, 515 g')
    expect(howFull(empty)).toBe('empty, 0 g')
    expect(howFull({ ...black, label_weight_g: null })).toBe('515 g left')
    expect(howFull({ ...black, remaining_g: null })).toBeNull()
  })
})

// #3818 (plan 2026-10-18 D1) — on a native run the question links a provider's record
// through ScadBuddy's own tray route, never Bambuddy's assignment.
describe('TrayQuestion on the native path', () => {
  const printer = '00000000-0000-4000-8000-0000000000c2'
  const record: SpoolOption = {
    ...black,
    storage_location: null,
    records: { bambuddy: { record_id: '27', brand: 'Inland', name: 'Black', remaining_g: 515 } },
  }

  it("names a candidate's records by their provider's own label (#4516)", () => {
    render(
      <TrayQuestion
        native={{ printer }}
        tray={{ ...tray, candidates: [16] }}
        spools={[record]}
        providerLabels={{ bambuddy: 'Bambuddy (garage)' }}
        onAssigned={vi.fn()}
        onDecline={vi.fn()}
      />,
    )
    expect(screen.getByText(/Bambuddy \(garage\)'s record/)).toBeInTheDocument()
  })

  it('links the record through PUT …/trays/{ams}/{tray}/spool and re-reads', async () => {
    const link = vi.spyOn(api, 'linkTraySpool').mockResolvedValue({ ams_id: 3, tray_id: 3, records: {} })
    const assign = vi.spyOn(api, 'assignTraySpool')
    const onAssigned = vi.fn()
    render(
      <TrayQuestion
        native={{ printer }}
        tray={{ ...tray, candidates: [16] }}
        spools={[record]}
        onAssigned={onAssigned}
        onDecline={vi.fn()}
      />,
    )
    expect(screen.getByText('AMS-D slot 4 has black PLA, but no spool record is linked to it.')).toBeInTheDocument()
    expect(screen.getByText(/Bambuddy's record/)).toBeInTheDocument()
    expect(screen.queryByText(/Bambuddy's inventory/)).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Yes, that one' }))
    await waitFor(() => expect(onAssigned).toHaveBeenCalled())
    expect(link).toHaveBeenCalledWith(printer, 3, 3, { provider: 'bambuddy', record_id: '27' })
    expect(assign).not.toHaveBeenCalled()
  })

  it('names the physical spool when the candidate has one', async () => {
    const link = vi.spyOn(api, 'linkTraySpool').mockResolvedValue({ ams_id: 3, tray_id: 3, records: {} })
    const spool = { ...record, physical_spool_id: '7a2c1f0e-0000-4000-8000-000000000009' }
    render(
      <TrayQuestion native={{ printer }} tray={{ ...tray, candidates: [16] }} spools={[spool]} onAssigned={vi.fn()} onDecline={vi.fn()} />,
    )
    fireEvent.click(screen.getByRole('button', { name: 'Yes, that one' }))
    await waitFor(() =>
      expect(link).toHaveBeenCalledWith(printer, 3, 3, { physical_spool_id: '7a2c1f0e-0000-4000-8000-000000000009' }),
    )
  })

  it('says so, and reports nothing linked, when the picked spool lost its record', async () => {
    const link = vi.spyOn(api, 'linkTraySpool')
    const onAssigned = vi.fn()
    const other = { ...record, spool_id: 18, records: { bambuddy: { record_id: '28' } } }
    const props = { native: { printer }, tray, onAssigned, onDecline: vi.fn() }
    const { rerender } = render(<TrayQuestion {...props} spools={[record, other]} />)
    // A re-read drops the picked spool's record; the pick stays on it.
    rerender(<TrayQuestion {...props} spools={[{ ...record, records: {} }, other]} />)
    fireEvent.click(screen.getByRole('button', { name: /^Yes/ }))
    expect(await screen.findByRole('alert')).toHaveTextContent('ScadBuddy has no record of that spool to link.')
    expect(link).not.toHaveBeenCalled()
    expect(onAssigned).not.toHaveBeenCalled()
  })

  // #4511 item 4 — a shelf row's `spool_id` is its place in one answer, not the spool.
  it('keys a link on the record, never the positional shelf id, across a re-read', async () => {
    const first = { ...record, spool_id: 16, records: { bambuddy: { record_id: '27' } } }
    const second = { ...record, spool_id: 18, color_name: 'Grey', records: { bambuddy: { record_id: '28' } } }
    expect(linkOf(first)).toEqual(linkOf({ ...first, spool_id: 41 }))
    expect(linkKey(linkOf({ ...first, spool_id: 41 }))).toBe('record:bambuddy:27')
    expect(linkKey(linkOf({ ...first, physical_spool_id: 'p-1' }))).toBe('spool:p-1')

    const link = vi.spyOn(api, 'linkTraySpool').mockResolvedValue({ ams_id: 3, tray_id: 3, records: {} })
    const onAssigned = vi.fn()
    const props = { native: { printer }, onAssigned, onDecline: vi.fn() }
    const { rerender } = render(<TrayQuestion {...props} tray={{ ...tray, candidates: [16, 18] }} spools={[first, second]} />)
    // The re-read numbers the same two records the other way round.
    rerender(
      <TrayQuestion
        {...props}
        tray={{ ...tray, candidates: [18, 16] }}
        spools={[{ ...second, spool_id: 16 }, { ...first, spool_id: 18 }]}
      />,
    )
    fireEvent.click(screen.getByRole('button', { name: 'Yes, this one' }))
    await waitFor(() => expect(onAssigned).toHaveBeenCalledWith({ provider: 'bambuddy', record_id: '27' }))
    expect(link).toHaveBeenCalledWith(printer, 3, 3, { provider: 'bambuddy', record_id: '27' })
  })

  it('offers no candidate it could not link', () => {
    render(
      <TrayQuestion native={{ printer }} tray={{ ...tray, candidates: [16] }} spools={[black]} onAssigned={vi.fn()} onDecline={vi.fn()} />,
    )
    expect(screen.queryByRole('button', { name: /^Yes/ })).not.toBeInTheDocument()
  })
})
