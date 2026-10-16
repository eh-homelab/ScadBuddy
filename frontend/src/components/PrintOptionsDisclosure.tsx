import { useEffect, useMemo, useState } from 'react'
import { api, ApiError } from '../api/client'
import type { OptionScope, PrintOptions, PrintOptionsState, PrintOptionsView } from '../api/types'
import {
  CALIBRATION_CHOICES,
  clampOption,
  effectiveScope,
  formatOption,
  isNonDefault,
  isSet,
  optionValue,
  PREHEAT_CHOICES,
  PRINT_OPTIONS,
  resolveOptions,
  scopeLabel,
  type OptionLayer,
  type OptionName,
  type OptionSpec,
  type OptionValue,
} from '../lib/printOptions'
import type { OptionsSubject } from '../lib/printSource'
import { ChevronDownIcon } from 'lucide-react'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Skeleton } from '@/components/ui/skeleton'
import { Spinner } from './ui/Spinner'

interface Props {
  /**
   * What is being printed — the key its own scope is remembered under (#1754): the
   * model's id, or a library file's `library:<file id>`. Without one, that scope is not
   * offered.
   */
  subject?: OptionsSubject
  /**
   * The printer the print will reach, when the caller already knows it. Left undefined,
   * the server says which one the per-printer scope keys on: the printer set in Settings.
   */
  printerId?: number | null
  /** Per-send overrides. Owned by the parent, which is what puts them on the wire. */
  value: PrintOptions
  onChange: (next: PrintOptions) => void
  /**
   * The merged result, for a caller that has to show one of these values outside the
   * disclosure — the send bar's Copies box is the effective `quantity`.
   */
  onEffective?: (effective: PrintOptions) => void
  /**
   * #3635 — only the options that change what happens after Print, for Simple mode,
   * which shows no Options but still sends what was remembered.
   */
  heldOnly?: boolean
  /** With `heldOnly`: the remembered options the caller already read, so none is read here. */
  known?: PrintOptionsState | null
  /**
   * What Remember saves, when it is not `value`: a caller that seeds a value of its own
   * into what this send shows (Queue's manual start, #2288) passes the user's edits here,
   * so a save never remembers the seed for every later print.
   */
  toRemember?: PrintOptions
}

const UNSET = ''
/** A tri-state select's value for Bambuddy's default: Radix's select takes no empty value. */
const DEFAULT = 'default'

/**
 * #3635 — options that change what happens after Print. Whenever one is on, it is
 * shown outside the collapsed disclosure, with where it was remembered.
 */
const HOLDING: readonly { name: OptionName; title: string; effect: string }[] = [
  {
    name: 'manual_start',
    title: 'Waits for a manual start.',
    effect: 'The printer holds this job until someone starts it there.',
  },
  {
    name: 'auto_off_after',
    title: 'Powers off afterwards.',
    effect: 'The printer switches off when this job ends.',
  },
]

/**
 * Issue #88 — the "Options" disclosure, collapsed by default.
 *
 * Every row is one option field of Bambuddy's own `PrintQueueItemCreate`, showing the
 * value that will actually go out once global, per-printer, per-model and this send have
 * been merged, and where that value came from. "Bambuddy's default" is a real choice, not
 * an absence: picking it removes the override rather than pinning the current default.
 */
export function PrintOptionsDisclosure({
  subject,
  printerId,
  value,
  onChange,
  onEffective,
  heldOnly = false,
  known,
  toRemember = value,
}: Props) {
  const slug = subject?.key
  const noun = subject?.noun ?? 'model'
  // Typed as the narrower view, because that is what the PUT answers with. The printer
  // the scope keys on is kept separately: only the GET resolves it, so reading it off
  // this object would make it vanish the moment a save lands — and `printer_id` being
  // optional in the generated types means TypeScript would not have said a word.
  const [read, setRemembered] = useState<PrintOptionsView | null>(null)
  const [readPrinterId, setServerPrinterId] = useState<number | null>(null)
  // Simple mode's held list takes the dialog's own read rather than reading again
  // (#2361: one read per open); a Stop remembering's answer then replaces it here.
  const remembered = heldOnly ? (read ?? known ?? null) : read
  const serverPrinterId = heldOnly ? (known?.printer_id ?? null) : readPrinterId
  const [loading, setLoading] = useState(true)
  const [scope, setScope] = useState<OptionScope>('printer')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [savedScope, setSavedScope] = useState<OptionScope | null>(null)

  useEffect(() => {
    if (heldOnly) return
    let live = true
    api
      .getPrintOptions()
      .then((view) => {
        if (!live) return
        setRemembered(view)
        setServerPrinterId(view.printer_id ?? null)
      })
      .catch((cause: unknown) =>
        live && setError(cause instanceof ApiError ? cause.detail : 'Could not read the options.'),
      )
      .finally(() => live && setLoading(false))
    return () => {
      live = false
    }
  }, [slug, heldOnly])

  // The per-printer scope only applies once the printer is known; with none chosen and
  // none in Settings, saving there would go nowhere — hence the disabled option and the note below.
  const resolvedPrinterId = printerId ?? serverPrinterId
  const printerKey = resolvedPrinterId === null ? null : String(resolvedPrinterId)
  // The scope state can still say "This printer" when no printer is known (it opens on
  // it). Falling back here means a click always does
  // something visible, rather than hitting a guard and changing nothing on screen.
  const fallback: OptionScope = slug ? 'model' : 'global'
  const activeScope: OptionScope =
    (scope === 'printer' && !printerKey) || (scope === 'model' && !slug) ? fallback : scope

  const layers = useMemo<OptionLayer[]>(
    () => [
      { scope: 'global', options: remembered?.global_options },
      { scope: 'printer', options: printerKey ? remembered?.printers?.[printerKey] : undefined },
      { scope: 'model', options: slug ? remembered?.models?.[slug] : undefined },
      { scope: 'request', options: value },
    ],
    [remembered, printerKey, slug, value],
  )

  const effective = useMemo(
    () => resolveOptions(...layers.map((layer) => layer.options)),
    [layers],
  )
  const defaults = remembered?.defaults ?? {}

  // Reported rather than recomputed by the caller, so there is one merge in the UI.
  useEffect(() => onEffective?.(effective), [effective, onEffective])

  function set(name: OptionName, next: OptionValue) {
    const draft: Record<string, OptionValue> = { ...value }
    if (isSet(next)) draft[name] = next
    else delete draft[name]
    onChange(draft as PrintOptions)
  }

  function storedFor(target: OptionScope): PrintOptions | undefined {
    if (target === 'global') return remembered?.global_options
    if (target === 'printer') return printerKey ? remembered?.printers?.[printerKey] : undefined
    return slug ? remembered?.models?.[slug] : undefined
  }

  async function remember(clear: boolean) {
    setBusy(true)
    setError(null)
    setSavedScope(null)
    try {
      const view = await api.putPrintOptions({
        scope: activeScope,
        key:
          activeScope === 'global' ? null : activeScope === 'printer' ? printerKey : (slug ?? null),
        // Merged with what that scope already holds, never just this dialog's edits: the
        // PUT replaces a scope wholesale, so sending only the current overlay would
        // delete every option remembered there on an earlier visit.
        options: clear ? {} : resolveOptions(storedFor(activeScope), toRemember),
      })
      setRemembered(view)
      setSavedScope(activeScope)
      if (!clear) onChange({})
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.detail : 'Could not save the options.')
    } finally {
      setBusy(false)
    }
  }

  /** Drops one option from the scope that remembered it, keeping the rest there (#3635). */
  async function stopRemembering(name: OptionName, target: OptionScope) {
    setBusy(true)
    setError(null)
    setSavedScope(null)
    try {
      const kept: Record<string, OptionValue> = { ...storedFor(target) }
      delete kept[name]
      const view = await api.putPrintOptions({
        scope: target,
        key: target === 'global' ? null : target === 'printer' ? printerKey : (slug ?? null),
        options: kept as PrintOptions,
      })
      setRemembered(view)
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.detail : 'Could not save the options.')
    } finally {
      setBusy(false)
    }
  }

  const changedSpecs = PRINT_OPTIONS.filter((spec) => isNonDefault(spec.name, effective, defaults))
  // #3635 — named, and marked when an earlier print remembered them, so "2 changes"
  // never hides a Manual start nobody chose for this print.
  const summary = changedSpecs
    .map((spec) => {
      const shown = optionValue(effective, spec.name)
      const text = shown === true ? spec.label : `${spec.label}: ${formatOption(spec.kind, shown)}`
      return effectiveScope(layers, spec.name) === 'request' ? text : `${text} (remembered)`
    })
    .join(', ')
  // What one click sets back to Bambuddy's defaults for this print: not Quantity, which
  // is the Copies box's, nor an option with no known default to go back to.
  const resettable = changedSpecs.filter(
    (spec) => spec.name !== 'quantity' && isSet(optionValue(defaults, spec.name)),
  )
  const holding = HOLDING.filter((hold) => optionValue(effective, hold.name) === true)

  const held = holding.length > 0 && (
    <ul className="mt-4 space-y-1.5" aria-label="Options that change this print">
      {holding.map((hold) => {
        const source = effectiveScope(layers, hold.name)
        const stored = source !== null && source !== 'request' ? source : null
        return (
          <li
            key={hold.name}
            className="flex flex-wrap items-center gap-x-2 gap-y-1 rounded-lg border border-warn/40 bg-warn/10 px-3 py-2 text-[13px] text-warn"
            data-testid={`holding-${hold.name}`}
          >
            <span className="min-w-0 flex-[1_1_16rem]">
              <span className="font-medium">{hold.title}</span> {hold.effect}
              {stored &&
                ` Remembered for ${stored === 'global' ? 'every print' : scopeLabel(stored, noun)}.`}
            </span>
            <Button
              type="button"
              size="sm"
              variant="outline"
              onClick={() => set(hold.name, optionValue(defaults, hold.name) ?? false)}
              disabled={busy}
            >
              Not this time
            </Button>
            {stored && (
              <Button
                type="button"
                size="sm"
                variant="ghost"
                onClick={() => void stopRemembering(hold.name, stored)}
                disabled={busy}
              >
                Stop remembering
              </Button>
            )}
          </li>
        )
      })}
    </ul>
  )

  if (heldOnly) {
    return (
      <>
        {held}
        {held && error && (
          <p role="alert" className="mt-2 text-xs text-destructive">
            {error}
          </p>
        )}
      </>
    )
  }

  return (
    <>
      {held}
      <Collapsible asChild>
        <Card size="sm" className="group/options mt-4 gap-0 py-0" data-testid="print-options">
        <CollapsibleTrigger className="flex w-full cursor-pointer items-center gap-2 rounded-xl px-3 py-2.5 text-left text-sm outline-none focus-visible:ring-3 focus-visible:ring-ring/50">
          <span className="font-medium">Options</span>
          <span className="min-w-0 flex-1 truncate text-xs text-muted-foreground" data-testid="print-options-summary">
            {changedSpecs.length === 0 ? "Bambuddy's defaults" : summary}
          </span>
          <ChevronDownIcon
            aria-hidden="true"
            className="size-4 shrink-0 text-muted-foreground transition-transform group-data-[state=open]/options:rotate-180"
          />
        </CollapsibleTrigger>

        <CollapsibleContent className="border-t px-3 py-2">
          {loading ? (
            <div className="space-y-2 py-2" data-testid="print-options-loading">
              <p className="text-sm text-muted-foreground">Loading the remembered options</p>
              <Skeleton className="h-8 w-full" />
              <Skeleton className="h-8 w-full" />
            </div>
          ) : (
            <>
              <ul className="divide-y">
                {PRINT_OPTIONS.map((spec) => (
                  <OptionRow
                    key={spec.name}
                    spec={spec}
                    effective={optionValue(effective, spec.name)}
                    fallback={optionValue(defaults, spec.name)}
                    source={effectiveScope(layers, spec.name)}
                    noun={noun}
                    nonDefault={isNonDefault(spec.name, effective, defaults)}
                    onChange={(next) => set(spec.name, next)}
                  />
                ))}
              </ul>

              <div className="mt-3 flex flex-wrap items-center gap-2">
                <Label htmlFor="option-scope" className="text-xs font-normal text-muted-foreground">
                  Remember for
                </Label>
                <Select value={activeScope} onValueChange={(next) => setScope(next as OptionScope)}>
                  <SelectTrigger id="option-scope">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="printer" disabled={!printerKey}>
                      This printer
                    </SelectItem>
                    {slug && <SelectItem value="model">This {noun}</SelectItem>}
                    <SelectItem value="global">Every print</SelectItem>
                  </SelectContent>
                </Select>
                <Button type="button" variant="outline" onClick={() => void remember(false)} disabled={busy || !remembered}>
                  {busy && <Spinner />}
                  Remember
                </Button>
                <Button type="button" variant="outline" onClick={() => void remember(true)} disabled={busy}>
                  Forget
                </Button>
                {savedScope && !error && (
                  <span role="status" className="text-xs text-ok">
                    Remembered for {scopeLabel(savedScope, noun)}.
                  </span>
                )}
              </div>

              <p className="mt-1 text-xs text-muted-foreground">
                Remember merges these into what that scope already holds; Forget clears the
                whole scope.
              </p>
              {resettable.length > 0 && (
                <Button
                  type="button"
                  size="sm"
                  variant="ghost"
                  className="mt-2"
                  onClick={() => {
                    const draft: Record<string, OptionValue> = { ...value }
                    for (const spec of resettable) draft[spec.name] = optionValue(defaults, spec.name)
                    onChange(draft as PrintOptions)
                  }}
                >
                  Use Bambuddy's defaults for this print
                </Button>
              )}
              {!remembered && (
                <p className="mt-2 text-xs text-destructive">
                  What is already remembered could not be read, so Remember is unavailable —
                  it would replace that scope with only these edits. Forget still works.
                </p>
              )}
              {!printerKey && (
                <p className="mt-2 text-xs text-muted-foreground">
                  No printer is picked yet, so options can only be remembered{' '}
                  {slug ? `for this ${noun} or every print` : 'for every print'}.
                </p>
              )}
              {error && (
                <p role="alert" className="mt-2 text-xs text-destructive">
                  {error}
                </p>
              )}
            </>
          )}
        </CollapsibleContent>
        </Card>
      </Collapsible>
    </>
  )
}

function OptionRow({
  spec,
  effective,
  fallback,
  source,
  noun,
  nonDefault,
  onChange,
}: {
  spec: OptionSpec
  effective: OptionValue
  fallback: OptionValue
  source: OptionLayer['scope'] | null
  noun: OptionsSubject['noun']
  nonDefault: boolean
  onChange: (next: OptionValue) => void
}) {
  const id = `option-${spec.name}`
  // One string, not several nodes: the readout is what the tests and a screen reader
  // both read, and splitting it makes both of them see fragments.
  const readout = [
    // The effective value, which for an option nobody set is Bambuddy's own default —
    // that is what the print will use, so that is what the row has to say.
    formatOption(spec.kind, isSet(effective) ? effective : fallback),
    source ? `from ${scopeLabel(source, noun)}` : "Bambuddy's default",
    spec.hint,
  ]
    .filter(Boolean)
    .join(' · ')
  return (
    <li className="flex items-center gap-3 py-2">
      <div className="min-w-0 flex-1">
        <div className="flex items-baseline gap-2">
          {/* The badge sits outside the label so the label's accessible name stays
              exactly the option's name. */}
          <Label htmlFor={id} className="font-normal">
            {spec.label}
          </Label>
          {nonDefault && (
            <Badge variant="outline" data-testid={`non-default-${spec.name}`}>
              changed
            </Badge>
          )}
        </div>
        <span className="text-[11px] text-muted-foreground">{readout}</span>
      </div>
      <OptionControl id={id} spec={spec} value={effective} fallback={fallback} onChange={onChange} />
    </li>
  )
}

function OptionControl({
  id,
  spec,
  value,
  fallback,
  onChange,
}: {
  id: string
  spec: OptionSpec
  value: OptionValue
  fallback: OptionValue
  onChange: (next: OptionValue) => void
}) {
  const defaultLabel =
    isSet(fallback) ? `Bambuddy default (${formatOption(spec.kind, fallback)})` : 'Bambuddy default'

  if (spec.kind === 'number') {
    return (
      <Input
        id={id}
        type="number"
        min={spec.min}
        max={spec.max}
        step={1}
        value={isSet(value) ? String(value) : UNSET}
        placeholder={isSet(fallback) ? String(fallback) : ''}
        onChange={(event) =>
          onChange(event.target.value === UNSET ? null : clampOption(event.target.value, spec))
        }
        className="sb-num w-24 text-right"
      />
    )
  }

  const choices: readonly string[] =
    spec.kind === 'calibration'
      ? CALIBRATION_CHOICES
      : spec.kind === 'preheat'
        ? PREHEAT_CHOICES
        : ['true', 'false']

  return (
    <Select
      value={isSet(value) ? String(value) : DEFAULT}
      onValueChange={(next) => {
        if (next === DEFAULT) return onChange(null)
        return onChange(spec.kind === 'boolean' ? next === 'true' : next)
      }}
    >
      <SelectTrigger id={id} className="w-40">
        <SelectValue />
      </SelectTrigger>
      <SelectContent>
        <SelectItem value={DEFAULT}>{defaultLabel}</SelectItem>
        {choices.map((choice) => (
          <SelectItem key={choice} value={choice}>
            {formatOption(spec.kind, spec.kind === 'boolean' ? choice === 'true' : choice)}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  )
}
