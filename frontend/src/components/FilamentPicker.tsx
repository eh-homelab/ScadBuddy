import { useState } from 'react'
import type { FilamentOptions, FilamentWarning, SlotChoice, SlotNeed } from '../api/types'
import { normalizeHex } from '../lib/format'
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
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import { Checkbox } from '@/components/ui/checkbox'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { RadioGroup, RadioGroupItem } from '@/components/ui/radio-group'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { cn } from '@/lib/utils'
import { Swatch } from './print/Swatch'

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
 * Only a slot with nothing chosen reads as a fault. "Load this spool into AMS-A slot 2"
 * is an instruction, and colouring it like a fault trains the user to ignore the colour.
 */
const WARNING_TONE: Record<FilamentWarning['kind'], string> = {
  'no-choice': 'text-warn',
  'not-loaded': 'text-muted',
  'low-filament': 'text-muted',
  'no-preset': 'text-muted',
  'no-fan-out': 'text-muted',
  // spec 2026-09-27 — the spool-first resolver's own kinds (task 5-7). None of these
  // are ever produced by this picker's own `warningsFor`, but the type is shared with
  // `PrintRunResult.warnings`, so the map still has to be exhaustive over it.
  'mixed-sizes': 'text-warn',
  'no-process': 'text-warn',
  'not-installed': 'text-warn',
  'plate-differs': 'text-muted',
  // #2366 — a plate the printer's model does not take: it sets the bed temperature.
  'plate-not-listed': 'text-warn',
  'hf-unsupported': 'text-muted',
  'hf-mounted': 'text-warn',
  // spec 2026-10-01 (#836) — the rack pick's own kinds; plate-wide, from PrintRunResult/check.
  'rack-unsafe-material': 'text-warn',
  'rack-left-to-bambuddy': 'text-muted',
  'rack-manual-partial': 'text-muted',
  // #956 — a by-object plate the toolhead or the gantry would hit; never a refusal.
  'sequence-clearance': 'text-warn',
  'profile-not-applied': 'text-warn',
  // #1899 — supports asked for and left out: the print differs from what was chosen.
  'supports-not-applied': 'text-warn',
  // #3628 — a prime tower setting the process preset has no value for, sent anyway.
  'prime-tower-unconfirmed': 'text-warn',
  // #3628 — a prime tower chosen for an already-sliced file, which queues as it is.
  'prime-tower-not-applied': 'text-warn',
  // #2671 — a sliced file queued as it is: what to set right before starting it.
  'sliced-other-model': 'text-warn',
  'sliced-nozzle-missing': 'text-warn',
  'sliced-plate-missing': 'text-warn',
  // #2955 — ScadBuddy's own earlier slice queued: said, so a missing slice job reads as intended.
  'slice-reused': 'text-muted',
}

/**
 * The side a spool feeds, lettered as the printer and Bambuddy letter it (#469): a label
 * only, since nothing is checked against the nozzle mounted there (#768). Shown only
 * without the Filament Track Switch, where the AMS is wired to that side: a square green
 * badge, like Bambuddy's nozzle-side badge.
 */
function SideBadge({ side, testId }: { side: 'L' | 'R'; testId: string }) {
  const word = side === 'L' ? 'left' : 'right'
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Badge
          className="size-4 rounded px-0 text-[10px] font-bold"
          style={{ background: 'var(--sb-side-bg)', color: 'var(--sb-side-ink)' }}
          aria-label={`Feeds the ${word} extruder`}
          data-testid={testId}
        >
          {side}
        </Badge>
      </TooltipTrigger>
      <TooltipContent>Feeds the {word} extruder</TooltipContent>
    </Tooltip>
  )
}

/** A filter select's value for no filter: Radix's select takes no empty value. */
const ANY = 'any'

function FilterSelect({
  label,
  value,
  options,
  onChange,
  testId,
}: {
  label: string
  value: string
  options: string[]
  onChange: (next: string) => void
  testId: string
}) {
  const id = `${testId}-select`
  return (
    <div className="grid gap-1">
      <Label htmlFor={id} className="text-xs font-normal text-muted-foreground">
        {label}
      </Label>
      <Select value={value || ANY} onValueChange={(next) => onChange(next === ANY ? '' : next)}>
        <SelectTrigger id={id} size="sm" className="min-w-24" data-testid={testId}>
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value={ANY}>Any</SelectItem>
          {options.map((option) => (
            <SelectItem key={option} value={option}>
              {option}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    </div>
  )
}

function grams(value: number): string {
  return `${value < 10 ? value.toFixed(1) : value.toFixed(0)} g`
}

/** What one slot needs, in words. Never "0 g" — see the module comment. */
function needLabel(slot: SlotNeed, copies: number, stale: boolean): string {
  const need = slotNeed(slot, copies)
  if (need === null) return 'grams unknown until this is sliced'
  const label = `${grams(need)} for ${copies} ${copies === 1 ? 'copy' : 'copies'}`
  return stale ? `${label}, from the last slice` : label
}

export function WarningList({ warnings, testId }: { warnings: FilamentWarning[]; testId: string }) {
  if (warnings.length === 0) return null
  return (
    <ul className="mt-1.5 space-y-0.5 text-xs" data-testid={testId}>
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
  /**
   * #2164 — the trays offered as themselves (by their negative spool id), after a "no"
   * to which spool is in them. Any other tray-only row is held back until then.
   */
  offeredTrays?: ReadonlySet<number>
  /** #2365 — the grams are the last slice's, and a slice for these choices is on its way. */
  gramsStale?: boolean
}

export function FilamentPicker({
  options,
  plan,
  onChange,
  copies,
  offeredTrays,
  gramsStale = false,
}: Props) {
  const [filters, setFilters] = useState<SpoolFilters>(NO_FILTERS)
  // Slots whose list is open although a spool is chosen. A slot with a spool shows only
  // that spool until "Change" opens it; a slot with nothing chosen is always open.
  const [opened, setOpened] = useState<ReadonlySet<number>>(() => new Set())
  const setOpen = (slotId: number, open: boolean) =>
    setOpened((current) => {
      const next = new Set(current)
      if (open) next.add(slotId)
      else next.delete(slotId)
      return next
    })

  const spools = (options.spools ?? []).filter(
    (spool) =>
      !spool.tray_only ||
      offeredTrays?.has(spool.spool_id) ||
      plan.some((choice) => choice.spool_id === spool.spool_id),
  )
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
    setOpen(slotId, false)
  }

  const set = <K extends keyof SpoolFilters>(key: K, value: SpoolFilters[K]) =>
    setFilters((current) => ({ ...current, [key]: value }))

  // Recomputed from the plan on screen, not read off the server's answer for its own
  // opening selection — that one stops being true the moment a slot is changed.
  const warnings = checkPlan(options, plan, copies)
  // #2166 — with the Filament Track Switch any spool reaches either nozzle, so which
  // inlet it rests on is not shown: ScadBuddy's nozzle plan says where each prints.
  const showSides = !(options.track_switch ?? false)
  const isOpen = (slotId: number) => chosenFor(slotId) === null || opened.has(slotId)
  // The filters narrow the open lists; with every slot showing only its spool they
  // would filter nothing.
  const anyOpen = slots.some((slot) => isOpen(slot.slot_id))

  return (
    <section className="space-y-3">
      <div className="flex items-baseline justify-between gap-3">
        <h3 className="text-sm font-medium">Filaments</h3>
        <Button
          type="button"
          variant="link"
          size="sm"
          className="h-auto px-0 text-xs"
          onClick={() => {
            onChange(suggested.map((choice) => ({ ...choice })))
            setOpened(new Set())
          }}
          data-testid="reset-filaments"
        >
          Reset to the suggested filaments
        </Button>
      </div>

      {anyOpen && (
        <div className="space-y-2">
          {/* One filter row for every slot: the inventory is the same list each time, and a
              per-slot copy would mean setting "PLA only" twice for a two-colour plate. */}
          <div className="flex flex-wrap items-end gap-2">
            <FilterSelect
              label="Material"
              value={filters.material}
              options={choices.materials}
              onChange={(next) => set('material', next)}
              testId="filter-material"
            />
            <FilterSelect
              label="Subtype"
              value={filters.subtype}
              options={choices.subtypes}
              onChange={(next) => set('subtype', next)}
              testId="filter-subtype"
            />
            <FilterSelect
              label="Brand"
              value={filters.brand}
              options={choices.brands}
              onChange={(next) => set('brand', next)}
              testId="filter-brand"
            />
            <div className="grid min-w-40 flex-1 gap-1">
              <Label htmlFor="filter-search" className="text-xs font-normal text-muted-foreground">
                Search
              </Label>
              <Input
                id="filter-search"
                type="search"
                value={filters.search}
                placeholder="Colour or brand"
                onChange={(event) => set('search', event.target.value)}
                className="h-7"
                data-testid="filter-search"
              />
            </div>
          </div>
          <div className="flex flex-wrap gap-4">
            <Label className="cursor-pointer text-xs font-normal text-muted-foreground">
              <Checkbox
                checked={filters.loadedOnly}
                onCheckedChange={(checked) => set('loadedOnly', checked === true)}
                data-testid="filter-loaded-only"
              />
              Loaded in a printer
            </Label>
            <Label className="cursor-pointer text-xs font-normal text-muted-foreground">
              <Checkbox
                checked={filters.enoughOnly}
                onCheckedChange={(checked) => set('enoughOnly', checked === true)}
                data-testid="filter-enough"
              />
              Enough for this print
            </Label>
          </div>
        </div>
      )}

      <div className="space-y-3">
        {slots.map((slot) => {
          const need = slotNeed(slot, copies)
          const chosen = chosenFor(slot.slot_id)
          const matched = filterSpools(spools, filters, need)
          /**
           * The chosen spool is always on the list, even when the filters exclude it.
           * A radio group whose checked member is not rendered shows nothing selected,
           * which reads as "this slot is empty" rather than "the filters hide it".
           */
          const chosenSpool = spools.find((spool) => spool.spool_id === chosen)
          const listOpen = isOpen(slot.slot_id)
          const rows = !listOpen
            ? [chosenSpool].filter((spool) => spool !== undefined)
            : matched.some((spool) => spool.spool_id === chosen)
              ? matched
              : [...spools.filter((spool) => spool.spool_id === chosen), ...matched]
          const heading = `filament-slot-${slot.slot_id}-heading`
          return (
            // min-w-0: without it the slot grows to its widest row and runs off a phone's
            // screen (#944).
            <Card
              key={slot.slot_id}
              size="sm"
              role="group"
              aria-labelledby={heading}
              className="min-w-0 gap-2 px-3"
              data-testid={`filament-slot-${slot.slot_id}`}
              onKeyDown={(event) => {
                // #3636 — Escape in an opened list closes that list, not the print
                // dialog around it: the dialog closes only on an Escape nothing took.
                if (event.key !== 'Escape' || !opened.has(slot.slot_id) || chosen === null) return
                event.stopPropagation()
                event.preventDefault()
                setOpen(slot.slot_id, false)
                event.currentTarget.querySelector<HTMLElement>(`[data-testid="change-slot-${slot.slot_id}"]`)?.focus()
              }}
            >
              <div id={heading} className="flex flex-wrap items-center gap-x-2 gap-y-1 text-sm">
                <span className="font-medium">Slot {slot.slot_id}</span>
                {/* The file's own colour for this slot, labelled so it is not read as a
                    second choice beside the "prints in" swatch. */}
                <Tooltip>
                  <TooltipTrigger asChild>
                    <span
                      className="flex items-center gap-1 text-xs text-muted-foreground"
                      data-testid={`slot-original-${slot.slot_id}`}
                    >
                      <Swatch colour={slot.colour} size="sm" />
                      original
                    </span>
                  </TooltipTrigger>
                  <TooltipContent>The colour this slot has in the file</TooltipContent>
                </Tooltip>
                {slot.material ? <Badge variant="outline">{slot.material}</Badge> : null}
                <span className="text-xs text-muted-foreground">{needLabel(slot, copies, gramsStale)}</span>
                {/* The colour this part will actually come out in: the file is recoloured
                    to it before slicing, so the plate thumbnail shows it too (#476). */}
                {chosenSpool && (
                  <span
                    className="flex min-w-0 flex-wrap items-center gap-1.5 text-xs text-muted-foreground"
                    data-testid={`slot-prints-in-${slot.slot_id}`}
                  >
                    <span aria-hidden="true">→</span>
                    <Swatch colour={chosenSpool.colour} size="sm" />
                    prints in {chosenSpool.color_name ?? normalizeHex(chosenSpool.colour ?? '#000000')}
                    {showSides && chosenSpool.side && (
                      <SideBadge side={chosenSpool.side} testId={`slot-side-${slot.slot_id}`} />
                    )}
                  </span>
                )}
              </div>
              {listOpen && rows.length === 0 ? (
                <p className="rounded-lg border px-2.5 py-3 text-xs text-muted-foreground">
                  No spool matches those filters.
                </p>
              ) : (
                <RadioGroup
                  aria-labelledby={heading}
                  value={chosen === null ? '' : String(chosen)}
                  onValueChange={(next) => choose(slot.slot_id, Number(next))}
                  className="max-h-56 gap-0 overflow-y-auto rounded-lg border"
                >
                  {rows.map((spool) => {
                    const where = loadedLabel(spool, options.printer_id)
                    // Shown, not hidden: printing two slots from one spool is a real thing
                    // to want, and Bambuddy accepts it. It just should not be a surprise.
                    const elsewhere = plan.find(
                      (choice) => choice.spool_id === spool.spool_id && choice.slot_id !== slot.slot_id,
                    )
                    return (
                      <label
                        key={spool.spool_id}
                        className={cn(
                          'flex cursor-pointer flex-wrap items-center gap-x-2.5 gap-y-1 border-b px-2.5 py-2 transition-colors last:border-b-0',
                          chosen === spool.spool_id ? 'bg-ui-accent' : 'hover:bg-ui-muted',
                        )}
                      >
                        <RadioGroupItem value={String(spool.spool_id)} data-testid={`spool-${spool.spool_id}`} />
                        <Swatch colour={spool.colour} size="sm" />
                        {/* The name takes at least 12rem; when the row is narrower than that
                            plus the details, the details wrap onto a second line (#944). */}
                        <span className="min-w-0 flex-[1_1_12rem] truncate text-xs">{spoolLabel(spool)}</span>
                        <span className="ml-auto flex shrink-0 flex-wrap items-center justify-end gap-2.5">
                          {elsewhere && (
                            <span className="shrink-0 text-[11px] text-muted-foreground">
                              used by slot {elsewhere.slot_id}
                            </span>
                          )}
                          {where && <Badge variant="secondary">{where}</Badge>}
                          {showSides && spool.side && <SideBadge side={spool.side} testId={`side-${spool.spool_id}`} />}
                          <span className="sb-num shrink-0 text-[11px] text-muted-foreground">
                            {spool.remaining_g === null || spool.remaining_g === undefined
                              ? '—'
                              : grams(spool.remaining_g)}
                          </span>
                        </span>
                      </label>
                    )
                  })}
                </RadioGroup>
              )}
              {chosenSpool && (
                <Button
                  type="button"
                  variant="link"
                  size="sm"
                  className="h-auto w-fit px-0 text-xs"
                  onClick={() => setOpen(slot.slot_id, !listOpen)}
                  aria-expanded={listOpen}
                  data-testid={`change-slot-${slot.slot_id}`}
                >
                  {listOpen ? 'Done' : 'Change'}
                </Button>
              )}

              <WarningList warnings={warningsFor(warnings, slot.slot_id)} testId={`filament-warnings-${slot.slot_id}`} />
            </Card>
          )
        })}
      </div>
    </section>
  )
}
