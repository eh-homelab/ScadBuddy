import { useState, type ReactNode } from 'react'
import type { FilamentOptions, FilamentWarning, SlotChoice, SlotNeed, SpoolOption } from '../api/types'
import {
  NO_FILTERS,
  choiceReasons,
  closeMatch,
  colourName,
  facets,
  filterSpools,
  fromTrays,
  loadedLabel,
  slotNeed,
  checkPlan,
  spoolLabel,
  tileGroups,
  trayChoiceLabel,
  trayCode,
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
import { NeedsTypeBadge } from './filament/NeedsTypeBadge'
import { RgbaSwatch } from './filament/RgbaSwatch'
import { Swatch } from './print/Swatch'

/**
 * Which spool prints each plate slot (#87).
 *
 * The list is the **whole** inventory, not only what is in the AMS: a spool on the
 * shelf is a legitimate choice that produces a "load this into …" instruction, and
 * hiding it would turn a two-step print into a dead end. The server has already sorted
 * the rows — loaded in this printer, then loaded elsewhere, then the shelf, most
 * remaining first within each band. The grid (#6885, `tileGroups`) only groups them:
 * the close matches for the slot first, then each unit as Bambu Studio maps it, then
 * other printers and the shelf, each group in the server's order.
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
  'not-loaded': 'text-subtle',
  'low-filament': 'text-subtle',
  'no-preset': 'text-subtle',
  'no-fan-out': 'text-subtle',
  // spec 2026-09-27 — the spool-first resolver's own kinds (task 5-7). None of these
  // are ever produced by this picker's own `warningsFor`, but the type is shared with
  // `PrintRunResult.warnings`, so the map still has to be exhaustive over it.
  'mixed-sizes': 'text-warn',
  'no-process': 'text-warn',
  'not-installed': 'text-warn',
  'plate-differs': 'text-subtle',
  // #2366 — a plate the printer's model does not take: it sets the bed temperature.
  'plate-not-listed': 'text-warn',
  'hf-unsupported': 'text-subtle',
  'hf-mounted': 'text-warn',
  // spec 2026-10-01 (#836) — the rack pick's own kinds; plate-wide, from PrintRunResult/check.
  'rack-unsafe-material': 'text-warn',
  'rack-left-to-bambuddy': 'text-subtle',
  'rack-manual-partial': 'text-subtle',
  // #956 — a by-object plate the toolhead or the gantry would hit; never a refusal.
  'sequence-clearance': 'text-warn',
  'profile-not-applied': 'text-warn',
  // #1899 — supports asked for and left out: the print differs from what was chosen.
  'supports-not-applied': 'text-warn',
  // #3628 — a prime tower setting the process preset has no value for, sent anyway.
  'prime-tower-unconfirmed': 'text-warn',
  // #2998 — a slicer setting the process preset has no value for, or a list over one value:
  // sent as set, and the slicer judges it.
  'process-override-unchecked': 'text-warn',
  'host-setting-dropped': 'text-warn',
  // #3628 — a prime tower chosen for an already-sliced file, which queues as it is;
  // #3962 — a tower asked for that Bambuddy's slicer dropped from the slice.
  'prime-tower-not-applied': 'text-warn',
  // #2671 — a sliced file queued as it is: what to set right before starting it.
  'sliced-other-model': 'text-warn',
  'sliced-nozzle-missing': 'text-warn',
  'sliced-plate-missing': 'text-warn',
  // #3436 — a sliced file whose settings could not be read: nothing was checked, so it is held.
  'sliced-unchecked': 'text-warn',
  // #2956 — sliced for another model of the same G-code family, which prints it.
  'sliced-same-family': 'text-subtle',
  // #2955 — ScadBuddy's own earlier slice queued: said, so a missing slice job reads as intended.
  'slice-reused': 'text-subtle',
  // #2722 — the chosen spools mix material families: a warning, never a refusal.
  'mixed-materials': 'text-warn',
  // #4272 — High Flow on one side and Standard on the other: refused.
  'mixed-flows': 'text-warn',
  // #4272 — a Track Switch inlet feeding both extruders, by a side chosen by hand.
  'inlet-shared': 'text-warn',
  // #6732 — the slicer refused a plate with "Object conflicts were detected".
  'object-conflict': 'text-warn',
  // #6781 — by object on a plate that is one object of several colours: it has no effect.
  'by-object-one-object': 'text-warn',
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

/**
 * #6743 — a warning's tone. A kind this build does not know yet (a newer API) still
 * shows, in the quiet tone, rather than with no class.
 */
function warningTone(kind: string): string {
  return (WARNING_TONE as Partial<Record<string, string>>)[kind] ?? 'text-subtle'
}

export function WarningList({ warnings, testId }: { warnings: FilamentWarning[]; testId: string }) {
  if (warnings.length === 0) return null
  return (
    <ul className="mt-1.5 space-y-0.5 text-xs" data-testid={testId}>
      {warnings.map((warning, index) => (
        <li key={`${warning.kind}-${warning.slot_id ?? 'plate'}-${index}`} className={warningTone(warning.kind)}>
          {warning.message}
        </li>
      ))}
    </ul>
  )
}

/**
 * #6262 — a spool's chip: its `#RRGGBBAA` (the file's `colour` for a row with no `rgba`),
 * with the grams left as an arc around it where the row lists a spool, not a "prints in".
 */
function SpoolChip({
  spool,
  grams: arc = false,
  size = 'sm',
}: {
  spool: SpoolOption
  grams?: boolean
  size?: 'sm' | 'md'
}) {
  return (
    <RgbaSwatch
      rgba={spool.rgba ?? spool.colour ?? '#000000'}
      size={size}
      gramsLeft={arc ? spool.remaining_g : null}
      labelWeight={arc ? spool.label_weight_g : null}
    />
  )
}

/**
 * What is left on a spool. On a native run the SB spool's grams when the tray holds one
 * (the ledger), else the AMS's own percent; on the Bambuddy path the grams. An unknown
 * is a dash, never "0 g": an untagged spool reports `remain: -1`.
 */
function remaining(spool: SpoolOption, trays: boolean): string {
  if (spool.remaining_g !== null && spool.remaining_g !== undefined) return grams(spool.remaining_g)
  if (trays && spool.remain_percent !== null && spool.remain_percent !== undefined) return `${spool.remain_percent}%`
  return '—'
}

/**
 * #6885 — one tray or spool in the grid, as Bambu Studio's filament mapping draws a tray:
 * the colour as a swatch (with the grams arc), the one colour name, the material and what
 * is left, and the tray's code ("A3", "HT-A"). The whole tile is the radio: the button
 * covers it, invisible, so a click anywhere picks it, the arrow keys move between tiles
 * (Radix's radio group), and the tile draws the focus ring. Its name is the full sentence
 * a screen reader needs, "PLA Blue in AMS-A slot 1, 812 g left".
 */
function SpoolTile({
  spool,
  trays,
  checked,
  printerId,
  usedBy,
  showSides,
  matches,
}: {
  spool: SpoolOption
  trays: boolean
  checked: boolean
  printerId: number | null | undefined
  usedBy: number | null
  showSides: boolean
  matches: boolean
}) {
  const name = colourName(spool, trays)
  const code = trayCode(spool)
  const left = remaining(spool, trays)
  const where = trays ? null : loadedLabel(spool, printerId)
  const away = !trays && spool.loaded && printerId != null && spool.loaded.printer_id !== printerId
  const said = trays ? trayChoiceLabel(spool) : [spoolLabel(spool), where].filter(Boolean).join(', ')
  const label = [said, left === '—' ? 'amount left unknown' : `${left} left`, usedBy ? `used by slot ${usedBy}` : null]
    .filter(Boolean)
    .join(', ')
  return (
    <label
      className={cn(
        'relative flex w-42 max-w-full min-w-0 cursor-pointer flex-col gap-1 rounded-md border px-2 py-1.5 text-xs transition-colors',
        'has-[:focus-visible]:ring-3 has-[:focus-visible]:ring-ring/50',
        checked ? 'border-primary bg-accent' : 'hover:bg-muted',
        !matches && !checked && 'text-muted-foreground',
      )}
      data-matches={matches ? 'true' : undefined}
    >
      <RadioGroupItem
        value={String(spool.spool_id)}
        aria-label={label}
        data-testid={`spool-${spool.spool_id}`}
        className="absolute inset-0 aspect-auto size-full rounded-md border-0 opacity-0 after:hidden"
      />
      <span className="flex min-w-0 items-center gap-2">
        <SpoolChip spool={spool} grams size="md" />
        <span className="min-w-0 flex-1">
          <span className={cn('block truncate font-medium', matches || checked ? 'text-foreground' : '')}>{name}</span>
          <span className="block truncate text-[11px] text-muted-foreground">
            {[spool.material, trays ? null : spool.brand].filter(Boolean).join(' · ')}
          </span>
        </span>
        <span className="flex shrink-0 flex-col items-end gap-0.5 text-[11px] text-muted-foreground">
          {code && <span className="sb-num font-semibold">{code}</span>}
          <span className="sb-num">{left}</span>
        </span>
      </span>
      {(away || usedBy || spool.sb_number != null || spool.needs_type || (showSides && spool.side)) && (
        <span className="relative z-10 flex flex-wrap items-center gap-1.5 text-[11px] text-muted-foreground">
          {spool.sb_number != null && (
            <span className="sb-num" data-testid={`sb-spool-${spool.spool_id}`}>
              #{spool.sb_number}
            </span>
          )}
          <NeedsTypeBadge spool={spool} />
          {showSides && spool.side && <SideBadge side={spool.side} testId={`side-${spool.spool_id}`} />}
          {away && <span className="truncate">on {spool.loaded?.printer_name ?? 'another printer'}</span>}
          {usedBy && <span>used by slot {usedBy}</span>}
        </span>
      )}
    </label>
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
  /** #5627 — more of a slot's row, under its spool: its "Settings for this colour". */
  slotExtra?: (slotId: number) => ReactNode
  /**
   * #6885 — say "Slot N has no filament chosen." under an empty slot. Off by default:
   * before the user acts, an empty slot is a question (its grid is open), not an error.
   * The dialog turns it on once Print is pressed; "Reset to the suggested filaments" is an
   * act too, and shows it for a slot the reset leaves empty.
   */
  showMissing?: boolean
}

export function FilamentPicker({
  options,
  plan,
  onChange,
  copies,
  offeredTrays,
  gramsStale = false,
  slotExtra,
  showMissing = false,
}: Props) {
  const [filters, setFilters] = useState<SpoolFilters>(NO_FILTERS)
  const [wasReset, setWasReset] = useState(false)
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

  /**
   * #3818 — a native run's choices are the printer's loaded trays (plan 2026-10-18 D1):
   * every one a choice of its own, naming the SB spool it holds (#6262). A shelf spool
   * (loaded nowhere) is only for linking, in the tray question, never a print choice.
   */
  const trays = fromTrays(options)
  const spools = (options.spools ?? []).filter((spool) =>
    trays
      ? spool.loaded !== null && spool.loaded !== undefined
      : !spool.tray_only ||
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
  // #6885 — an empty slot is no fault until the user acts: see `showMissing`.
  const warnings = checkPlan(options, plan, copies).filter(
    (warning) => warning.kind !== 'no-choice' || showMissing || wasReset,
  )
  // #2166 — with the Filament Track Switch any spool reaches either nozzle, so which
  // inlet it rests on is not shown: ScadBuddy's nozzle plan says where each prints.
  const showSides = !(options.track_switch ?? false)
  const isOpen = (slotId: number) => chosenFor(slotId) === null || opened.has(slotId)
  // The filters narrow the open lists; with every slot showing only its spool they
  // would filter nothing.
  // A native run's trays are a few slots, not an inventory: nothing to filter (#3818).
  const anyOpen = !trays && slots.some((slot) => isOpen(slot.slot_id))

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
            setWasReset(true)
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
          const matched = trays ? spools : filterSpools(spools, filters, need)
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
          const reasons = chosenSpool ? choiceReasons(slot, chosenSpool, trays) : []
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
                    <SpoolChip spool={chosenSpool} />
                    prints in {colourName(chosenSpool, trays)}
                    {showSides && chosenSpool.side && (
                      <SideBadge side={chosenSpool.side} testId={`slot-side-${slot.slot_id}`} />
                    )}
                  </span>
                )}
              </div>
              {/* #6885 — why the choice is not what the file asks for: a choice, said plainly,
                  not a fault. */}
              {reasons.length > 0 && (
                <p className="text-xs text-muted-foreground" data-testid={`slot-reason-${slot.slot_id}`}>
                  {reasons.join('; ')}.
                </p>
              )}
              {listOpen && rows.length === 0 ? (
                <p className="rounded-lg border px-2.5 py-3 text-xs text-muted-foreground">
                  No spool matches those filters.
                </p>
              ) : (
                <RadioGroup
                  aria-labelledby={heading}
                  value={chosen === null ? '' : String(chosen)}
                  onValueChange={(next) => choose(slot.slot_id, Number(next))}
                  className={cn('flex flex-wrap items-start gap-x-4 gap-y-2.5', !trays && listOpen && 'max-h-96 overflow-y-auto pr-1')}
                >
                  {/* Closed, the chosen tile stays in the group it has when open, its heading
                      hidden: the same keys keep the same tile, so the focus and the radio
                      stay put when a pick closes the grid. */}
                  {tileGroups(rows, slot, options.printer_id).map(
                    (group) => (
                      <div key={group.key} className="flex max-w-full min-w-0 flex-col gap-1" data-testid={`slot-${slot.slot_id}-group-${group.key}`}>
                        {listOpen && (
                          <p className="text-[11px] font-medium text-muted-foreground">{group.label}</p>
                        )}
                        {/* Tiles of 10.5rem side by side, and the units side by side, as Bambu
                            Studio maps them; on a phone, one tile a row (#944). */}
                        <div className="flex flex-wrap gap-1.5">
                          {group.spools.map((spool) => {
                            // Shown, not hidden: printing two slots from one spool is a real
                            // thing to want, and Bambuddy accepts it. It just should not be a
                            // surprise.
                            const elsewhere = plan.find(
                              (choice) => choice.spool_id === spool.spool_id && choice.slot_id !== slot.slot_id,
                            )
                            return (
                              <SpoolTile
                                key={spool.spool_id}
                                spool={spool}
                                trays={trays}
                                checked={chosen === spool.spool_id}
                                printerId={options.printer_id}
                                usedBy={elsewhere?.slot_id ?? null}
                                showSides={showSides}
                                matches={group.key === 'best' || closeMatch(slot, spool)}
                              />
                            )
                          })}
                        </div>
                      </div>
                    ),
                  )}
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
              {slotExtra?.(slot.slot_id)}
            </Card>
          )
        })}
      </div>
    </section>
  )
}
