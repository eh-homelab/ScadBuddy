import { useState } from 'react'
import type { FilamentOptions, FilamentWarning, SlotChoice, SlotNeed } from '../api/types'
import { inkOn, normalizeHex } from '../lib/format'
import {
  NO_FILTERS,
  facets,
  filterSpools,
  loadedLabel,
  slotNeed,
  checkPlan,
  spoolLabel,
  warningsFor,
  type SpoolFilters,
} from '../lib/filaments'

/**
 * Which spool prints each plate slot (#87).
 *
 * The list is the **whole** inventory, not only what is in the AMS: a spool on the
 * shelf is a legitimate choice that produces a "load this into …" instruction, and
 * hiding it would turn a two-step print into a dead end. The server has already sorted
 * the rows — loaded in this printer, then loaded elsewhere, then the shelf, most
 * remaining first within each band — so the order is meaningful and is not re-sorted
 * here.
 *
 * Two of Bambuddy's answers are "unknown" spelled as a number, and both are handled
 * rather than rendered:
 *
 * - **A plate ScadBuddy has just uploaded has not been sliced**, so
 *   `filament-requirements` answers `used_grams: 0` for every slot. The backend passes
 *   that through as `null`, and this says so instead of printing "0 g" — which would
 *   read as "this print needs no filament".
 * - **An untagged spool has no remaining weight** (`remain: -1` on the tray). It shows
 *   an em dash, and no filter may hide it: "unknown" is not "empty".
 *
 * Every warning here is advisory and none of them disables Run. Whether the filaments
 * can share a plate, and which tray each one is drawn from, are Bambuddy's questions —
 * its eligibility report answers them beside these, and #86's `force` covers the things
 * it refuses outright.
 */

/**
 * Only a slot with nothing chosen reads as a fault. "Load this spool into AMS 0 slot 2"
 * is an instruction, and colouring it like a fault trains the user to ignore the colour.
 */
const WARNING_TONE: Record<FilamentWarning['kind'], string> = {
  'no-choice': 'text-warn',
  'not-loaded': 'text-muted',
  'low-filament': 'text-muted',
  'no-preset': 'text-muted',
}

function Swatch({ colour, size = 'md' }: { colour: string | null | undefined; size?: 'sm' | 'md' }) {
  const hex = normalizeHex(colour ?? '#000000')
  return (
    <span
      aria-hidden="true"
      title={hex}
      style={{ background: hex, color: inkOn(hex) }}
      className={`shrink-0 rounded-[3px] ring-1 ring-black/25 ring-inset ${
        size === 'sm' ? 'size-4' : 'size-5'
      }`}
    />
  )
}

function grams(value: number): string {
  return `${value < 10 ? value.toFixed(1) : value.toFixed(0)} g`
}

/** What one slot needs, in words. Never "0 g" — see the module comment. */
function needLabel(slot: SlotNeed, copies: number): string {
  const need = slotNeed(slot, copies)
  if (need === null) return 'grams unknown until this is sliced'
  return `${grams(need)} for ${copies} ${copies === 1 ? 'copy' : 'copies'}`
}

function WarningList({ warnings, testId }: { warnings: FilamentWarning[]; testId: string }) {
  if (warnings.length === 0) return null
  return (
    <ul className="mt-1.5 space-y-0.5 text-[12px]" data-testid={testId}>
      {warnings.map((warning, index) => (
        <li key={`${warning.kind}-${warning.slot_id ?? 'plate'}-${index}`} className={WARNING_TONE[warning.kind]}>
          {warning.message}
        </li>
      ))}
    </ul>
  )
}

interface Props {
  options: FilamentOptions
  plan: SlotChoice[]
  onChange: (plan: SlotChoice[]) => void
  copies: number
}

export function FilamentPicker({ options, plan, onChange, copies }: Props) {
  const [filters, setFilters] = useState<SpoolFilters>(NO_FILTERS)

  const spools = options.spools ?? []
  const slots = options.slots ?? []
  const suggested = options.suggested ?? []
  const choices = facets(spools)

  const chosenFor = (slotId: number) =>
    plan.find((choice) => choice.slot_id === slotId)?.spool_id ?? null

  function choose(slotId: number, spoolId: number) {
    onChange([
      ...plan.filter((choice) => choice.slot_id !== slotId),
      { slot_id: slotId, spool_id: spoolId },
    ])
  }

  const set = <K extends keyof SpoolFilters>(key: K, value: SpoolFilters[K]) =>
    setFilters((current) => ({ ...current, [key]: value }))

  // Recomputed from the plan on screen, not read off the server's answer for its own
  // opening selection — that one stops being true the moment a slot is changed.
  const warnings = checkPlan(options, plan, copies)

  return (
    <section className="mt-4">
      <div className="flex items-baseline justify-between gap-3">
        <h3 className="text-[13px] text-ink">Filaments</h3>
        <button
          type="button"
          onClick={() => onChange(suggested.map((choice) => ({ ...choice })))}
          className="text-[12px] text-muted underline decoration-dotted underline-offset-2 hover:text-ink"
          data-testid="reset-filaments"
        >
          Reset to the suggested filaments
        </button>
      </div>

      {/* One filter row for every slot: the inventory is the same list each time, and a
          per-slot copy would mean setting "PLA only" twice for a two-colour plate. */}
      <div className="mt-2 flex flex-wrap items-end gap-2">
        <label className="flex flex-col gap-1 text-[12px] text-muted">
          Material
          <select
            value={filters.material}
            onChange={(event) => set('material', event.target.value)}
            className="sb-field cursor-pointer py-1 text-[12px]"
            data-testid="filter-material"
          >
            <option value="">Any</option>
            {choices.materials.map((material) => (
              <option key={material} value={material}>
                {material}
              </option>
            ))}
          </select>
        </label>
        <label className="flex flex-col gap-1 text-[12px] text-muted">
          Subtype
          <select
            value={filters.subtype}
            onChange={(event) => set('subtype', event.target.value)}
            className="sb-field cursor-pointer py-1 text-[12px]"
            data-testid="filter-subtype"
          >
            <option value="">Any</option>
            {choices.subtypes.map((subtype) => (
              <option key={subtype} value={subtype}>
                {subtype}
              </option>
            ))}
          </select>
        </label>
        <label className="flex flex-col gap-1 text-[12px] text-muted">
          Brand
          <select
            value={filters.brand}
            onChange={(event) => set('brand', event.target.value)}
            className="sb-field cursor-pointer py-1 text-[12px]"
            data-testid="filter-brand"
          >
            <option value="">Any</option>
            {choices.brands.map((brand) => (
              <option key={brand} value={brand}>
                {brand}
              </option>
            ))}
          </select>
        </label>
        <label className="flex flex-1 flex-col gap-1 text-[12px] text-muted">
          Search
          <input
            type="search"
            value={filters.search}
            placeholder="Colour or brand"
            onChange={(event) => set('search', event.target.value)}
            className="sb-field py-1 text-[12px]"
            data-testid="filter-search"
          />
        </label>
      </div>
      <div className="mt-2 flex flex-wrap gap-4">
        <label className="flex cursor-pointer items-center gap-2 text-[12px] text-muted">
          <input
            type="checkbox"
            checked={filters.loadedOnly}
            onChange={(event) => set('loadedOnly', event.target.checked)}
            className="accent-[var(--sb-accent)]"
            data-testid="filter-loaded-only"
          />
          Loaded in a printer
        </label>
        <label className="flex cursor-pointer items-center gap-2 text-[12px] text-muted">
          <input
            type="checkbox"
            checked={filters.enoughOnly}
            onChange={(event) => set('enoughOnly', event.target.checked)}
            className="accent-[var(--sb-accent)]"
            data-testid="filter-enough"
          />
          Enough for this print
        </label>
      </div>

      <div className="mt-3 space-y-3">
        {slots.map((slot) => {
          const need = slotNeed(slot, copies)
          const chosen = chosenFor(slot.slot_id)
          const matched = filterSpools(spools, filters, need)
          /**
           * The chosen spool is always on the list, even when the filters exclude it.
           * A radio group whose checked member is not rendered shows nothing selected,
           * which reads as "this slot is empty" rather than "the filters hide it".
           */
          const rows = matched.some((spool) => spool.spool_id === chosen)
            ? matched
            : [...spools.filter((spool) => spool.spool_id === chosen), ...matched]
          return (
            <fieldset key={slot.slot_id} data-testid={`filament-slot-${slot.slot_id}`}>
              <legend className="flex items-center gap-2 text-[13px] text-ink">
                <Swatch colour={slot.colour} />
                Slot {slot.slot_id}
                {slot.material ? <span className="text-muted">{slot.material}</span> : null}
                <span className="text-[12px] text-faint">{needLabel(slot, copies)}</span>
              </legend>

              <ul className="mt-1.5 max-h-56 overflow-y-auto rounded-[6px] border border-line">
                {rows.length === 0 && (
                  <li className="px-2.5 py-3 text-[12px] text-muted">
                    No spool matches those filters.
                  </li>
                )}
                {rows.map((spool) => {
                  const where = loadedLabel(spool, options.printer_id)
                  // Shown, not hidden: printing two slots from one spool is a real thing
                  // to want, and Bambuddy accepts it. It just should not be a surprise.
                  const elsewhere = plan.find(
                    (choice) => choice.spool_id === spool.spool_id && choice.slot_id !== slot.slot_id,
                  )
                  return (
                    <li key={spool.spool_id} className="border-b border-line last:border-b-0">
                      <label
                        className={`flex cursor-pointer items-center gap-2.5 px-2.5 py-2 transition-colors ${
                          chosen === spool.spool_id ? 'bg-accent/8' : 'hover:bg-surface-2'
                        }`}
                      >
                        <input
                          type="radio"
                          name={`filament-slot-${slot.slot_id}`}
                          value={spool.spool_id}
                          checked={chosen === spool.spool_id}
                          onChange={() => choose(slot.slot_id, spool.spool_id)}
                          className="accent-[var(--sb-accent)]"
                          data-testid={`spool-${spool.spool_id}`}
                        />
                        <Swatch colour={spool.colour} size="sm" />
                        <span className="min-w-0 flex-1 truncate text-[12px] text-ink">
                          {spoolLabel(spool)}
                        </span>
                        {elsewhere && (
                          <span className="shrink-0 text-[11px] text-faint">
                            used by slot {elsewhere.slot_id}
                          </span>
                        )}
                        {where && (
                          <span className="shrink-0 rounded-full bg-surface-2 px-1.5 py-0.5 text-[11px] text-muted">
                            {where}
                          </span>
                        )}
                        <span className="sb-num shrink-0 text-[11px] text-faint">
                          {spool.remaining_g === null || spool.remaining_g === undefined
                            ? '—'
                            : grams(spool.remaining_g)}
                        </span>
                      </label>
                    </li>
                  )
                })}
              </ul>

              <WarningList
                warnings={warningsFor(warnings, slot.slot_id)}
                testId={`filament-warnings-${slot.slot_id}`}
              />
            </fieldset>
          )
        })}
      </div>

    </section>
  )
}
