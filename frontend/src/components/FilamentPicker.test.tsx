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
  offeredTrays,
}: {
  options: FilamentOptions
  copies?: number
  onChange?: (plan: SlotChoice[]) => void
  offeredTrays?: ReadonlySet<number>
}) {
  const [plan, setPlan] = useState<SlotChoice[]>(options.suggested ?? [])
  return (
    <FilamentPicker
      options={options}
      plan={plan}
      copies={copies}
      offeredTrays={offeredTrays}
      onChange={(next) => {
        setPlan(next)
        onChange?.(next)
      }}
    />
  )
}

function open(options: FilamentOptions = fixtures.filamentOptions, copies = 1, offeredTrays?: ReadonlySet<number>) {
  const onChange = vi.fn()
  return {
    onChange,
    ...renderPage(<Harness options={options} copies={copies} onChange={onChange} offeredTrays={offeredTrays} />),
  }
}

const slot = (id: number) => screen.getByTestId(`filament-slot-${id}`)

/** Opens a slot's list: a slot with a spool chosen shows only that spool until "Change". */
async function expand(user: { click: (element: Element) => Promise<void> }, id: number) {
  await user.click(screen.getByTestId(`change-slot-${id}`))
}

/** The fixture is printer 1, which has the Filament Track Switch fitted. */
const switched: FilamentOptions = fixtures.filamentOptions
/** The same printer as if each AMS were wired to one side. */
const wired: FilamentOptions = { ...fixtures.filamentOptions, track_switch: false }

describe('FilamentPicker', () => {
  // #469 — the fixture printer has the 0.2 on the right (extruder 0) and the 0.4 on the
  // left (1); spools 9 and 21 are on the right, the HT's spool 22 on the left.
  it('badges each loaded spool with the side it feeds, and the chosen one in the heading', async () => {
    const { user } = open(wired)
    await expand(user, 1)

    expect(within(slot(1)).getByTestId('side-9')).toHaveTextContent('R')
    expect(within(slot(1)).getByTestId('side-22')).toHaveTextContent('L')
    expect(within(slot(1)).queryByTestId('side-27')).toBeNull()
    expect(within(slot(1)).getByTestId('slot-side-1')).toHaveTextContent('R')
  })

  it('rules out no spool for the nozzle mounted on its side (#768)', async () => {
    const { user } = open(wired)
    await expand(user, 2)

    for (const id of [9, 22, 27]) expect(within(slot(2)).getByTestId(`spool-${id}`)).toBeEnabled()
    expect(screen.queryByText(/nozzle is fitted/)).toBeNull()
    expect(screen.queryByText(/Neither nozzle/)).toBeNull()
    expect(screen.queryByText(/spreads a multi-color print/)).toBeNull()
    // Nor does it list the mounted nozzles: nothing is judged by them.
    expect(screen.queryByText(/mounted\./)).toBeNull()
  })

  // #2166 — with the switch any spool reaches either nozzle: the plan says where each
  // prints, so no side is shown at all.
  it('with the track switch, shows no side', async () => {
    const { user } = open(switched)
    await expand(user, 2)

    expect(within(slot(2)).queryByTestId('side-9')).toBeNull()
    expect(screen.queryByTestId('slot-side-1')).toBeNull()
    expect(within(slot(2)).getByTestId('spool-9')).toBeEnabled()
  })

  // #2164 — the tray itself is offered only once the question about it was answered no.
  it('holds a tray-only row back until it is offered', async () => {
    const tray = {
      spool_id: -52,
      material: 'PLA',
      color_name: "what's in AMS-D slot 4",
      colour: '#27272C',
      tray_only: true,
    }
    const options = { ...fixtures.filamentOptions, spools: [...(fixtures.filamentOptions.spools ?? []), tray] }
    const first = open(options)
    await expand(first.user, 2)
    expect(screen.queryByTestId('spool--52')).toBeNull()
    first.unmount()

    const { user } = open(options, 1, new Set([-52]))
    await expand(user, 2)
    expect(within(slot(2)).getByTestId('spool--52')).toBeInTheDocument()
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

  it('shows every spool with its label, remaining grams and where it is loaded', async () => {
    const { user } = open()
    await expand(user, 1)
    const rows = slot(1)

    const misty = within(rows).getByTestId('spool-9').closest('label') as HTMLElement
    expect(misty).toHaveTextContent('Bambu Lab PETG Basic — Misty Blue')
    expect(misty).toHaveTextContent('1000 g')
    // Where it is, as a label. Which tray the print actually draws from is Bambuddy's
    // to decide at dispatch, so no tray number is computed here.
    expect(misty).toHaveTextContent('AMS-A · slot 2')

    // Loaded, but in the other machine — the difference between "ready" and "fetch it".
    const away = within(rows).getByTestId('spool-30').closest('label') as HTMLElement
    expect(away).toHaveTextContent('on 3DP-77A-114')
  })

  it('shows an em dash, never 0 g, for a spool Bambuddy cannot weigh', async () => {
    const { user } = open()
    await expand(user, 1)
    // An untagged spool reports `remain: -1`, which is unknown, not empty.
    const untagged = within(slot(1)).getByTestId('spool-27').closest('label') as HTMLElement
    expect(untagged).toHaveTextContent('\u2014')
    expect(untagged).not.toHaveTextContent('0 g')
  })

  it('starts on the server’s suggestion and moves when a spool is clicked', async () => {
    const { user, onChange } = open()

    expect(within(slot(1)).getByTestId('spool-21')).toBeChecked()
    expect(within(slot(2)).getByTestId('spool-27')).toBeChecked()

    await expand(user, 2)
    await user.click(within(slot(2)).getByTestId('spool-22'))

    expect(onChange).toHaveBeenCalledWith([
      { slot_id: 1, spool_id: 21 },
      { slot_id: 2, spool_id: 22 },
    ])
    expect(within(slot(2)).getByTestId('spool-22')).toBeChecked()
    // Picking collapses the list to the spool picked.
    expect(within(slot(2)).queryByTestId('spool-27')).not.toBeInTheDocument()
  })

  it('shows only the chosen spool until Change opens the list, and closes it on a pick', async () => {
    const { user } = open()

    expect(within(slot(1)).getAllByRole('radio')).toHaveLength(1)
    expect(within(slot(1)).getByTestId('spool-21')).toBeChecked()
    // Nothing is open, so there is nothing for the filters to narrow.
    expect(screen.queryByTestId('filter-material')).not.toBeInTheDocument()

    await expand(user, 1)
    expect(within(slot(1)).getAllByRole('radio').length).toBeGreaterThan(1)
    expect(screen.getByTestId('change-slot-1')).toHaveAttribute('aria-expanded', 'true')
    expect(screen.getByTestId('filter-material')).toBeInTheDocument()

    // "Done" keeps the spool and closes the list.
    await user.click(screen.getByTestId('change-slot-1'))
    expect(within(slot(1)).getAllByRole('radio')).toHaveLength(1)

    await expand(user, 1)
    await user.click(within(slot(1)).getByTestId('spool-9'))
    expect(within(slot(1)).getAllByRole('radio')).toHaveLength(1)
    expect(within(slot(1)).getByTestId('spool-9')).toBeChecked()
  })

  it('keeps a slot with nothing chosen open', () => {
    open({ ...fixtures.filamentOptions, suggested: [{ slot_id: 1, spool_id: 21 }] })

    expect(within(slot(2)).getAllByRole('radio').length).toBeGreaterThan(1)
    expect(screen.queryByTestId('change-slot-2')).not.toBeInTheDocument()
    expect(screen.getByTestId('filter-material')).toBeInTheDocument()
  })

  it("labels the file's own colour as the original, beside what it prints in", () => {
    open()

    expect(screen.getByTestId('slot-original-1')).toHaveTextContent('original')
    expect(screen.getByTestId('slot-prints-in-1')).toHaveTextContent('prints in')
  })

  it('says which colour each slot will print in, following the spool picked', async () => {
    const { user } = open()
    const spools = fixtures.filamentOptions.spools ?? []
    const named = (id: number) => spools.find((spool) => spool.spool_id === id)?.color_name

    expect(screen.getByTestId('slot-prints-in-2')).toHaveTextContent(`prints in ${named(27)}`)

    await expand(user, 2)
    await user.click(within(slot(2)).getByTestId('spool-22'))

    expect(screen.getByTestId('slot-prints-in-2')).toHaveTextContent(`prints in ${named(22)}`)
  })

  it('marks a spool already used by another slot without hiding it', async () => {
    const { user } = open()
    await expand(user, 2)
    await user.click(within(slot(2)).getByTestId('spool-21'))

    // Printing two slots from one spool is legitimate; it just should not be a surprise.
    const row = within(slot(1)).getByTestId('spool-21').closest('label') as HTMLElement
    expect(row).toHaveTextContent('used by slot 2')
    expect(within(slot(1)).getByTestId('spool-21')).toBeChecked()
  })

  it('restores the suggested filaments', async () => {
    const { user } = open()
    await expand(user, 2)
    await user.click(within(slot(2)).getByTestId('spool-22'))
    expect(within(slot(2)).getByTestId('spool-22')).toBeChecked()

    await user.click(screen.getByTestId('reset-filaments'))

    expect(within(slot(2)).getByTestId('spool-27')).toBeChecked()
  })

  it('filters the list every slot offers, not just one', async () => {
    const { user } = open()
    await expand(user, 1)
    await expand(user, 2)
    expect(within(slot(1)).queryByTestId('spool-9')).toBeInTheDocument()

    await user.selectOptions(screen.getByTestId('filter-material'), 'PLA')

    // The PETG spools go from BOTH slots: one inventory, one filter row.
    expect(within(slot(1)).queryByTestId('spool-9')).not.toBeInTheDocument()
    expect(within(slot(2)).queryByTestId('spool-9')).not.toBeInTheDocument()
    expect(within(slot(1)).queryByTestId('spool-21')).toBeInTheDocument()
  })

  it('searches the colour name and the brand', async () => {
    const { user } = open()
    await expand(user, 1)
    await user.type(screen.getByTestId('filter-search'), 'cookiecad')

    expect(within(slot(1)).queryByTestId('spool-24')).toBeInTheDocument()
    expect(within(slot(1)).queryByTestId('spool-9')).not.toBeInTheDocument()
  })

  it('hides the shelf when only what is loaded will do', async () => {
    const { user } = open()
    await expand(user, 1)
    await user.click(screen.getByTestId('filter-loaded-only'))

    expect(within(slot(1)).queryByTestId('spool-9')).toBeInTheDocument()
    expect(within(slot(1)).queryByTestId('spool-24')).not.toBeInTheDocument()
  })

  it('hides a spool with less than this print needs', async () => {
    const { user } = open()
    await expand(user, 1)
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
    await expand(user, 1)
    await user.click(screen.getByTestId('filter-enough'))

    // There is no figure to compare against, so silently emptying the list would look
    // like a broken inventory rather than an unsliced plate.
    expect(within(slot(1)).queryByTestId('spool-26')).toBeInTheDocument()
    expect(within(slot(1)).queryByTestId('spool-9')).toBeInTheDocument()
  })

  it('keeps the chosen spool on the list even when the filters exclude it', async () => {
    const { user } = open()
    await expand(user, 2)
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

    // "Load this spool into AMS-A slot 2" is an instruction, not a fault; colouring it
    // like one trains the user to ignore the colour.
    expect(screen.getByTestId('filament-warnings-1').firstChild).toHaveClass('text-muted')
    expect(screen.getByTestId('filament-warnings-2').firstChild).toHaveClass('text-warn')
  })

  it('recomputes a slot’s warnings for the spool the user actually picked', async () => {
    const { user } = open()
    expect(screen.getByTestId('filament-warnings-2')).toHaveTextContent('Load Elegoo')

    // Spool 22 is loaded in this printer, so the "load it in" instruction must go —
    // and it must go because it was recomputed, not because warnings were dropped.
    await expand(user, 2)
    await user.click(within(slot(2)).getByTestId('spool-22'))
    expect(screen.queryByTestId('filament-warnings-2')).not.toBeInTheDocument()

    // Back to the shelf spool and it comes back, which a dropped list could not do.
    await expand(user, 2)
    await user.click(within(slot(2)).getByTestId('spool-27'))
    expect(screen.getByTestId('filament-warnings-2')).toHaveTextContent('Load Elegoo')
  })

  it('gives every slot a labelled radio group and every filter a label', async () => {
    const { user } = open()
    await expand(user, 1)

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
