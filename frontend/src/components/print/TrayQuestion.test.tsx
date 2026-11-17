import { act, fireEvent, render, renderHook, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '../../api/client'
import type { FilamentSpool, SpoolOption, UnknownTray } from '../../api/types'
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

  it("posts the SB spool's number on Bambuddy's path, and names ScadBuddy when it fails (#6262)", async () => {
    // On Bambuddy's path an option's spool_id is the SB spool's number (build_options).
    const sb: SpoolOption = { ...black, spool_id: 7, sb_number: 7, sb_spool_id: '8d6f3c1e-0000-4000-8000-000000000007' }
    const assign = vi.spyOn(api, 'assignTraySpool').mockRejectedValue(new Error('offline'))
    render(<TrayQuestion printerId={1} tray={{ ...tray, candidates: [7] }} spools={[sb]} onAssigned={vi.fn()} onDecline={vi.fn()} />)
    expect(screen.getByText(/spool #7/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Yes, that one' }))
    expect(await screen.findByText('ScadBuddy could not record it.')).toBeInTheDocument()
    expect(assign).toHaveBeenCalledWith(1, 3, 3, 7)
    assign.mockRestore()
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

// #3818 (plan 2026-10-18 D1), #6262 Task 4 — on a native run the question places an SB
// spool through ScadBuddy's own tray route, never Bambuddy's assignment.
describe('TrayQuestion on the native path', () => {
  const printer = '00000000-0000-4000-8000-0000000000c2'
  const SB = '7a2c1f0e-0000-4000-8000-000000000009'
  const placed = { id: SB, number: 9 } as FilamentSpool
  const record: SpoolOption = { ...black, storage_location: null, sb_spool_id: SB, sb_number: 16 }

  it('names a candidate by its SB spool number (#6262)', () => {
    render(
      <TrayQuestion
        native={{ printer }}
        tray={{ ...tray, candidates: [16] }}
        spools={[{ ...record, sb_number: 7 }]}
        onAssigned={vi.fn()}
        onDecline={vi.fn()}
      />,
    )
    expect(screen.getByText(/spool #7/)).toBeInTheDocument()
  })

  it('draws a candidate in RGBA and links a spool with no type to its page (#6262)', () => {
    render(
      <TrayQuestion
        native={{ printer }}
        tray={{ ...tray, candidates: [16] }}
        spools={[{ ...record, rgba: '#1A1A1A80', needs_type: true }]}
        onAssigned={vi.fn()}
        onDecline={vi.fn()}
      />,
    )
    const chips = screen.getAllByTestId('rgba-swatch')
    expect(chips.some((chip) => chip.getAttribute('data-colour') === '#1A1A1A80')).toBe(true)
    const badge = screen.getByRole('link', { name: 'Needs type' })
    expect(badge.getAttribute('href')).toMatch(new RegExp(`/filament/spools/${SB}$`))
  })

  it('posts {spool_id: <uuid>} to PUT …/trays/{ams}/{tray}/spool and re-reads', async () => {
    const link = vi.spyOn(api, 'linkTraySpool').mockResolvedValue(placed)
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
    expect(screen.getByText(/spool #16/)).toBeInTheDocument()
    expect(screen.queryByText(/Bambuddy's inventory/)).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Yes, that one' }))
    await waitFor(() => expect(onAssigned).toHaveBeenCalledWith({ spool_id: SB }))
    expect(link).toHaveBeenCalledWith(printer, 3, 3, { spool_id: SB })
    expect(assign).not.toHaveBeenCalled()
  })

  it('says so, and reports nothing linked, when the picked spool lost its SB spool', async () => {
    const link = vi.spyOn(api, 'linkTraySpool')
    const onAssigned = vi.fn()
    const other = { ...record, spool_id: 18, sb_spool_id: '7a2c1f0e-0000-4000-8000-000000000010' }
    const props = { native: { printer }, tray, onAssigned, onDecline: vi.fn() }
    const { rerender } = render(<TrayQuestion {...props} spools={[record, other]} />)
    // A re-read drops the picked candidate's spool; the pick stays on it.
    rerender(<TrayQuestion {...props} spools={[{ ...record, sb_spool_id: null }, other]} />)
    fireEvent.click(screen.getByRole('button', { name: /^Yes/ }))
    expect(await screen.findByRole('alert')).toHaveTextContent('ScadBuddy has no record of that spool to link.')
    expect(link).not.toHaveBeenCalled()
    expect(onAssigned).not.toHaveBeenCalled()
  })

  // #4511 item 4 — a link is keyed on the SB spool, never the row's `spool_id`.
  it('keys a link on the SB spool, never the positional shelf id, across a re-read', async () => {
    const grey = '7a2c1f0e-0000-4000-8000-000000000010'
    const first = { ...record, spool_id: 16 }
    const second = { ...record, spool_id: 18, color_name: 'Grey', sb_spool_id: grey }
    expect(linkOf(first)).toEqual(linkOf({ ...first, spool_id: 41 }))
    expect(linkKey(linkOf(first))).toBe(`spool:${SB}`)

    const link = vi.spyOn(api, 'linkTraySpool').mockResolvedValue(placed)
    const onAssigned = vi.fn()
    const props = { native: { printer }, onAssigned, onDecline: vi.fn() }
    const { rerender } = render(<TrayQuestion {...props} tray={{ ...tray, candidates: [16, 18] }} spools={[first, second]} />)
    // The re-read numbers the same two spools the other way round.
    rerender(
      <TrayQuestion
        {...props}
        tray={{ ...tray, candidates: [18, 16] }}
        spools={[{ ...second, spool_id: 16 }, { ...first, spool_id: 18 }]}
      />,
    )
    fireEvent.click(screen.getByRole('button', { name: 'Yes, this one' }))
    await waitFor(() => expect(onAssigned).toHaveBeenCalledWith({ spool_id: SB }))
    expect(link).toHaveBeenCalledWith(printer, 3, 3, { spool_id: SB })
  })

  it('offers no candidate with no SB spool', () => {
    render(
      <TrayQuestion
        native={{ printer }}
        tray={{ ...tray, candidates: [16] }}
        spools={[{ ...record, sb_spool_id: null }]}
        onAssigned={vi.fn()}
        onDecline={vi.fn()}
      />,
    )
    expect(screen.queryByRole('button', { name: /^Yes/ })).not.toBeInTheDocument()
  })
})
