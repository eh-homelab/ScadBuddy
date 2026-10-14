import { createContext, useContext, useEffect, useLayoutEffect, useRef, useState, type ReactNode } from 'react'
import { USER_ONLY } from '../../agent/dom'
import type { Settings, SettingSource } from '../../api/types'
import { Button } from '../../components/ui/Button'
import { Spinner } from '../../components/ui/Spinner'
import { Chevron } from '../../components/ui/Chevron'
import { useCollapsible } from './collapseState'
import {
  BYTE_UNITS,
  LOG_LEVELS,
  bestUnit,
  envVar,
  hasKey,
  type SecretName,
  humanSeconds,
  inUnit,
  type ByteUnit,
  type FieldSpec,
} from './fields'

const BADGE: Record<SettingSource, string> = {
  stored: 'border-accent/40 bg-accent/10 text-ink',
  env: 'border-line bg-surface-2 text-ink',
  default: 'border-line bg-transparent text-muted',
  cleared: 'border-warn/40 bg-warn/10 text-warn',
}

/** The label of the field a badge sits in (#2456): what its Reset button is named after. */
const FieldLabel = createContext<string | null>(null)

/** #322 — where a value came from, and the way back to the deployment's value. */
export function SourceBadge({
  name,
  settings,
  onReset,
  resetting,
}: {
  name: string
  settings: Settings
  onReset: () => void
  resetting: boolean
}) {
  const fieldLabel = useContext(FieldLabel)
  const source = settings.sources?.[name]
  if (!source) return null
  const label =
    source === 'stored'
      ? 'Set here'
      : source === 'env'
        ? `From ${envVar(name)}`
        : source === 'cleared'
          ? 'Cleared'
          : 'Default'
  const restart = settings.restart_required?.includes(name)
  return (
    <span className="flex flex-wrap items-center gap-1.5" data-testid={`source-${name}`}>
      <span
        className={`inline-flex h-5 items-center rounded-full border px-2 text-[11px] leading-none ${BADGE[source]}`}
        title={source === 'cleared' ? `${envVar(name)} is ignored while this is cleared` : undefined}
      >
        {label}
      </span>
      {source === 'cleared' && (
        <span className="text-[11px] text-muted">{envVar(name)} is ignored</span>
      )}
      {settings.applies?.[name] === 'restart' && (
        <span
          className={`inline-flex h-5 items-center rounded-full border px-2 text-[11px] leading-none ${
            restart ? 'border-warn/40 bg-warn/10 text-warn' : 'border-line text-muted'
          }`}
        >
          {restart ? 'Saved; restart ScadBuddy to apply' : 'Applies on restart'}
        </span>
      )}
      {(source === 'stored' || source === 'cleared') && (
        <button
          type="button"
          onClick={onReset}
          disabled={resetting}
          // #1035 — 24 px tall: a one-tap control that discards a stored setting.
          className="inline-flex min-h-6 items-center text-[11px] text-accent underline-offset-2 hover:underline disabled:opacity-50"
          // #2456 — the visible text first (WCAG 2.5.3), then the field it resets by its label.
          aria-label={`Reset to deployment value: ${fieldLabel ?? name}`}
          {...USER_ONLY}
        >
          Reset to deployment value
        </button>
      )}
    </span>
  )
}

/**
 * A labelled field with its badge above and its help and error below. #2347 — the control
 * (the element with `id`) is described by both, and marked invalid while there is an
 * error; the help stays shown beside the error, since it is often what explains it.
 */
export function FieldRow({
  id,
  label,
  badge,
  help,
  error,
  children,
}: {
  id: string
  label: string
  badge?: ReactNode
  help?: ReactNode
  error?: string
  children: ReactNode
}) {
  const row = useRef<HTMLDivElement>(null)
  const helpId = `${id}-help`
  const errorId = `${id}-error`
  // The control is whatever the caller rendered with `id` (an input, a select, the bytes
  // input), so its description is set on it here rather than through every caller.
  useLayoutEffect(() => {
    const control = row.current?.querySelector<HTMLElement>(`#${CSS.escape(id)}`)
    if (!control) return
    const own = new Set([helpId, errorId])
    const others = (control.getAttribute('aria-describedby') ?? '').split(/\s+/).filter((token) => token && !own.has(token))
    const ids = [...others, ...(help ? [helpId] : []), ...(error ? [errorId] : [])]
    if (ids.length > 0) control.setAttribute('aria-describedby', ids.join(' '))
    else control.removeAttribute('aria-describedby')
    if (error) control.setAttribute('aria-invalid', 'true')
    else control.removeAttribute('aria-invalid')
  })
  return (
    <div ref={row}>
      <div className="flex flex-wrap items-center justify-between gap-x-3 gap-y-1">
        <label htmlFor={id} className="block text-[13px]">
          {label}
        </label>
        <FieldLabel.Provider value={label}>{badge}</FieldLabel.Provider>
      </div>
      <div className="mt-1.5">{children}</div>
      {error && (
        <p id={errorId} role="alert" className="mt-1.5 text-[12px] text-warn">
          {error}
        </p>
      )}
      {help && (
        <p id={helpId} className="mt-1.5 text-[12px] text-muted">
          {help}
        </p>
      )}
    </div>
  )
}

/** Bytes, edited in MB or GB (or their binary MiB and GiB), and stored as bytes. */
export function BytesInput({
  id,
  value,
  onChange,
}: {
  id: string
  value: string
  onChange: (bytes: string) => void
}) {
  const bytes = Number(value)
  const [unit, setUnit] = useState<ByteUnit>(() => (value === '' ? 'MB' : bestUnit(bytes)))
  const [text, setText] = useState(() => (value === '' ? '' : inUnit(bytes, unit)))
  // The value the shown text stands for. A unit change converts the text for display
  // only, so the text alone cannot say whether the value changed from outside.
  const own = useRef(value)

  // Follow the value when it changes from outside (a discard, a reload).
  useEffect(() => {
    if (own.current === value) return
    own.current = value
    const next = value === '' ? unit : bestUnit(Number(value))
    setUnit(next)
    setText(value === '' ? '' : inUnit(Number(value), next))
    // Only the outside value decides; the text and unit are this input's own.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [value])

  // Editing the number reinterprets it under the current unit.
  function editNumber(nextText: string) {
    setText(nextText)
    const parsed = Number(nextText)
    const next = nextText.trim() === '' || !Number.isFinite(parsed) ? nextText : String(Math.round(parsed * BYTE_UNITS[unit]))
    own.current = next
    onChange(next)
  }

  // Changing the unit keeps the byte value and converts the shown number.
  function changeUnit(nextUnit: ByteUnit) {
    setUnit(nextUnit)
    if (value.trim() !== '' && Number.isFinite(bytes)) setText(inUnit(bytes, nextUnit))
  }

  return (
    <div className="flex gap-2">
      <input
        id={id}
        type="number"
        min={0}
        step="any"
        inputMode="decimal"
        value={text}
        onChange={(event) => editNumber(event.target.value)}
        className="sb-field sb-num"
      />
      <select
        aria-label={`Unit for ${id}`}
        value={unit}
        onChange={(event) => changeUnit(event.target.value as ByteUnit)}
        className="sb-field w-24 cursor-pointer"
      >
        {(Object.keys(BYTE_UNITS) as ByteUnit[]).map((name) => (
          <option key={name} value={name}>
            {name}
          </option>
        ))}
      </select>
    </div>
  )
}

/** One runtime setting's control, chosen by its kind. */
export function RuntimeInput({
  spec,
  value,
  onChange,
  settings,
  clearing,
  onClear,
}: {
  spec: FieldSpec
  value: string
  onChange: (next: string) => void
  settings: Settings
  clearing?: boolean
  onClear?: () => void
}) {
  const id = `setting-${spec.name}`
  switch (spec.kind) {
    case 'bytes':
      return <BytesInput id={id} value={value} onChange={onChange} />
    case 'bool':
      return (
        <input
          id={id}
          type="checkbox"
          checked={value === 'true'}
          onChange={(event) => onChange(String(event.target.checked))}
        />
      )
    case 'level':
      return (
        <select
          id={id}
          value={value}
          onChange={(event) => onChange(event.target.value)}
          className="sb-field cursor-pointer"
        >
          {LOG_LEVELS.map((level) => (
            <option key={level} value={level}>
              {level}
            </option>
          ))}
        </select>
      )
    case 'secret': {
      const stored = Boolean(settings[hasKey(spec.name as SecretName)])
      return (
        <div className="flex gap-2">
          <input
            id={id}
            type="password"
            autoComplete="off"
            value={value}
            onChange={(event) => onChange(event.target.value)}
            placeholder={
              clearing
                ? 'Cleared when you save.'
                : stored
                  ? 'A key is stored. Paste a new one to replace it.'
                  : 'Paste the key'
            }
            className="sb-field sb-num"
          />
          {stored && onClear && (
            <Button size="sm" variant="ghost" onClick={onClear} disabled={clearing} {...USER_ONLY}>
              Remove key
            </Button>
          )}
        </div>
      )
    }
    case 'seconds':
      return (
        <div className="flex items-center gap-2">
          <input
            id={id}
            type="number"
            min={0}
            step="any"
            inputMode="decimal"
            value={value}
            onChange={(event) => onChange(event.target.value)}
            className="sb-field sb-num"
          />
          <span className="shrink-0 text-[12px] text-muted">
            s{value && humanSeconds(Number(value)) ? ` · ${humanSeconds(Number(value))}` : ''}
          </span>
        </div>
      )
    case 'count':
      return (
        <input
          id={id}
          type="number"
          min={0}
          step={1}
          inputMode="numeric"
          value={value}
          onChange={(event) => onChange(event.target.value)}
          className="sb-field sb-num"
        />
      )
    default:
      return (
        <input
          id={id}
          type="text"
          value={value}
          onChange={(event) => onChange(event.target.value)}
          className="sb-field sb-num"
        />
      )
  }
}

/** A section: a heading with its unsaved marker, the body, and Save / Discard. */
export function Section({
  id,
  title,
  description,
  dirty,
  saving,
  savedAt,
  onSave,
  onDiscard,
  children,
}: {
  id: string
  title: string
  description?: ReactNode
  dirty?: boolean
  saving?: boolean
  savedAt?: string | null
  onSave?: () => void
  onDiscard?: () => void
  children: ReactNode
}) {
  const headingId = `${id}-heading`
  // #2173 — folded from its heading, remembered per browser. Folding only hides the body:
  // its fields keep their edits, and a dirty section keeps its badge and its Save bar.
  const { open, toggle } = useCollapsible(id)
  return (
    <section
      id={id}
      data-collapsible=""
      aria-labelledby={headingId}
      className="mt-4 scroll-mt-14 rounded-[6px] border border-line bg-surface first:mt-0"
    >
      <div className={`flex items-center gap-2 px-4 py-1.5 ${open ? 'border-b border-line' : ''}`}>
        {/* #2346 — focusable from script only: a section link moves focus here. */}
        <h2 id={headingId} tabIndex={-1} className="text-[13px] font-medium outline-none">
          <button
            type="button"
            aria-expanded={open}
            aria-controls={`${id}-body`}
            onClick={toggle}
            className="flex min-h-6 items-center gap-1.5 text-left"
          >
            <Chevron open={open} />
            {title}
          </button>
        </h2>
        {dirty && (
          <span className="rounded-full bg-accent/15 px-2 py-0.5 text-[11px] text-accent" data-testid={`${id}-unsaved`}>
            Unsaved
          </span>
        )}
      </div>
      <div id={`${id}-body`} hidden={!open} className="space-y-4 p-4">
        {description && <p className="text-[13px] text-muted">{description}</p>}
        {children}
      </div>
      {onSave && (open || dirty) && (
        <div className="flex items-center gap-2 border-t border-line px-4 py-2.5">
          <Button
            variant="primary"
            size="sm"
            onClick={onSave}
            disabled={!dirty || saving}
            aria-busy={saving}
            aria-label={`Save ${title}`}
            {...USER_ONLY}
          >
            {saving && <Spinner />}
            Save
          </Button>
          <Button
            size="sm"
            variant="ghost"
            onClick={onDiscard}
            disabled={!dirty || saving}
            aria-label={`Discard ${title} changes`}
          >
            Discard changes
          </Button>
          {savedAt && !dirty && <span className="text-[12px] text-ok">Saved at {savedAt}</span>}
        </div>
      )}
    </section>
  )
}
