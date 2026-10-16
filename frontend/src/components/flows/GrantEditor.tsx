import { useId, useMemo, useState, type ReactNode } from 'react'
import { USER_ONLY } from '../../agent/dom'
import { api } from '../../api/client'
import type { BambuddyTargets, FlowGrantRule, FlowGrantSpec, ModelSummary } from '../../api/types'
import {
  BOUNDS,
  type Bound,
  grantProblems,
  type HostFn,
  HOUR,
  isUnbounded,
  MAX_GRANT_MS,
  MAX_STANDING_GRANT_MS,
  unboundedText,
} from '../../lib/grants'
import { useAsync, type AsyncState } from '../../lib/useAsync'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Skeleton } from '@/components/ui/skeleton'
import { Spinner } from '../ui/spinner'

/** A checkbox and its label, side by side. */
function Check({
  checked,
  onChange,
  children,
}: {
  checked: boolean
  onChange: (checked: boolean) => void
  children: ReactNode
}) {
  const id = useId()
  return (
    <div className="flex items-center gap-2">
      <Checkbox id={id} checked={checked} onCheckedChange={(next) => onChange(next === true)} />
      <Label htmlFor={id} className="font-normal">
        {children}
      </Label>
    </div>
  )
}

// -- the draft ------------------------------------------------------------------------

interface ArgDraft {
  path: string
  values: string
}

/** One rule as the form holds it: a list bound is null for "any", numbers as typed. */
interface RowDraft {
  key: string
  fn: HostFn
  tool: string | null
  on: boolean
  printers: number[] | null
  models: string[] | null
  libraryFiles: string | null
  maxPrints: string
  maxFilament: string
  maxCalls: string
  args: ArgDraft[]
}

const numberText = (value: number | null | undefined) => (value == null ? '' : String(value))

function rowOf(key: string, rule: FlowGrantRule, on: boolean): RowDraft {
  return {
    key,
    fn: rule.fn,
    tool: rule.tool ?? null,
    on,
    printers: rule.printers ?? null,
    models: rule.models ?? null,
    libraryFiles: rule.library_files == null ? null : rule.library_files.join(', '),
    maxPrints: numberText(rule.max_prints),
    maxFilament: numberText(rule.max_filament_g),
    maxCalls: numberText(rule.max_calls),
    args: (rule.args ?? []).map((a) => ({ path: a.path, values: a.in.map((v) => JSON.stringify(v)).join(', ') })),
  }
}

/** A value as typed: JSON when it parses (`3`, `true`, `"x"`), else the text itself. */
function valueOf(text: string): string | number | boolean | null {
  try {
    const parsed: unknown = JSON.parse(text)
    if (parsed === null || ['string', 'number', 'boolean'].includes(typeof parsed)) return parsed as string | number | boolean | null
  } catch {
    // A bare word is a string.
  }
  return text
}

const listOf = (text: string) =>
  text
    .split(',')
    .map((part) => part.trim())
    .filter(Boolean)

function ruleOf(row: RowDraft): FlowGrantRule {
  const rule: FlowGrantRule = { fn: row.fn }
  if (row.fn === 'tool' && row.tool) rule.tool = row.tool
  const applies = (bound: Bound) => BOUNDS[bound].includes(row.fn)
  if (applies('printers') && row.printers) rule.printers = row.printers
  if (applies('models') && row.models) rule.models = row.models
  if (applies('library_files') && row.libraryFiles !== null) rule.library_files = listOf(row.libraryFiles).map(Number)
  if (applies('max_prints') && row.maxPrints.trim()) rule.max_prints = Number(row.maxPrints)
  if (applies('max_filament_g') && row.maxFilament.trim()) rule.max_filament_g = Number(row.maxFilament)
  if (applies('max_calls') && row.maxCalls.trim()) rule.max_calls = Number(row.maxCalls)
  if (applies('args') && row.args.length > 0) {
    rule.args = row.args.map((a) => ({ path: a.path.trim(), in: listOf(a.values).map(valueOf) }))
  }
  return rule
}

/** Problems the typed numbers have before they are a rule. */
function typingProblems(rows: RowDraft[]): string[] {
  const problems: string[] = []
  rows.forEach((row) => {
    if (!row.on) return
    const name = row.tool ?? row.fn
    const whole = (text: string) => /^\d+$/.test(text.trim())
    if (row.maxPrints.trim() && !whole(row.maxPrints)) problems.push(`${name}: max prints is a whole number.`)
    if (row.maxCalls.trim() && !whole(row.maxCalls)) problems.push(`${name}: max calls is a whole number.`)
    if (row.maxFilament.trim() && !/^\d+(\.\d+)?$/.test(row.maxFilament.trim())) {
      problems.push(`${name}: max filament is a number of grams.`)
    }
    if (row.libraryFiles !== null && listOf(row.libraryFiles).some((id) => !whole(id))) {
      problems.push(`${name}: library file ids are whole numbers.`)
    }
  })
  return problems
}

/** `iso` as a `datetime-local` value, in the browser's time zone. */
function localInput(iso: string | number): string {
  const date = new Date(iso)
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`
}

/** The rows: the initial grant's rules (on), then one per host function and outward tool it lacks (off). */
function initialRows(initial: FlowGrantSpec | undefined, outwardTools: string[]): RowDraft[] {
  const rows = (initial?.rules ?? []).map((rule, i) => rowOf(`rule-${i}`, rule, true))
  const has = (fn: HostFn, tool: string | null) => rows.some((r) => r.fn === fn && r.tool === tool)
  const offered: FlowGrantRule[] = [
    { fn: 'queue_print' },
    { fn: 'arrange' },
    ...outwardTools.map((tool) => ({ fn: 'tool' as const, tool })),
  ]
  for (const rule of offered) {
    if (!has(rule.fn, rule.tool ?? null)) rows.push(rowOf(`offer-${rule.tool ?? rule.fn}`, rule, false))
  }
  return rows
}

// -- the form -------------------------------------------------------------------------

interface Props {
  /** A grant to start from: a requested one being approved. */
  initial?: FlowGrantSpec
  submitLabel: string
  busy?: boolean
  onSubmit: (spec: FlowGrantSpec) => void
  onCancel?: () => void
  /** #2484 — a trigger's standing grant: up to 30 days, 7 by default, and no starting session to ask. */
  standing?: boolean
}

/**
 * #2481 (spec 2026-10-11 §3.1) — a run grant: what the run's outward calls may do
 * without asking. One rule per host function (`queue_print`, `arrange`) and outward
 * tool, each with the bounds that apply to it; a list bound is "any" until narrowed,
 * and a narrowed one with nothing picked admits nothing. An unbounded rule is said in
 * red. The expiry defaults to 24 h and may be at most 7 days away.
 */
export function GrantEditor(props: Props) {
  const host = useAsync(() => api.getFlowHostFunctions(), [])
  if (host.error) {
    return (
      <p role="alert" className="text-[13px] text-destructive">
        The tools a grant may name could not be read: {host.error.message}
      </p>
    )
  }
  if (!host.data) {
    return (
      <div role="status" aria-label="Loading" className="flex flex-col gap-2">
        <Skeleton className="h-16 w-full rounded-xl" />
        <Skeleton className="h-16 w-full rounded-xl" />
      </div>
    )
  }
  return <Form {...props} outwardTools={host.data.tools.filter((t) => t.tier === 'outward').map((t) => t.name)} />
}

function Form({
  initial,
  submitLabel,
  busy,
  onSubmit,
  onCancel,
  standing = false,
  outwardTools,
}: Props & { outwardTools: string[] }) {
  const maxMs = standing ? MAX_STANDING_GRANT_MS : MAX_GRANT_MS
  const printers = useAsync(() => api.getBambuddyTargets(), [])
  const models = useAsync(() => api.listModels(), [])
  const [rows, setRows] = useState(() => initialRows(initial, outwardTools))
  const [expires, setExpires] = useState(() =>
    localInput(initial?.expires_at ?? Date.now() + (standing ? 7 * 24 : 24) * HOUR),
  )
  const [sessions, setSessions] = useState(initial?.sessions ?? false)
  const [askStarter, setAskStarter] = useState(standing ? false : (initial?.ask_starter_session ?? true))
  // The picker's range, from when the form opened; the check below is against now.
  const [openedAt] = useState(() => Date.now())

  const expiresAt = new Date(expires)
  const spec: FlowGrantSpec = {
    expires_at: Number.isNaN(expiresAt.getTime()) ? '' : expiresAt.toISOString(),
    sessions,
    ask_starter_session: askStarter,
    rules: rows.filter((r) => r.on).map(ruleOf),
  }
  const outward = useMemo(() => new Set(outwardTools), [outwardTools])
  const typing = typingProblems(rows)
  const problems = [...typing, ...(typing.length > 0 ? [] : grantProblems(spec, new Date(), outward, maxMs))]

  const expiresId = useId()
  const update = (key: string, change: Partial<RowDraft>) =>
    setRows((current) => current.map((r) => (r.key === key ? { ...r, ...change } : r)))

  return (
    <form
      // A grant is a person's alone (spec §3.2): the browser bridge acts as the browser
      // user, so every field and the submit refuse it (`dom.ts` `isUserOnly`).
      {...USER_ONLY}
      className="flex flex-col gap-3 text-[13px]"
      onSubmit={(event) => {
        event.preventDefault()
        if (problems.length === 0) onSubmit(spec)
      }}
    >
      <p className="text-[12.5px] text-muted-foreground">
        Calls inside an allowed rule run without asking you; any other outward call still waits for a decision.
      </p>
      {rows.map((row) => (
        <RuleRow
          key={row.key}
          row={row}
          name={row.tool ?? row.fn}
          update={(change) => update(row.key, change)}
          printers={printers}
          models={models}
        />
      ))}
      <div className="flex flex-col gap-1.5">
        <Label htmlFor={expiresId}>Expires</Label>
        <Input
          id={expiresId}
          type="datetime-local"
          aria-label="Expires"
          value={expires}
          min={localInput(openedAt)}
          max={localInput(openedAt + maxMs)}
          onChange={(e) => setExpires(e.target.value)}
          className="w-fit"
        />
      </div>
      <Check checked={sessions} onChange={setSessions}>
        Extends to the sessions it starts
      </Check>
      {!standing && (
        <Check checked={askStarter} onChange={setAskStarter}>
          Ask the starting session first
        </Check>
      )}
      {problems.length > 0 && (
        <ul role="alert" className="list-disc pl-5 text-destructive">
          {problems.map((p) => (
            <li key={p}>{p}</li>
          ))}
        </ul>
      )}
      <div className="flex gap-2">
        <Button type="submit" disabled={busy || problems.length > 0} aria-busy={busy}>
          {busy && <Spinner />}
          {submitLabel}
        </Button>
        {onCancel && (
          <Button type="button" variant="ghost" onClick={onCancel} disabled={busy}>
            Cancel
          </Button>
        )}
      </div>
    </form>
  )
}

interface RowProps {
  row: RowDraft
  name: string
  update: (change: Partial<RowDraft>) => void
  printers: AsyncState<BambuddyTargets>
  models: AsyncState<ModelSummary[]>
}

function RuleRow({ row, name, update, printers, models }: RowProps) {
  const applies = (bound: Bound) => BOUNDS[bound].includes(row.fn)
  const rule = ruleOf(row)
  const printerChoices = (printers.data?.printers ?? []).map((p) => ({ value: p.id, label: p.name }))
  const modelChoices = (models.data ?? []).map((m) => ({ value: m.slug, label: m.name }))
  return (
    <fieldset aria-label={`Rule ${name}`} className="rounded-xl p-3 ring-1 ring-foreground/10">
      <legend className="px-1 font-mono text-[12.5px]">{row.fn === 'tool' ? `tool: ${name}` : name}</legend>
      <Check checked={row.on} onChange={(on) => update({ on })}>
        Allow without asking
      </Check>
      {row.on && (
        <div className="mt-2 flex flex-col gap-2">
          {applies('printers') && (
            <Picker
              label="Printers"
              anyLabel="Any printer"
              value={row.printers}
              choices={printerChoices}
              unavailable={printers.error ? `Printers could not be listed: ${printers.error.message}` : undefined}
              describe={(id) => `Printer #${id}`}
              onChange={(printers) => update({ printers })}
            />
          )}
          {applies('models') && (
            <Picker
              label="Models"
              anyLabel="Any model"
              value={row.models}
              choices={modelChoices}
              unavailable={models.error ? `Models could not be listed: ${models.error.message}` : undefined}
              describe={(slug) => slug}
              onChange={(models) => update({ models })}
            />
          )}
          {applies('library_files') && (
            <div className="flex flex-col gap-1.5">
              <Check checked={row.libraryFiles === null} onChange={(any) => update({ libraryFiles: any ? null : '' })}>
                Any library file
              </Check>
              {row.libraryFiles !== null && (
                <Input
                  aria-label="Library file ids"
                  placeholder="Bambuddy library file ids, comma-separated; empty for none"
                  value={row.libraryFiles}
                  onChange={(e) => update({ libraryFiles: e.target.value })}
                />
              )}
            </div>
          )}
          <div className="flex flex-wrap gap-2">
            {applies('max_prints') && (
              <NumberField label="Max prints" value={row.maxPrints} onChange={(maxPrints) => update({ maxPrints })} />
            )}
            {applies('max_filament_g') && (
              <NumberField label="Max filament (g)" value={row.maxFilament} onChange={(maxFilament) => update({ maxFilament })} />
            )}
            <NumberField label="Max calls" value={row.maxCalls} onChange={(maxCalls) => update({ maxCalls })} />
          </div>
          {applies('args') && (
            <div className="flex flex-col gap-1">
              {row.args.map((arg, i) => (
                <div key={i} className="flex flex-wrap items-center gap-2">
                  <Input
                    aria-label="Argument path"
                    placeholder="/printer_id"
                    value={arg.path}
                    onChange={(e) => update({ args: row.args.map((a, j) => (j === i ? { ...a, path: e.target.value } : a)) })}
                    className="w-40 font-mono"
                  />
                  <Input
                    aria-label="Allowed values"
                    placeholder="3, 4"
                    value={arg.values}
                    onChange={(e) => update({ args: row.args.map((a, j) => (j === i ? { ...a, values: e.target.value } : a)) })}
                    className="w-auto min-w-0 flex-1"
                  />
                  <Button type="button" size="sm" variant="ghost" onClick={() => update({ args: row.args.filter((_, j) => j !== i) })}>
                    Remove
                  </Button>
                </div>
              ))}
              <div>
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  onClick={() => update({ args: [...row.args, { path: '', values: '' }] })}
                >
                  Add argument bound
                </Button>
              </div>
            </div>
          )}
          {isUnbounded(rule) && <p className="text-[12.5px] text-destructive">{unboundedText(rule)}</p>}
        </div>
      )}
    </fieldset>
  )
}

function NumberField({ label, value, onChange }: { label: string; value: string; onChange: (next: string) => void }) {
  return (
    <label className="flex flex-col gap-1 text-[12.5px]">
      <span className="text-muted-foreground">{label}</span>
      <Input
        aria-label={label}
        inputMode="decimal"
        placeholder="no limit"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="w-28"
      />
    </label>
  )
}

interface PickerProps<T extends string | number> {
  label: string
  anyLabel: string
  value: T[] | null
  choices: { value: T; label: string }[]
  unavailable?: string
  describe: (value: T) => string
  onChange: (next: T[] | null) => void
}

/** A list bound: "any" (no bound), or the values picked; picking none admits none. */
function Picker<T extends string | number>({ label, anyLabel, value, choices, unavailable, describe, onChange }: PickerProps<T>) {
  // A value the request named that the read no longer lists stays pickable.
  const all = [...choices, ...(value ?? []).filter((v) => !choices.some((c) => c.value === v)).map((v) => ({ value: v, label: describe(v) }))]
  return (
    <div role="group" aria-label={label} className="flex flex-col gap-1.5">
      <Check checked={value === null} onChange={(any) => onChange(any ? null : [])}>
        {anyLabel}
      </Check>
      {value !== null && (
        <div className="ml-6 flex max-h-32 flex-col gap-1.5 overflow-y-auto py-0.5">
          {unavailable && <p className="text-[12.5px] text-muted-foreground">{unavailable}</p>}
          {all.map((choice) => (
            <Check
              key={String(choice.value)}
              checked={value.includes(choice.value)}
              onChange={(on) => onChange(on ? [...value, choice.value] : value.filter((v) => v !== choice.value))}
            >
              {choice.label}
            </Check>
          ))}
          {value.length === 0 && (
            <p className="text-[12.5px] text-muted-foreground">None picked: this rule admits none.</p>
          )}
        </div>
      )}
    </div>
  )
}
