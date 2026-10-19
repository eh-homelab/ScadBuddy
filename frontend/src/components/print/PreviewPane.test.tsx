import { fireEvent, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
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
  return { job_id: 9, status: 'completed', slicer: 'bambuddy', print_time_seconds: 2666, filament_used_g: 19, slots: entries }
}

function pane(
  result: SlicePreview | null,
  nozzlePlan: NozzlePlan | null = plan,
  plateSlots: readonly SlotNeed[] | null = slots,
  stale = false,
  keepsSides = true,
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
      stale={stale}
      keepsSides={keepsSides}
      slicing={false}
      sliceError={null}
    />,
  )
}

describe('PreviewPane: where the slice departs from the plan (#2449)', () => {
  it('says nothing of a stale slice, which the next slice may contradict (#3208)', () => {
    pane(
      slice([
        { slot_id: 1, grams: 14.5, side: 'R' },
        { slot_id: 3, grams: 4.5, side: 'L' },
      ]),
      plan,
      slots,
      true,
    )
    expect(screen.queryByTestId('slice-departures')).toBeNull()
    // Nor the per-slot sides, which say the same as the moved-nozzle note (#3500 review).
    expect(screen.queryByTestId('preview-side-1')).toBeNull()
    expect(screen.queryByTestId('preview-side-3')).toBeNull()
  })

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
    expect(screen.getByTestId('preview-side-1')).toHaveTextContent('right nozzle')
  })

  it('says plainly that a side chosen by hand was not kept, with no reason given (#2703, #3955)', () => {
    const byHand = { ...plan, slots: plan.slots!.map((slot) => (slot.slot_id === 1 ? { ...slot, by_hand: true } : slot)) }
    pane(
      slice([
        { slot_id: 1, grams: 14.5, side: 'R' },
        { slot_id: 2, grams: 0.5, side: 'R' },
        { slot_id: 3, grams: 4.8, side: 'R' },
      ]),
      byHand,
    )
    const notes = screen.getByTestId('slice-departures')
    expect(notes).toHaveTextContent(
      'The slicer put Indigo Purple (slot 1) on the right nozzle, not the left you chose. Check the sides before printing.',
    )
    expect(notes).not.toHaveTextContent('a side set by hand is not kept')
  })

  it('gives the regrouping as the reason in a file of more plates, where a side is not pinned (#3955 review)', () => {
    const byHand = { ...plan, slots: plan.slots!.map((slot) => (slot.slot_id === 1 ? { ...slot, by_hand: true } : slot)) }
    pane(
      slice([
        { slot_id: 1, grams: 14.5, side: 'R' },
        { slot_id: 2, grams: 0.5, side: 'R' },
        { slot_id: 3, grams: 4.8, side: 'R' },
      ]),
      byHand,
      slots,
      false,
      false,
    )
    expect(screen.getByTestId('slice-departures')).toHaveTextContent(
      'The slicer put Indigo Purple (slot 1) on the right nozzle, not the left you chose: it groups filaments itself to cut purging.',
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


describe('PreviewPane: a colour tried on the preview (#3669)', () => {
  it('opens from the slot chip, tries the colour, and Undo puts the spool back', async () => {
    const user = userEvent.setup()
    pane(null)
    await user.click(screen.getByRole('button', { name: 'Try a colour for slot 2' }))
    const field = await screen.findByLabelText('Colour for slot 2')
    fireEvent.input(field, { target: { value: '#00ff00' } })
    expect(screen.getByText('colour tried here, not a spool')).toBeInTheDocument()
    await user.keyboard('{Escape}')
    expect(screen.queryByLabelText('Colour for slot 2')).toBeNull()
    await user.click(screen.getByRole('button', { name: 'Undo' }))
    expect(screen.queryByText('colour tried here, not a spool')).toBeNull()
  })
})

describe('PreviewPane: the native slicer (plan 2026-10-16 Task 14)', () => {
  function slicing(progress: number | null, sliceError: string | null = null, running = true) {
    render(
      <PreviewPane
        url={null}
        thumbnailUrl={null}
        slots={slots}
        spools={[]}
        plan={[]}
        colors={new Map()}
        slice={null}
        nozzlePlan={plan}
        plateSlots={slots}
        stale={false}
        keepsSides
        slicing={running}
        progress={progress}
        sliceError={sliceError}
      />,
    )
  }

  it('shows how far a native slice has got, as text and a bar', () => {
    slicing(40)
    expect(screen.getByTestId('slice-status')).toHaveTextContent('Slicing… 40%')
    const bar = screen.getByTestId('slice-progress')
    expect(bar).toHaveAttribute('max', '100')
    expect(bar).toHaveAttribute('value', '40')
  })

  it('shows the text alone while there is no progress (Bambuddy, or before the first beat)', () => {
    slicing(null)
    expect(screen.getByTestId('slice-status')).toHaveTextContent(/^Slicing…$/)
    expect(screen.queryByTestId('slice-progress')).toBeNull()
  })

  it('shows a refused plate the native slicer failed as it said it (spec §5.5)', () => {
    slicing(null, 'The slicer refused the plate: plate 99 is empty', false)
    expect(screen.getByRole('alert')).toHaveTextContent(/^The slicer refused the plate: plate 99 is empty$/)
  })
})
