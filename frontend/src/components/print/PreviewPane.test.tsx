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

function pane(result: SlicePreview | null, nozzlePlan: NozzlePlan | null = plan) {
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
      "Magenta (slot 2) isn't used by the sliced plate: its parts are too small to print, so it won't print.",
    )
    expect(notes).toHaveTextContent('The slicer put Indigo Purple (slot 1) on the right nozzle, not the left as planned.')
    expect(notes).toHaveTextContent('The slicer put Inland Black (slot 3) on the left nozzle, not the right as planned.')
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

  it('says nothing without a plan, or without slots read from the slice', () => {
    pane(slice([{ slot_id: 1, grams: 14.5, side: 'R' }]), null)
    expect(screen.queryByTestId('slice-departures')).toBeNull()
  })

  it('says nothing when the slice could not say which slots it used', () => {
    pane(slice([]))
    expect(screen.queryByTestId('slice-departures')).toBeNull()
  })
})
