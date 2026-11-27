import { ChevronLeftIcon } from 'lucide-react'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link, useParams } from 'react-router'
import { ApiError } from '../../api/client'
import { diffPreset, getPreset, problemCode, routeMissing, type PresetDetail, type PresetDiff, type PresetKeyChange, type PresetMirror, type PresetSource } from '../../api/filamentPresets'
import { FilamentSearchBox } from '../../components/filament/FilamentSearchBox'
import { filamentLinks } from '../../lib/filamentLinks'
import { Button } from '@/components/ui/button'
import { cn } from '@/lib/utils'
import { count } from './plural'

const SOURCE_LABEL: Record<PresetSource, string> = { '3dfp': '3DFP', bambuddy: 'Bambuddy', manual: 'Manual' }
const MIRROR_LABEL: Record<PresetMirror, string> = { sent: 'Sent to Bambuddy', pending: 'Not sent to Bambuddy yet', failed: 'Send to Bambuddy failed', bambuddy_owned: 'Owned by Bambuddy' }

/** Why the current version's document is not shown (`document_error` of the detail). */
const DOCUMENT_ERROR: Record<string, string> = {
  document_missing: 'The document of this version is not in the store.',
  document_too_large: 'The document of this version is too large to show.',
  document_unreadable: 'The document of this version cannot be read.',
  store_not_configured: 'The blob store is not set up, so the document cannot be read.',
  store_unavailable: 'The blob store did not answer. Reload to try again.',
}

/** Why a diff failed (its problem `code`). */
const DIFF_ERROR: Record<string, string> = {
  document_missing: 'The document of one of these versions is not in the store.',
  document_too_large: 'The document of one of these versions is too large to compare.',
  document_unreadable: 'The document of one of these versions cannot be read.',
  version_not_found: 'One of these versions does not exist.',
  store_not_configured: 'The blob store is not set up, so versions cannot be compared.',
  store_unavailable: 'The blob store did not answer. Try again.',
}

const diffMessage = (cause: unknown) => {
  const code = problemCode(cause)
  if (code && DIFF_ERROR[code]) return DIFF_ERROR[code]
  return cause instanceof ApiError ? cause.detail : 'The versions could not be compared. Try again.'
}

const date = (iso: string) => new Date(iso).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' })

/** A document value: a string as it is, a list as one line for each item, anything else as JSON. */
function Value({ value }: { value: unknown }) {
  if (value === null || value === undefined) return <span className="text-muted-foreground">Not set</span>
  if (Array.isArray(value)) {
    if (value.length === 0) return <span className="text-muted-foreground">Empty list</span>
    return (
      <ul className="flex flex-col">
        {value.map((item, at) => (
          <li key={at} className="break-all">
            {typeof item === 'string' ? item : JSON.stringify(item)}
          </li>
        ))}
      </ul>
    )
  }
  return <span className="break-all">{typeof value === 'string' ? value : JSON.stringify(value)}</span>
}

/** The keys that changed between two versions: before and after. */
function ChangesTable({ label, changes }: { label: string; changes: PresetKeyChange[] }) {
  if (changes.length === 0) return <p className="text-[13px] text-muted-foreground">No key changed.</p>
  return (
    <div className="overflow-x-auto rounded-md border">
      <table aria-label={label} className="w-full text-left text-[13px]">
        <thead className="bg-muted/50 text-muted-foreground">
          <tr>
            <th scope="col" className="px-2 py-1.5 font-medium">Key</th>
            <th scope="col" className="px-2 py-1.5 font-medium">Before</th>
            <th scope="col" className="px-2 py-1.5 font-medium">After</th>
          </tr>
        </thead>
        <tbody>
          {changes.map((change) => (
            <tr key={change.key} className="border-t align-top">
              <th scope="row" className="px-2 py-1.5 font-mono font-normal">{change.key}</th>
              <td className="px-2 py-1.5"><Value value={change.before} /></td>
              <td className="px-2 py-1.5"><Value value={change.after} /></td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

type DiffResult = { diff: PresetDiff } | { error: string }

/** Every diff the page asks for, by `from:to`: one request for a pair, whoever asks for it. */
function useDiffCache(id: number) {
  const [results, setResults] = useState<Map<string, DiffResult>>(new Map())
  const asked = useRef(new Set<string>())
  const ask = useCallback(
    (from: number, to: number) => {
      const key = `${from}:${to}`
      if (asked.current.has(key)) return
      asked.current.add(key)
      diffPreset(id, from, to).then(
        (diff) => setResults((was) => new Map(was).set(key, { diff })),
        (cause: unknown) => {
          asked.current.delete(key)
          setResults((was) => new Map(was).set(key, { error: diffMessage(cause) }))
        },
      )
    },
    [id],
  )
  return { results, ask }
}

type Diffs = ReturnType<typeof useDiffCache>

/** How many versions show their changed keys at first, and how many more each "Show more" adds. */
const PAGE = 10

/** The keys each version changed from the one before it. A version that cannot be diffed shows why, and the rest still show. */
function Versions({ preset, diffs }: { preset: PresetDetail; diffs: Diffs }) {
  const [limit, setLimit] = useState(PAGE)
  const versions = useMemo(() => [...preset.versions].sort((a, b) => b.version - a.version), [preset.versions])
  const shown = useMemo(() => versions.slice(0, limit), [versions, limit])
  const { ask } = diffs
  useEffect(() => {
    shown.forEach((one, at) => {
      const before = versions[at + 1]
      if (before) ask(before.version, one.version)
    })
  }, [shown, versions, ask])
  return (
    <section aria-labelledby="preset-versions" className="flex flex-col gap-2">
      <h2 id="preset-versions" className="text-sm font-semibold">Versions</h2>
      <ol aria-label="Versions" className="flex flex-col gap-2">
        {shown.map((one, at) => {
          const before = versions[at + 1]
          const result = before ? diffs.results.get(`${before.version}:${one.version}`) : undefined
          return (
            <li key={one.version} className="rounded-md border p-3">
              <div className="flex flex-wrap items-baseline gap-x-3 gap-y-0.5 text-[13px]">
                <span className="font-semibold tabular-nums">{`v${one.version}`}</span>
                {one.version === preset.version && <span className="text-muted-foreground">Current</span>}
                <span className="text-muted-foreground">{date(one.made_at)}</span>
                {one.made_by && <span className="text-muted-foreground">{`by ${one.made_by}`}</span>}
                <span className="font-mono text-[12px] text-muted-foreground" title={one.sha256}>{one.sha256.slice(0, 8)}</span>
              </div>
              <div className="mt-2">
                {!before ? (
                  <p className="text-[13px] text-muted-foreground">First version.</p>
                ) : !result ? (
                  <p className="text-[13px] text-muted-foreground">Loading changes…</p>
                ) : 'error' in result ? (
                  <p className="text-[13px] text-warn">{result.error}</p>
                ) : result.diff.changes.length === 0 ? (
                  <p className="text-[13px] text-muted-foreground">No key changed.</p>
                ) : (
                  <ul aria-label={`Keys changed in v${one.version}`} className="flex flex-wrap gap-1.5">
                    {result.diff.changes.map((change) => (
                      <li key={change.key} className="rounded border bg-muted/40 px-1.5 py-0.5 font-mono text-[12px]">
                        {change.key}
                      </li>
                    ))}
                  </ul>
                )}
              </div>
            </li>
          )
        })}
      </ol>
      {versions.length > limit && (
        <div>
          <Button type="button" variant="outline" size="sm" onClick={() => setLimit(limit + PAGE)}>
            Show more
          </Button>
        </div>
      )}
    </section>
  )
}

function VersionSelect({ label, value, versions, onChange }: { label: string; value: number; versions: number[]; onChange: (version: number) => void }) {
  return (
    <label className="flex items-center gap-2 text-[13px]">
      {label}
      <select
        value={value}
        onChange={(event) => onChange(Number(event.target.value))}
        className="h-7 rounded-md border border-input bg-background px-2 text-[13px] tabular-nums outline-none focus-visible:ring-2 focus-visible:ring-ring/50"
      >
        {versions.map((n) => (
          <option key={n} value={n}>{`v${n}`}</option>
        ))}
      </select>
    </label>
  )
}

function Compare({ preset, diffs }: { preset: PresetDetail; diffs: Diffs }) {
  const numbers = useMemo(() => preset.versions.map((one) => one.version).sort((a, b) => a - b), [preset.versions])
  const [to, setTo] = useState(preset.version)
  // The newest version before the current one: version numbers may have gaps.
  const [from, setFrom] = useState(numbers.filter((n) => n < preset.version).pop() ?? preset.version)
  const key = `${from}:${to}`
  const same = from === to
  const { ask } = diffs

  useEffect(() => {
    if (from !== to) ask(from, to)
  }, [ask, from, to])

  const result = diffs.results.get(key)

  return (
    <section aria-labelledby="preset-compare" className="flex flex-col gap-2">
      <h2 id="preset-compare" className="text-sm font-semibold">Compare versions</h2>
      {numbers.length < 2 ? (
        <p className="text-[13px] text-muted-foreground">This preset has one version. There is nothing to compare.</p>
      ) : (
        <div className="flex flex-col gap-2">
          <div className="flex flex-wrap items-center gap-3">
            <VersionSelect label="From" value={from} versions={numbers} onChange={setFrom} />
            <VersionSelect label="To" value={to} versions={numbers} onChange={setTo} />
          </div>
          {same ? (
            <p className="text-[13px] text-muted-foreground">Pick two different versions.</p>
          ) : !result ? (
            <p className="text-[13px] text-muted-foreground">Loading changes…</p>
          ) : 'error' in result ? (
            <p role="alert" className="text-[13px] text-warn">{result.error}</p>
          ) : (
            <ChangesTable label={`Changed keys, v${from} to v${to}`} changes={result.diff.changes} />
          )}
        </div>
      )}
    </section>
  )
}

function Document({ preset }: { preset: PresetDetail }) {
  const [filter, setFilter] = useState('')
  const entries = useMemo(() => Object.entries(preset.document ?? {}).sort(([a], [b]) => a.localeCompare(b)), [preset.document])
  const needle = filter.trim().toLowerCase()
  const shown = needle ? entries.filter(([key]) => key.toLowerCase().includes(needle)) : entries
  return (
    <section aria-label="Document" className="flex flex-col gap-2">
      <h2 className="text-sm font-semibold">{`Document, v${preset.version}`}</h2>
      {preset.document === null ? (
        <p className="text-[13px] text-warn">{DOCUMENT_ERROR[preset.document_error ?? ''] ?? 'The document cannot be shown.'}</p>
      ) : (
        <>
          <div className="flex flex-wrap items-center gap-3">
            <FilamentSearchBox label="Filter keys" placeholder="Filter keys  /" value={filter} onChange={setFilter} className="w-full sm:w-72" />
            <span className="text-[13px] text-muted-foreground tabular-nums">{needle ? `${shown.length} of ${entries.length} keys` : `${entries.length} keys`}</span>
          </div>
          <div className="overflow-x-auto rounded-md border">
            <table aria-label="Document keys" className="w-full text-left text-[13px]">
              <tbody>
                {shown.map(([key, value]) => (
                  <tr key={key} className="border-t first:border-t-0 align-top">
                    <th scope="row" className="w-1/3 px-2 py-1.5 font-mono font-normal break-all">{key}</th>
                    <td className="px-2 py-1.5"><Value value={value} /></td>
                  </tr>
                ))}
                {shown.length === 0 && (
                  <tr>
                    <td className="px-2 py-3 text-muted-foreground">{entries.length === 0 ? 'The document has no keys.' : `No key matches “${filter}”.`}</td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </>
      )}
    </section>
  )
}

function Shell({ children }: { children: React.ReactNode }) {
  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto max-w-6xl px-4 py-6">
        <Link to={filamentLinks.presets} className="mb-3 inline-flex items-center gap-1 text-[13px] text-muted-foreground hover:text-foreground">
          <ChevronLeftIcon className="size-3.5" /> Presets
        </Link>
        {children}
      </div>
    </div>
  )
}

type Load = { state: 'loading' } | { state: 'off' } | { state: 'missing' } | { state: 'error'; message: string } | { state: 'ready'; preset: PresetDetail }

function Loaded({ preset }: { preset: PresetDetail }) {
  const diffs = useDiffCache(preset.id)
  const where = [preset.printer_model, preset.nozzle_diameter && `${preset.nozzle_diameter} mm`, preset.filament_type].filter(Boolean).join(', ')
  return (
    <Shell>
      <header className="mb-4 flex flex-col gap-1">
        <h1 className="text-lg font-semibold tracking-tight">{preset.name}</h1>
        <p className="flex flex-wrap items-center gap-x-3 gap-y-0.5 text-[13px] text-muted-foreground">
          <span>{SOURCE_LABEL[preset.source]}</span>
          <span className="tabular-nums">{`v${preset.version}`}</span>
          <span className="font-mono">{preset.ref}</span>
          {where && <span>{where}</span>}
          {preset.type_id && (
            <Link to={filamentLinks.type(preset.type_id)} className="underline-offset-4 hover:underline">
              Type
            </Link>
          )}
          <span className={cn(preset.mirror === 'failed' && 'font-medium text-warn')}>{MIRROR_LABEL[preset.mirror]}</span>
          <span className="tabular-nums">{`Used by ${count(preset.used_by_types, 'type')}, ${count(preset.used_by_spools, 'spool')}`}</span>
        </p>
        {preset.mirror_error && <p className="text-[13px] text-muted-foreground">{preset.mirror_error}</p>}
      </header>
      <div className="flex flex-col gap-6">
        <Versions preset={preset} diffs={diffs} />
        <Compare preset={preset} diffs={diffs} />
        <Document preset={preset} />
      </div>
    </Shell>
  )
}

/** #7204 — one filament preset, read only: its versions with the keys each changed, a compare of any two, and the current document. */
export function PresetPage() {
  const { id } = useParams()
  const [load, setLoad] = useState<{ id: string | undefined; value: Load }>({ id, value: { state: 'loading' } })

  useEffect(() => {
    const number = Number(id)
    let live = true
    const done = (value: Load) => live && setLoad({ id, value })
    if (!Number.isInteger(number) || number < 1) {
      done({ state: 'missing' })
      return
    }
    getPreset(number).then(
      (preset) => done({ state: 'ready', preset }),
      (cause: unknown) => {
        if (routeMissing(cause)) done({ state: 'off' })
        else if (problemCode(cause) === 'preset_not_found') done({ state: 'missing' })
        else done({ state: 'error', message: cause instanceof ApiError ? cause.detail : 'The preset could not be loaded. Try again.' })
      },
    )
    return () => {
      live = false
    }
  }, [id])

  const value: Load = load.id === id ? load.value : { state: 'loading' }
  if (value.state === 'loading')
    return (
      <Shell>
        <p className="text-[13px] text-muted-foreground">Loading preset…</p>
      </Shell>
    )
  if (value.state === 'off')
    return (
      <Shell>
        <h1 className="text-lg font-semibold tracking-tight">Presets</h1>
        <p className="mt-1 text-[13px] text-muted-foreground">Presets are not on yet on this server.</p>
      </Shell>
    )
  if (value.state === 'missing')
    return (
      <Shell>
        <h1 className="text-lg font-semibold tracking-tight">No such preset</h1>
        <p className="mt-1 text-[13px] text-muted-foreground">There is no such preset.</p>
      </Shell>
    )
  if (value.state === 'error')
    return (
      <Shell>
        <p role="alert" className="text-[13px] text-warn">{value.message}</p>
      </Shell>
    )

  return <Loaded key={value.preset.id} preset={value.preset} />
}
