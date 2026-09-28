import { useEffect, useState, type ReactNode } from 'react'
import { USER_ONLY } from '../../agent/dom'
import type { Settings, SettingSource } from '../../api/types'
import { Button } from '../../components/ui/Button'
import { Spinner } from '../../components/ui/Spinner'
import {
  BYTE_UNITS,
  LOG_LEVELS,
  bestUnit,
  envVar,
  hasKey,
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
          className="text-[11px] text-accent underline-offset-2 hover:underline disabled:opacity-50"
          aria-label={`Reset ${name} to the deployment value`}
          {...USER_ONLY}
        >
          Reset to deployment value
        </button>
      )}
    </span>
  )
}

/** A labelled field with its badge above and its help or error below. */
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
  return (
    <div>
      <div className="flex flex-wrap items-center justify-between gap-x-3 gap-y-1">
        <label htmlFor={id} className="block text-[13px]">
          {label}
        </label>
        {badge}
      </div>
      <div className="mt-1.5">{children}</div>
      {error ? (
        <p role="alert" className="mt-1.5 text-[12px] text-warn">
          {error}
        </p>
      ) : (
        help && <p className="mt-1.5 text-[12px] text-muted">{help}</p>
      )}
    </div>
  )
}

/** Bytes, edited in MB or GB (binary, as the server's own messages count them). */
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

  // Follow the value when it changes from outside (a discard, a reload).
  useEffect(() => {
    const shown = text === '' ? '' : String(Math.round(Number(text) * BYTE_UNITS[unit]))
    if (shown === value) return
    const next = value === '' ? unit : bestUnit(Number(value))
    setUnit(next)
    setText(value === '' ? '' : inUnit(Number(value), next))
    // Only the outside value decides; the text and unit are this input's own.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [value])

  function emit(nextText: string, nextUnit: ByteUnit) {
    setText(nextText)
    setUnit(nextUnit)
    const parsed = Number(nextText)
    onChange(nextText.trim() === '' || !Number.isFinite(parsed) ? nextText : String(Math.round(parsed * BYTE_UNITS[nextUnit])))
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
        onChange={(event) => emit(event.target.value, unit)}
        className="sb-field sb-num"
      />
      <select
        aria-label={`Unit for ${id}`}
        value={unit}
        onChange={(event) => emit(text, event.target.value as ByteUnit)}
        className="sb-field w-24 cursor-pointer"
      >
        <option value="MB">MB</option>
        <option value="GB">GB</option>
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
      const stored = Boolean(settings[hasKey(spec.name as 'bambuddy_api_key' | 'google_fonts_api_key')])
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
  return (
    <section
      id={id}
      aria-labelledby={headingId}
      className="mt-4 scroll-mt-14 rounded-[6px] border border-line bg-surface first:mt-0"
    >
      <div className="flex items-center gap-2 border-b border-line px-4 py-2.5">
        <h2 id={headingId} className="text-[13px] font-medium">
          {title}
        </h2>
        {dirty && (
          <span className="rounded-full bg-accent/15 px-2 py-0.5 text-[11px] text-accent" data-testid={`${id}-unsaved`}>
            Unsaved
          </span>
        )}
      </div>
      <div className="space-y-4 p-4">
        {description && <p className="text-[13px] text-muted">{description}</p>}
        {children}
      </div>
      {onSave && (
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
