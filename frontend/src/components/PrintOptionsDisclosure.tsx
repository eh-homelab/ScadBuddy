import { useEffect, useMemo, useState } from 'react'
import { api, ApiError } from '../api/client'
import type { OptionScope, PrintOptions, PrintOptionsState } from '../api/types'
import {
  CALIBRATION_CHOICES,
  effectiveScope,
  formatOption,
  isNonDefault,
  isSet,
  optionValue,
  PREHEAT_CHOICES,
  PRINT_OPTIONS,
  resolveOptions,
  SCOPE_LABELS,
  type OptionLayer,
  type OptionName,
  type OptionSpec,
  type OptionValue,
} from '../lib/printOptions'
import { Button } from './ui/Button'
import { Spinner } from './ui/Spinner'

interface Props {
  /** The model being sent — the key the per-model scope is remembered under. */
  slug: string
  /**
   * The printer the send will reach, when the caller already knows it — #86's picker
   * will. Left undefined the server says which one the per-printer scope keys on,
   * resolving a configured pipeline's target the same way the send does.
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
}

const UNSET = ''

/**
 * Issue #88 — the "Options" disclosure, collapsed by default.
 *
 * Every row is one option field of Bambuddy's own `PrintQueueItemCreate`, showing the
 * value that will actually go out once global, per-printer, per-model and this send have
 * been merged, and where that value came from. "Bambuddy's default" is a real choice, not
 * an absence: picking it removes the override rather than pinning the current default.
 */
export function PrintOptionsDisclosure({
  slug,
  printerId,
  value,
  onChange,
  onEffective,
}: Props) {
  const [remembered, setRemembered] = useState<PrintOptionsState | null>(null)
  const [loading, setLoading] = useState(true)
  const [scope, setScope] = useState<OptionScope>('printer')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [savedScope, setSavedScope] = useState<OptionScope | null>(null)

  useEffect(() => {
    let live = true
    api
      .getPrintOptions(slug)
      .then((view) => live && setRemembered(view))
      .catch((cause: unknown) =>
        live && setError(cause instanceof ApiError ? cause.detail : 'Could not read the options.'),
      )
      .finally(() => live && setLoading(false))
    return () => {
      live = false
    }
  }, [slug])

  // The per-printer scope only applies once the printer is known; with a printer-class
  // pipeline and no configured printer nothing knows it, and saving there would go
  // nowhere — hence the disabled option and the note below.
  const resolvedPrinterId = printerId ?? remembered?.printer_id ?? null
  const printerKey = resolvedPrinterId === null ? null : String(resolvedPrinterId)

  const layers = useMemo<OptionLayer[]>(
    () => [
      { scope: 'global', options: remembered?.global_options },
      { scope: 'printer', options: printerKey ? remembered?.printers?.[printerKey] : undefined },
      { scope: 'model', options: remembered?.models?.[slug] },
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

  async function remember(clear: boolean) {
    if (scope === 'printer' && !printerKey) return
    setBusy(true)
    setError(null)
    setSavedScope(null)
    try {
      const view = await api.putPrintOptions({
        scope,
        key: scope === 'global' ? null : scope === 'printer' ? printerKey : slug,
        options: clear ? {} : resolveOptions(value),
      })
      setRemembered(view)
      setSavedScope(scope)
      if (!clear) onChange({})
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.detail : 'Could not save the options.')
    } finally {
      setBusy(false)
    }
  }

  const changed = PRINT_OPTIONS.filter((spec) => isNonDefault(spec.name, effective, defaults)).length

  return (
    <details className="mt-4 rounded-[6px] border border-line bg-surface-2">
      <summary className="cursor-pointer px-3 py-2 text-[13px] text-ink">
        Options
        <span className="ml-2 text-[12px] text-muted">
          {changed === 0
            ? "Bambuddy's defaults"
            : `${changed} ${changed === 1 ? 'change' : 'changes'}`}
        </span>
      </summary>

      <div className="border-t border-line px-3 py-2">
        {loading ? (
          <p className="flex items-center gap-2 py-2 text-[13px] text-muted">
            <Spinner /> Loading the remembered options
          </p>
        ) : (
          <>
            <ul className="divide-y divide-line">
              {PRINT_OPTIONS.map((spec) => (
                <OptionRow
                  key={spec.name}
                  spec={spec}
                  effective={optionValue(effective, spec.name)}
                  fallback={optionValue(defaults, spec.name)}
                  source={effectiveScope(layers, spec.name)}
                  nonDefault={isNonDefault(spec.name, effective, defaults)}
                  onChange={(next) => set(spec.name, next)}
                />
              ))}
            </ul>

            <div className="mt-3 flex flex-wrap items-center gap-2">
              <label htmlFor="option-scope" className="text-[12px] text-muted">
                Remember for
              </label>
              <select
                id="option-scope"
                value={scope}
                onChange={(event) => setScope(event.target.value as OptionScope)}
                className="sb-field"
              >
                <option value="printer" disabled={!printerKey}>
                  This printer
                </option>
                <option value="model">This model</option>
                <option value="global">Every print</option>
              </select>
              <Button onClick={() => void remember(false)} disabled={busy}>
                {busy && <Spinner />}
                Remember
              </Button>
              <Button onClick={() => void remember(true)} disabled={busy}>
                Forget
              </Button>
              {savedScope && !error && (
                <span role="status" className="text-[12px] text-ok">
                  Remembered for {SCOPE_LABELS[savedScope]}.
                </span>
              )}
            </div>

            {!printerKey && (
              <p className="mt-2 text-[12px] text-faint">
                No printer is picked yet, so options can only be remembered for this model or
                every print.
              </p>
            )}
            {error && (
              <p role="alert" className="mt-2 text-[12px] text-warn">
                {error}
              </p>
            )}
          </>
        )}
      </div>
    </details>
  )
}

function OptionRow({
  spec,
  effective,
  fallback,
  source,
  nonDefault,
  onChange,
}: {
  spec: OptionSpec
  effective: OptionValue
  fallback: OptionValue
  source: OptionLayer['scope'] | null
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
    source ? `from ${SCOPE_LABELS[source]}` : "Bambuddy's default",
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
          <label htmlFor={id} className="text-[13px] text-ink">
            {spec.label}
          </label>
          {nonDefault && (
            <span
              data-testid={`non-default-${spec.name}`}
              className="rounded-[3px] border border-accent px-1 text-[11px] text-accent"
            >
              changed
            </span>
          )}
        </div>
        <span className="text-[11px] text-faint">{readout}</span>
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
      <input
        id={id}
        type="number"
        value={isSet(value) ? String(value) : UNSET}
        placeholder={isSet(fallback) ? String(fallback) : ''}
        onChange={(event) => onChange(event.target.value === UNSET ? null : Number(event.target.value))}
        className="sb-field sb-num w-24 text-right"
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
    <select
      id={id}
      value={isSet(value) ? String(value) : UNSET}
      onChange={(event) => {
        const next = event.target.value
        if (next === UNSET) return onChange(null)
        return onChange(spec.kind === 'boolean' ? next === 'true' : next)
      }}
      className="sb-field w-40"
    >
      <option value={UNSET}>{defaultLabel}</option>
      {choices.map((choice) => (
        <option key={choice} value={choice}>
          {formatOption(spec.kind, spec.kind === 'boolean' ? choice === 'true' : choice)}
        </option>
      ))}
    </select>
  )
}
