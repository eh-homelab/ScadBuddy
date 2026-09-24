import { screen, within } from '@testing-library/react'
import { useState } from 'react'
import { describe, expect, it, vi } from 'vitest'
import type { FilamentOptions, SlotChoice } from '../api/types'
import * as fixtures from '../mocks/fixtures'
import { renderPage } from '../test/utils'
import { FilamentPicker } from './FilamentPicker'

/** Controlled the way `PrintPicker` holds it, so a click really moves the selection. */
function Harness({
  options,
  copies = 1,
  onChange,
}: {
  options: FilamentOptions
  copies?: number
  onChange?: (plan: SlotChoice[]) => void
}) {
  const [plan, setPlan] = useState<SlotChoice[]>(options.suggested ?? [])
  return (
    <FilamentPicker
      options={options}
      plan={plan}
      copies={copies}
      onChange={(next) => {
        setPlan(next)
        onChange?.(next)
      }}
    />
  )
}

function open(options: FilamentOptions = fixtures.filamentOptions, copies = 1) {
  const onChange = vi.fn()
  return { onChange, ...renderPage(<Harness options={options} copies={copies} onChange={onChange} />) }
}

const slot = (id: number) => screen.getByTestId(`filament-slot-${id}`)

describe('FilamentPicker', () => {
  it('heads each slot with its colour, number, material and the grams it needs', () => {
    open(fixtures.filamentOptions, 3)

    expect(slot(1)).toHaveTextContent('Slot 1')
    expect(slot(1)).toHaveTextContent('PLA')
    // 4.8 g a copy, three copies.
    expect(slot(1)).toHaveTextContent('14 g for 3 copies')
    expect(slot(2)).toHaveTextContent('5.7 g for 3 copies')
  })

  it('says the grams are unknown rather than printing 0 g for an unsliced plate', () => {
    // `filament-requirements` answers `used_grams: 0` for every slot of a plate Bambuddy
    // has not sliced; the backend passes that through as null, and 0 g would read as
    // "this print needs no filament".
    const unsliced: FilamentOptions = {
      ...fixtures.filamentOptions,
      slots: (fixtures.filamentOptions.slots ?? []).map((need) => ({
        ...need,
        used_grams: null,
      })),
    }
    open(unsliced)

    // Scoped to the heading: a spool row may legitimately read "1000 g", and it is the
    // slot's own requirement that must never be spelled as a weight it does not have.
    const heading = (id: number) => slot(id).querySelector('legend') as HTMLElement
    expect(heading(1)).toHaveTextContent('grams unknown until this is sliced')
    expect(heading(1)).not.toHaveTextContent('0 g')
    expect(heading(2)).not.toHaveTextContent('0 g')
  })

  it('shows every spool with its label, remaining grams and where it is loaded', () => {
    open()
    const rows = slot(1)

    const misty = within(rows).getByTestId('spool-9').closest('label') as HTMLElement
    expect(misty).toHaveTextContent('Bambu Lab PETG Basic — Misty Blue')
    expect(misty).toHaveTextContent('1000 g')
    expect(misty).toHaveTextContent('AMS 0 · slot 2 · inlet B')

    // The AMS-HT holds one spool, so it has no slot number to show.
    const pink = within(rows).getByTestId('spool-22').closest('label') as HTMLElement
    expect(pink).toHaveTextContent('AMS-HT · inlet A')

    // Loaded, but in the other machine — the difference between "ready" and "fetch it".
    const away = within(rows).getByTestId('spool-30').closest('label') as HTMLElement
    expect(away).toHaveTextContent('on 3DP-77A-114')
  })

  it('shows an em dash, never 0 g, for a spool Bambuddy cannot weigh', () => {
    open()
    // An untagged spool reports `remain: -1`, which is unknown, not empty.
    const untagged = within(slot(1)).getByTestId('spool-27').closest('label') as HTMLElement
    expect(untagged).toHaveTextContent('\u2014')
    expect(untagged).not.toHaveTextContent('0 g')
  })

  it('starts on the server’s suggestion and moves when a spool is clicked', async () => {
    const { user, onChange } = open()

    expect(within(slot(1)).getByTestId('spool-21')).toBeChecked()
    expect(within(slot(2)).getByTestId('spool-27')).toBeChecked()

    await user.click(within(slot(2)).getByTestId('spool-22'))

    expect(onChange).toHaveBeenCalledWith([
      { slot_id: 1, spool_id: 21 },
      { slot_id: 2, spool_id: 22 },
    ])
    expect(within(slot(2)).getByTestId('spool-22')).toBeChecked()
    expect(within(slot(2)).getByTestId('spool-27')).not.toBeChecked()
  })

  it('marks a spool already used by another slot without hiding it', async () => {
    const { user } = open()
    await user.click(within(slot(2)).getByTestId('spool-21'))

    // Printing two slots from one spool is legitimate; it just should not be a surprise.
    const row = within(slot(1)).getByTestId('spool-21').closest('label') as HTMLElement
    expect(row).toHaveTextContent('used by slot 2')
    expect(within(slot(1)).getByTestId('spool-21')).toBeChecked()
  })

  it('restores the suggested filaments', async () => {
    const { user } = open()
    await user.click(within(slot(2)).getByTestId('spool-22'))
    expect(within(slot(2)).getByTestId('spool-22')).toBeChecked()

    await user.click(screen.getByTestId('reset-filaments'))

    expect(within(slot(2)).getByTestId('spool-27')).toBeChecked()
  })

  it('filters the list every slot offers, not just one', async () => {
    const { user } = open()
    expect(within(slot(1)).queryByTestId('spool-9')).toBeInTheDocument()

    await user.selectOptions(screen.getByTestId('filter-material'), 'PLA')

    // The PETG spools go from BOTH slots: one inventory, one filter row.
    expect(within(slot(1)).queryByTestId('spool-9')).not.toBeInTheDocument()
    expect(within(slot(2)).queryByTestId('spool-9')).not.toBeInTheDocument()
    expect(within(slot(1)).queryByTestId('spool-21')).toBeInTheDocument()
  })

  it('searches the colour name and the brand', async () => {
    const { user } = open()
    await user.type(screen.getByTestId('filter-search'), 'cookiecad')

    expect(within(slot(1)).queryByTestId('spool-24')).toBeInTheDocument()
    expect(within(slot(1)).queryByTestId('spool-9')).not.toBeInTheDocument()
  })

  it('hides the shelf when only what is loaded will do', async () => {
    const { user } = open()
    await user.click(screen.getByTestId('filter-loaded-only'))

    expect(within(slot(1)).queryByTestId('spool-9')).toBeInTheDocument()
    expect(within(slot(1)).queryByTestId('spool-24')).not.toBeInTheDocument()
  })

  it('hides a spool with less than this print needs', async () => {
    const { user } = open()
    // Spool 26 has 2 g against slot 1's 4.8 g.
    expect(within(slot(1)).queryByTestId('spool-26')).toBeInTheDocument()

    await user.click(screen.getByTestId('filter-enough'))

    expect(within(slot(1)).queryByTestId('spool-26')).not.toBeInTheDocument()
    // A spool Bambuddy cannot weigh survives: unknown is not empty.
    expect(within(slot(1)).queryByTestId('spool-27')).toBeInTheDocument()
  })

  it('hides nothing behind “enough” when the plate has not been sliced', async () => {
    const unsliced: FilamentOptions = {
      ...fixtures.filamentOptions,
      slots: (fixtures.filamentOptions.slots ?? []).map((need) => ({ ...need, used_grams: null })),
    }
    const { user } = open(unsliced)
    await user.click(screen.getByTestId('filter-enough'))

    // There is no figure to compare against, so silently emptying the list would look
    // like a broken inventory rather than an unsliced plate.
    expect(within(slot(1)).queryByTestId('spool-26')).toBeInTheDocument()
    expect(within(slot(1)).queryByTestId('spool-9')).toBeInTheDocument()
  })

  it('keeps the chosen spool on the list even when the filters exclude it', async () => {
    const { user } = open()
    // Slot 2 starts on the Elegoo pink, which is PLA; filter to PETG and it must stay,
    // or the radio group would show nothing selected and read as an empty slot.
    await user.selectOptions(screen.getByTestId('filter-material'), 'PETG')

    expect(within(slot(2)).getByTestId('spool-27')).toBeChecked()
  })

  it('shows the per-slot warnings under that slot and the plate-wide ones once', () => {
    open()

    expect(screen.getByTestId('filament-warnings-2')).toHaveTextContent(
      'Load Elegoo PLA Basic Deep Pink into the printer',
    )
    expect(screen.queryByTestId('filament-warnings-1')).not.toBeInTheDocument()
    expect(screen.getByTestId('filament-warnings')).toHaveTextContent(
      'no nozzle temperature for Elegoo PLA Basic Deep Pink',
    )
  })

  it('reads an advisory as muted and a real mismatch as a warning', () => {
    const options: FilamentOptions = {
      ...fixtures.filamentOptions,
      warnings: [
        { kind: 'not-loaded', slot_id: 1, message: 'Load it in.' },
        { kind: 'unreachable', slot_id: 1, message: 'The other inlet feeds that extruder.' },
      ],
      suggested: [
        { slot_id: 1, spool_id: 21 },
        { slot_id: 2, spool_id: 27 },
      ],
    }
    open(options)

    // "Load this spool into AMS 0 slot 2" is an instruction, not a fault; colouring it
    // like one trains the user to ignore the colour.
    expect(screen.getByText('Load it in.')).toHaveClass('text-muted')
    expect(screen.getByText('The other inlet feeds that extruder.')).toHaveClass('text-warn')
  })

  it('drops a slot’s warnings once that slot points somewhere else', async () => {
    const { user } = open()
    expect(screen.getByTestId('filament-warnings-2')).toBeInTheDocument()

    await user.click(within(slot(2)).getByTestId('spool-22'))

    // The server computed those against `suggested`; the spool they describe is no
    // longer chosen, so leaving them would assert something untrue.
    expect(screen.queryByTestId('filament-warnings-2')).not.toBeInTheDocument()
  })

  it('gives every slot a labelled radio group and every filter a label', () => {
    open()

    expect(screen.getByRole('group', { name: /Slot 1/ })).toBeInTheDocument()
    expect(screen.getByRole('group', { name: /Slot 2/ })).toBeInTheDocument()
    expect(screen.getByLabelText('Material')).toBe(screen.getByTestId('filter-material'))
    expect(screen.getByLabelText('Subtype')).toBe(screen.getByTestId('filter-subtype'))
    expect(screen.getByLabelText('Brand')).toBe(screen.getByTestId('filter-brand'))
    expect(screen.getByLabelText('Search')).toBe(screen.getByTestId('filter-search'))
    expect(screen.getByLabelText('Loaded in a printer')).toBe(
      screen.getByTestId('filter-loaded-only'),
    )
    expect(screen.getByLabelText('Enough for this print')).toBe(screen.getByTestId('filter-enough'))
  })
})
