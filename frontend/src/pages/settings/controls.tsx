import { createContext, useContext, useEffect, useLayoutEffect, useRef, useState, type ReactNode } from 'react'
import { USER_ONLY } from '../../agent/dom'
import type { Settings, SettingSource } from '../../api/types'
import { ChevronRightIcon } from 'lucide-react'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardFooter } from '@/components/ui/card'
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Switch } from '@/components/ui/switch'
import { cn } from '@/lib/utils'
import { ChoiceSelect, Tip } from '../../components/settings/form'
import { Spinner } from '../../components/ui/Spinner'
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
  stored: 'border-primary/40 bg-primary/10 text-foreground',
  env: 'bg-secondary text-secondary-foreground',
  default: 'border-border text-muted-foreground',
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
  const pill = (
    <Badge variant="outline" className={BADGE[source]}>
      {label}
    </Badge>
  )
  return (
    <span className="flex flex-wrap items-center gap-1.5" data-testid={`source-${name}`}>
      {source === 'cleared' ? (
        <Tip content={`${envVar(name)} is ignored while this is cleared`}>
          {/* Focusable, so the tooltip is reached from the keyboard too. */}
          <Badge variant="outline" tabIndex={0} className={BADGE[source]}>
            {label}
          </Badge>
        </Tip>
      ) : (
        pill
      )}
      {source === 'cleared' && <span className="text-[11px] text-muted-foreground">{envVar(name)} is ignored</span>}
      {settings.applies?.[name] === 'restart' && (
        <Badge
          variant="outline"
          className={restart ? 'border-warn/40 bg-warn/10 text-warn' : 'text-muted-foreground'}
        >
          {restart ? 'Saved; restart ScadBuddy to apply' : 'Applies on restart'}
        </Badge>
      )}
      {(source === 'stored' || source === 'cleared') && (
        <Button
          type="button"
          variant="link"
          size="xs"
          onClick={onReset}
          disabled={resetting}
          // #1035 — 24 px tall: a one-tap control that discards a stored setting.
          className="h-6 px-0 text-[11px]"
          // #2456 — the visible text first (WCAG 2.5.3), then the field it resets by its label.
          aria-label={`Reset to deployment value: ${fieldLabel ?? name}`}
          {...USER_ONLY}
        >
          Reset to deployment value
        </Button>
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
  // input), so its description is set on it here rather than through every caller. It
  // runs after every render of this row, and only then: a control that re-renders on its
  // own and writes its own `aria-describedby` drops the help and error ids until the row
  // renders again. No control here sets that prop; one that must should take the ids as
  // props instead.
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
        <Label htmlFor={id} className="font-normal">
          {label}
        </Label>
        <FieldLabel.Provider value={label}>{badge}</FieldLabel.Provider>
      </div>
      <div className="mt-1.5">{children}</div>
      {error && (
        <p id={errorId} role="alert" className="mt-1.5 text-xs text-warn">
          {error}
        </p>
      )}
      {help && (
        <p id={helpId} className="mt-1.5 text-xs text-muted-foreground">
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
      <Input
        id={id}
        type="number"
        min={0}
        step="any"
        inputMode="decimal"
        value={text}
        onChange={(event) => editNumber(event.target.value)}
        className="sb-num"
      />
      <ChoiceSelect
        aria-label={`Unit for ${id}`}
        value={unit}
        onValueChange={(next) => changeUnit(next as ByteUnit)}
        choices={(Object.keys(BYTE_UNITS) as ByteUnit[]).map((name) => ({ value: name, label: name }))}
        className="w-24 shrink-0"
      />
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
      return <Switch id={id} checked={value === 'true'} onCheckedChange={(checked) => onChange(String(checked))} />
    case 'level':
      return (
        <ChoiceSelect
          id={id}
          value={value}
          onValueChange={onChange}
          choices={LOG_LEVELS.map((level) => ({ value: level, label: level }))}
        />
      )
    case 'secret': {
      const stored = Boolean(settings[hasKey(spec.name as SecretName)])
      return (
        <div className="flex gap-2">
          <Input
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
            className="sb-num"
          />
          {stored && onClear && (
            <Button type="button" variant="outline" onClick={onClear} disabled={clearing} {...USER_ONLY}>
              Remove key
            </Button>
          )}
        </div>
      )
    }
    case 'seconds':
      return (
        <div className="flex items-center gap-2">
          <Input
            id={id}
            type="number"
            min={0}
            step="any"
            inputMode="decimal"
            value={value}
            onChange={(event) => onChange(event.target.value)}
            className="sb-num"
          />
          <span className="shrink-0 text-xs text-muted-foreground">
            s{value && humanSeconds(Number(value)) ? ` · ${humanSeconds(Number(value))}` : ''}
          </span>
        </div>
      )
    case 'count':
      return (
        <Input
          id={id}
          type="number"
          min={0}
          step={1}
          inputMode="numeric"
          value={value}
          onChange={(event) => onChange(event.target.value)}
          className="sb-num"
        />
      )
    default:
      return (
        <Input id={id} type="text" value={value} onChange={(event) => onChange(event.target.value)} className="sb-num" />
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
    <Collapsible open={open} onOpenChange={(next) => next !== open && toggle()} asChild>
      <section id={id} data-collapsible="" aria-labelledby={headingId} className="mt-4 scroll-mt-14 first:mt-0">
        <Card className="gap-0 py-0">
        <div className={cn('flex min-h-11 items-center gap-2 px-4 py-1.5', open && 'border-b')}>
          {/* #2346 — focusable from script only: a section link moves focus here. */}
          <h2 id={headingId} tabIndex={-1} className="font-heading text-sm font-medium outline-none">
            <CollapsibleTrigger aria-controls={`${id}-body`} className="flex min-h-6 items-center gap-1.5 text-left">
              <ChevronRightIcon
                aria-hidden="true"
                className={cn('size-4 shrink-0 text-muted-foreground transition-transform', open && 'rotate-90')}
              />
              {title}
            </CollapsibleTrigger>
          </h2>
          {dirty && (
            <Badge className="bg-primary/15 text-primary" data-testid={`${id}-unsaved`}>
              Unsaved
            </Badge>
          )}
        </div>
        <CollapsibleContent forceMount hidden={!open} id={`${id}-body`} className="space-y-4 p-4">
          {description && <p className="text-sm text-muted-foreground">{description}</p>}
          {children}
        </CollapsibleContent>
        {onSave && (open || dirty) && (
          <CardFooter className="flex-wrap gap-2 px-4 py-2.5">
            <Button
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
            {savedAt && !dirty && <span className="text-xs text-ok">Saved at {savedAt}</span>}
          </CardFooter>
        )}
      </Card>
      </section>
    </Collapsible>
  )
}
