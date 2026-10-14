import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import type { NozzlePlan, SlicePreview, SlotNeed } from '../../api/types'
import { PreviewPane } from './PreviewPane'

const plan: NozzlePlan = {
  slots: [
    { slot_id: 1, side: 'L', name: 'Indigo Purple', colour: '#4B0082', material: 'PLA', by_hand: false },
    { slot_id: 2, side: 'R', name: 'Magenta', colour: '#FF00FF', material: 'PLA', by_hand: false },
    { slot_id: 3, side: 'R', name: 'Inland Black', colour: '#1A1A1A', material: 'PLA', by_hand: false },
  ],
  size: '0.4',
  left_flow: 'high_flow',
  right_flow: 'high_flow',
  track_switch: true,
  summary: '',
}

const slots: SlotNeed[] = [1, 2, 3].map((slot_id) => ({ slot_id, colour_matches: [] }))

function slice(entries: SlicePreview['slots']): SlicePreview {
  return { job_id: 9, status: 'completed', print_time_seconds: 2666, filament_used_g: 19, slots: entries }
}

function pane(
  result: SlicePreview | null,
  nozzlePlan: NozzlePlan | null = plan,
  plateSlots: readonly SlotNeed[] | null = slots,
) {
  render(
    <PreviewPane
      url={null}
      thumbnailUrl={null}
      slots={slots}
      spools={[]}
      plan={[]}
      colors={new Map()}
      slice={result}
      nozzlePlan={nozzlePlan}
      plateSlots={plateSlots}
      stale={false}
      slicing={false}
      sliceError={null}
    />,
  )
}

describe('PreviewPane: where the slice departs from the plan (#2449)', () => {
  it('names a planned colour the sliced plate dropped, and a filament moved to the other nozzle', () => {
    // Preview-slice 32: slot 2 too fine to slice, and the other two regrouped.
    pane(
      slice([
        { slot_id: 1, grams: 14.5, side: 'R' },
        { slot_id: 3, grams: 4.5, side: 'L' },
      ]),
    )
    const notes = screen.getByTestId('slice-departures')
    expect(notes).toHaveTextContent(
      "Magenta (slot 2) isn't used by the sliced plate, so it won't print: its parts may be too small to print.",
    )
    expect(notes).toHaveTextContent(
      'The slicer put Indigo Purple (slot 1) on the right nozzle, not the left as planned: it groups filaments itself to cut purging.',
    )
    expect(notes).toHaveTextContent('The slicer put Inland Black (slot 3) on the left nozzle, not the right as planned: it groups')
  })

  it('says a side chosen by hand was not kept (#2703)', () => {
    const byHand = { ...plan, slots: plan.slots!.map((slot) => (slot.slot_id === 1 ? { ...slot, by_hand: true } : slot)) }
    pane(
      slice([
        { slot_id: 1, grams: 14.5, side: 'R' },
        { slot_id: 2, grams: 0.5, side: 'R' },
        { slot_id: 3, grams: 4.8, side: 'R' },
      ]),
      byHand,
    )
    expect(screen.getByTestId('slice-departures')).toHaveTextContent(
      'The slicer put Indigo Purple (slot 1) on the right nozzle, not the left you chose: it groups filaments itself to cut purging, so a side set by hand is not kept.',
    )
  })

  it("warns that a spool moved to the other side may not reach it without the Track Switch (#2737 review)", () => {
    pane(
      slice([
        { slot_id: 1, grams: 14.5, side: 'R' },
        { slot_id: 2, grams: 0.5, side: 'R' },
        { slot_id: 3, grams: 4.8, side: 'R' },
      ]),
      { ...plan, track_switch: false },
    )
    const notes = screen.getByTestId('slice-departures')
    expect(notes).toHaveTextContent(
      'The slicer put Indigo Purple (slot 1) on the right nozzle, not the left as planned: it groups filaments itself to cut purging. Without the Filament Track Switch its AMS may not feed the right nozzle: check where the spool is loaded before printing.',
    )
    // A slot left on its side says nothing.
    expect(notes).not.toHaveTextContent('Magenta')
  })

  it('says nothing about feeding with the Track Switch, which reaches either nozzle', () => {
    pane(slice([{ slot_id: 1, grams: 14.5, side: 'R' }]))
    expect(screen.getByTestId('slice-departures')).not.toHaveTextContent('Track Switch')
  })

  it('does not warn about feeding for an older verdict without the Track Switch field (#2737 review)', () => {
    const { track_switch: _omitted, ...older } = plan
    pane(slice([{ slot_id: 1, grams: 14.5, side: 'R' }]), older as typeof plan)
    expect(screen.getByTestId('slice-departures')).toHaveTextContent('not the left as planned')
    expect(screen.getByTestId('slice-departures')).not.toHaveTextContent('Track Switch')
  })

  it('says nothing when the slice follows the plan', () => {
    pane(
      slice([
        { slot_id: 1, grams: 14.5, side: 'L' },
        { slot_id: 2, grams: 0.5, side: 'R' },
        { slot_id: 3, grams: 4.8, side: 'R' },
      ]),
    )
    expect(screen.queryByTestId('slice-departures')).toBeNull()
  })

  it('says nothing without a plan', () => {
    pane(slice([{ slot_id: 1, grams: 14.5, side: 'R' }]), null)
    expect(screen.queryByTestId('slice-departures')).toBeNull()
  })

  it('says nothing when the slice could not say which slots it used', () => {
    pane(slice([]))
    expect(screen.queryByTestId('slice-departures')).toBeNull()
  })

  it("never calls another plate's colour dropped when the sliced plate's own slots are unknown (#2702 review)", () => {
    // A two-plate file under "All plates": slot 2 (Magenta) is only on plate 2, so the
    // plan and the dialog's slots hold it, but the background slice is of plate 1.
    pane(
      slice([
        { slot_id: 1, grams: 14.5, side: 'L' },
        { slot_id: 3, grams: 4.8, side: 'L' },
      ]),
      plan,
      null,
    )
    const notes = screen.getByTestId('slice-departures')
    expect(notes).not.toHaveTextContent('Magenta')
    // A slot the slice used is on the sliced plate, so its side is still checked.
    expect(notes).toHaveTextContent('The slicer put Inland Black (slot 3) on the left nozzle, not the right as planned')
  })
})

