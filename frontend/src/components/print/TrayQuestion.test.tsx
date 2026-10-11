import { act, fireEvent, render, renderHook, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '../../api/client'
import type { SpoolOption, UnknownTray } from '../../api/types'
import { howFull, useDeclinedTrays } from '../../lib/trays'
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
