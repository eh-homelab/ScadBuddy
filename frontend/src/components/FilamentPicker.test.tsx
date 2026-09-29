import { cleanup, screen, within } from '@testing-library/react'
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
  nozzleSize,
  onChange,
}: {
  options: FilamentOptions
  copies?: number
  nozzleSize?: string
  onChange?: (plan: SlotChoice[]) => void
}) {
  const [plan, setPlan] = useState<SlotChoice[]>(options.suggested ?? [])
  return (
    <FilamentPicker
      options={options}
      plan={plan}
      copies={copies}
      nozzleSize={nozzleSize}
      onChange={(next) => {
        setPlan(next)
        onChange?.(next)
      }}
    />
  )
}

function open(options: FilamentOptions = fixtures.filamentOptions, copies = 1, nozzleSize?: string) {
  const onChange = vi.fn()
  return {
    onChange,
    ...renderPage(
      <Harness options={options} copies={copies} nozzleSize={nozzleSize} onChange={onChange} />,
    ),
  }
}

const slot = (id: number) => screen.getByTestId(`filament-slot-${id}`)

/** The fixture is printer 1, which has the Filament Track Switch fitted. */
const switched: FilamentOptions = fixtures.filamentOptions
/** The same printer as if each AMS were wired to one side. */
const wired: FilamentOptions = { ...fixtures.filamentOptions, track_switch: false }
const bothAt02: FilamentOptions = {
  ...switched,
  nozzles: [
    { nozzle_type: 'HS00', nozzle_diameter: '0.2' },
    { nozzle_type: 'HS00', nozzle_diameter: '0.2' },
  ],
}

describe('FilamentPicker', () => {
  // #469 — the fixture printer has the 0.2 on the right (extruder 0) and the 0.4 on the
  // left (1); spools 9 and 21 are on the right, the HT's spool 22 on the left.
  it('badges each loaded spool with the side it feeds, and the chosen one in the heading', () => {
    open(wired)

    expect(within(slot(1)).getByTestId('side-9')).toHaveTextContent('R')
    expect(within(slot(1)).getByTestId('side-22')).toHaveTextContent('L')
    expect(within(slot(1)).queryByTestId('side-27')).toBeNull()
    expect(within(slot(1)).getByTestId('slot-side-1')).toHaveTextContent('R')
  })

  it('disables a spool whose side has another nozzle fitted, and says which', () => {
    open(wired, 1, '0.4')

    expect(within(slot(2)).getByTestId('spool-9')).toBeDisabled()
    expect(within(slot(2)).getByTestId('mismatch-9')).toHaveTextContent('R · 0.2 fitted')
    expect(within(slot(2)).getByTestId('spool-22')).toBeEnabled()
    expect(within(slot(2)).queryByTestId('mismatch-22')).toBeNull()
    // A shelf spool has no side, so nothing rules it out.
    expect(within(slot(2)).getByTestId('spool-27')).toBeEnabled()
  })

  it('says why a chosen spool on the wrong side will not print', () => {
    // The suggestion puts spool 21, on the right's 0.2, in slot 1.
    open(wired, 1, '0.4')

    expect(screen.getByTestId('slot-mismatch-1')).toHaveTextContent(
      "This spool feeds the right extruder, where the 0.2 mm nozzle is fitted, so it can't print at 0.4 mm.",
    )
    expect(screen.queryByTestId('slot-mismatch-2')).toBeNull()
  })

  it('with the track switch, a side is only where the spool rests and rules nothing out', () => {
    open(switched, 1, '0.4')

    expect(within(slot(2)).getByTestId('side-9')).toHaveTextContent('rests on R')
    expect(within(slot(2)).getByTestId('side-9')).toHaveAttribute(
      'title',
      'Rests on the right inlet; the Filament Track Switch can feed it to either nozzle.',
    )
    expect(within(slot(2)).getByTestId('spool-9')).toBeEnabled()
    expect(screen.queryByTestId('slot-mismatch-1')).toBeNull()
  })

  it('says up front that a multi-color print can not run when only one side fits', () => {
    open(switched, 1, '0.4')
    expect(screen.getByTestId('one-fitting-nozzle')).toHaveTextContent(
      "Only the left nozzle is 0.4 mm, and the slicer spreads a multi-color print across both, so this can't print.",
    )
    expect(screen.queryByRole('combobox', { name: 'Slot 1 extruder' })).toBeNull()
  })

  it('lets a single-nozzle printer print in several colors', () => {
    open({ ...wired, nozzles: [{ nozzle_type: 'HS00', nozzle_diameter: '0.4' }] }, 1, '0.4')
    expect(screen.queryByTestId('one-fitting-nozzle')).toBeNull()
    expect(screen.queryByTestId('no-fitting-nozzle')).toBeNull()
  })

  it('does not refuse up front when one side of two is unreported', () => {
    open(
      {
        ...wired,
        nozzles: [
          { nozzle_type: 'HS00', nozzle_diameter: '0.4' },
          { nozzle_type: 'HH01', nozzle_diameter: '' },
        ],
      },
      1,
      '0.4',
    )
    expect(screen.queryByTestId('one-fitting-nozzle')).toBeNull()
  })

  it('counts a spare of the size in the rack, and names it when it serves one side', () => {
    // Both sides 0.4 and a 0.2 in the rack: one side can print 0.2, so two colors can't.
    const both04: FilamentOptions = {
      ...wired,
      nozzles: [
        { nozzle_type: 'HH01', nozzle_diameter: '0.4' },
        { nozzle_type: 'HH01', nozzle_diameter: '0.4' },
      ],
      rack: [{ nozzle_type: 'HS00', nozzle_diameter: '0.2' }],
    }
    open(both04, 1, '0.2')
    expect(screen.queryByTestId('no-fitting-nozzle')).toBeNull()
    expect(screen.getByTestId('one-fitting-nozzle')).toHaveTextContent(
      'Neither nozzle is 0.2 mm and the rack holds one spare, enough for one side',
    )
    cleanup()
    // A second 0.2 in the rack, beside the mounted one: both sides can print it.
    open({ ...wired, rack: [{ nozzle_type: 'HS00', nozzle_diameter: '0.2' }] }, 1, '0.2')
    expect(screen.queryByTestId('one-fitting-nozzle')).toBeNull()
    expect(within(slot(1)).getByTestId('spool-22')).toBeEnabled()
    expect(screen.queryByTestId('slot-mismatch-1')).toBeNull()
  })

  it('says nothing about sides when both nozzles fit', () => {
    open(bothAt02, 1, '0.2')
    expect(screen.queryByTestId('one-fitting-nozzle')).toBeNull()
    expect(screen.queryByTestId('no-fitting-nozzle')).toBeNull()
  })

  it('says so up front when neither nozzle is the chosen size', () => {
    open(switched, 1, '0.6')
    expect(screen.getByTestId('no-fitting-nozzle')).toHaveTextContent(
      'Neither nozzle is 0.6 mm: the right has 0.2 mm and the left 0.4 mm.',
    )
  })

  it('rules nothing out before a nozzle size is chosen', () => {
    open()
    expect(within(slot(2)).getByTestId('spool-9')).toBeEnabled()
    expect(screen.queryByTestId('slot-mismatch-1')).toBeNull()
  })

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
    // Where it is, as a label. Which tray the print actually draws from is Bambuddy's
    // to decide at dispatch, so no tray number is computed here.
    expect(misty).toHaveTextContent('AMS 0 · slot 2')

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

  it('says which colour each slot will print in, following the spool picked', async () => {
    const { user } = open()
    const spools = fixtures.filamentOptions.spools ?? []
    const named = (id: number) => spools.find((spool) => spool.spool_id === id)?.color_name

    expect(screen.getByTestId('slot-prints-in-2')).toHaveTextContent(`prints in ${named(27)}`)

    await user.click(within(slot(2)).getByTestId('spool-22'))

    expect(screen.getByTestId('slot-prints-in-2')).toHaveTextContent(`prints in ${named(22)}`)
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

  it('shows a slot’s warnings under that slot', () => {
    open()

    expect(screen.getByTestId('filament-warnings-2')).toHaveTextContent(
      'Load Elegoo PLA Basic — Deep Pink into the printer',
    )
    expect(screen.queryByTestId('filament-warnings-1')).not.toBeInTheDocument()
  })

  it('reads an instruction as muted and an unfilled slot as a fault', () => {
    open({ ...fixtures.filamentOptions, suggested: [{ slot_id: 1, spool_id: 27 }] })

    // "Load this spool into AMS 0 slot 2" is an instruction, not a fault; colouring it
    // like one trains the user to ignore the colour.
    expect(screen.getByTestId('filament-warnings-1').firstChild).toHaveClass('text-muted')
    expect(screen.getByTestId('filament-warnings-2').firstChild).toHaveClass('text-warn')
  })

  it('recomputes a slot’s warnings for the spool the user actually picked', async () => {
    const { user } = open()
    expect(screen.getByTestId('filament-warnings-2')).toHaveTextContent('Load Elegoo')

    // Spool 22 is loaded in this printer, so the "load it in" instruction must go —
    // and it must go because it was recomputed, not because warnings were dropped.
    await user.click(within(slot(2)).getByTestId('spool-22'))
    expect(screen.queryByTestId('filament-warnings-2')).not.toBeInTheDocument()

    // Back to the shelf spool and it comes back, which a dropped list could not do.
    await user.click(within(slot(2)).getByTestId('spool-27'))
    expect(screen.getByTestId('filament-warnings-2')).toHaveTextContent('Load Elegoo')
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
