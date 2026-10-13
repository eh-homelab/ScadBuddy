import { useEffect, useRef, useState } from 'react'
import { isActivityFiltered, splitList, type ActivityQuery } from '../../lib/activityQuery'
import type { ActivityFacets } from '../../api/types'
import { statusLabel } from '../prints/status'
import { chipClass, principalLabel, SINCE_OPTIONS, toggleValue } from './labels'

interface Props {
  query: ActivityQuery
  facets: ActivityFacets | undefined
  /** `typing` is a change made keystroke by keystroke: it replaces the history entry. */
  onChange: (next: ActivityQuery, options?: { typing?: boolean }) => void
}

function Group({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div role="group" aria-label={label} className="flex flex-wrap items-center gap-1.5">
      <span className="text-[12px] text-muted">{label}</span>
      {children}
    </div>
  )
}

const CHIP_WRAP = 'max-w-full break-all text-left'

/** A text input that reports after a pause, and follows the URL when it changes elsewhere. */
function DebouncedInput({
  value,
  onCommit,
  ...rest
}: { value: string; onCommit: (value: string) => void } & Omit<
  React.InputHTMLAttributes<HTMLInputElement>,
  'value' | 'onChange'
>) {
  const [text, setText] = useState(value)
  const sent = useRef(value)
  const commit = useRef(onCommit)
  useEffect(() => {
    commit.current = onCommit
  })
  useEffect(() => {
    if (value !== sent.current) {
      sent.current = value
      setText(value)
    }
  }, [value])
  useEffect(() => {
    if (text === sent.current) return
    const timer = setTimeout(() => {
      sent.current = text
      commit.current(text)
    }, 250)
    return () => clearTimeout(timer)
  }, [text])
  return <input {...rest} value={text} onChange={(e) => setText(e.target.value)} />
}

/** The values of one facet as chips, plus any selected value the facets do not list. */
function valueChips(
  listed: Array<{ value: string; count: number }>,
  selected: string,
): Array<{ value: string; count: number | null }> {
  const extra = selected && !listed.some((l) => l.value === selected) ? [{ value: selected, count: null }] : []
  return [...listed, ...extra]
}

/** The filters above the table; every choice comes from the facets endpoint. */
export function ActivityFilters({ query, facets, onChange }: Props) {
  const latest = useRef(query)
  useEffect(() => {
    latest.current = query
  })

  const selectedKinds = splitList(query.kind)
  const selectedStatuses = splitList(query.status)
  const single = selectedKinds.length === 1 ? facets?.kinds.find((k) => k.key === selectedKinds[0]) : undefined
  const filterable = single?.facets.filter((f) => f.filterable) ?? []
  const filterableNames = new Set(filterable.map((f) => f.name))
  // An applied facet whose input is not shown still has to be visible and removable.
  const strayFacets = Object.entries(query.facets).filter(([name]) => !filterableNames.has(name))
  const kinds = [
    ...(facets?.kinds ?? []).map((k) => ({ key: k.key, label: k.label })),
    ...selectedKinds.filter((k) => !facets?.kinds.some((f) => f.key === k)).map((k) => ({ key: k, label: k })),
  ]
  const statuses = [
    ...(facets?.statuses ?? []),
    ...selectedStatuses.filter((s) => !(facets?.statuses as string[] | undefined)?.includes(s)),
  ]

  function setKind(next: string) {
    // A kind's own facets mean nothing once the kind is not alone.
    const alone = splitList(next).length === 1
    onChange({ ...query, kind: next, facets: alone ? query.facets : {} })
  }

  function setFacet(name: string, value: string, typing: boolean) {
    const rest = { ...latest.current.facets }
    if (value) rest[name] = value
    else delete rest[name]
    onChange({ ...latest.current, facets: rest }, { typing })
  }

  const principals = valueChips(facets?.principals ?? [], query.principal)
  const tools = valueChips(facets?.tools ?? [], query.tool)
  const subjects = valueChips(facets?.subjects ?? [], query.subject)
  const sinceKnown = SINCE_OPTIONS.some((o) => o.value === query.since)

  return (
    <div className="flex flex-col gap-2" aria-label="Activity filters" role="search">
      <div className="flex flex-wrap items-center gap-2">
        <DebouncedInput
          type="search"
          className="sb-field max-w-xs"
          placeholder="Search activity"
          aria-label="Search activity"
          value={query.q}
          onCommit={(q) => onChange({ ...latest.current, q }, { typing: true })}
        />
        <select
          className="sb-field w-auto"
          aria-label="Since"
          value={sinceKnown ? query.since : ''}
          onChange={(e) => onChange({ ...query, since: e.target.value })}
        >
          <option value="">Any time</option>
          {SINCE_OPTIONS.map((o) => (
            <option key={o.value} value={o.value}>
              {o.label}
            </option>
          ))}
        </select>
        {isActivityFiltered(query) && (
          <button type="button" className="text-[12px] text-muted hover:text-ink" onClick={() => onChange({ ...query, kind: '', status: '', principal: '', tool: '', subject: '', since: '', q: '', facets: {} })}>
            Clear filters
          </button>
        )}
      </div>
      {kinds.length > 0 && (
        <Group label="Kind">
          {kinds.map((k) => (
            <button
              key={k.key}
              type="button"
              className={`${chipClass(selectedKinds.includes(k.key))} ${CHIP_WRAP}`}
              aria-pressed={selectedKinds.includes(k.key)}
              onClick={() => setKind(toggleValue(query.kind, k.key))}
            >
              {k.label}
            </button>
          ))}
        </Group>
      )}
      {statuses.length > 0 && (
        <Group label="Status">
          {statuses.map((s) => {
            const on = selectedStatuses.includes(s)
            return (
              <button
                key={s}
                type="button"
                className={chipClass(on)}
                aria-pressed={on}
                onClick={() => onChange({ ...query, status: toggleValue(query.status, s) })}
              >
                {statusLabel(s)}
              </button>
            )
          })}
        </Group>
      )}
      {principals.length > 0 && (
        <Group label="Started by">
          {principals.map((p) => (
            <button
              key={p.value}
              type="button"
              className={`${chipClass(query.principal === p.value)} ${CHIP_WRAP}`}
              aria-pressed={query.principal === p.value}
              onClick={() => onChange({ ...query, principal: query.principal === p.value ? '' : p.value })}
            >
              {principalLabel(p.value, facets)}
              {p.count !== null && <> <span className="sb-num text-muted">{p.count}</span></>}
            </button>
          ))}
        </Group>
      )}
      {tools.length > 0 && (
        <Group label="Tool">
          {tools.map((t) => (
            <button
              key={t.value}
              type="button"
              className={`${chipClass(query.tool === t.value)} ${CHIP_WRAP}`}
              aria-pressed={query.tool === t.value}
              onClick={() => onChange({ ...query, tool: query.tool === t.value ? '' : t.value })}
            >
              {t.value}
              {t.count !== null && <> <span className="sb-num text-muted">{t.count}</span></>}
            </button>
          ))}
        </Group>
      )}
      {subjects.length > 0 && (
        <Group label="Subject">
          {subjects.map((s) => (
            <button
              key={s.value}
              type="button"
              className={`${chipClass(query.subject === s.value)} ${CHIP_WRAP}`}
              aria-pressed={query.subject === s.value}
              onClick={() => onChange({ ...query, subject: query.subject === s.value ? '' : s.value })}
            >
              {s.value}
              {s.count !== null && <> <span className="sb-num text-muted">{s.count}</span></>}
            </button>
          ))}
        </Group>
      )}
      {filterable.length > 0 && (
        <div className="flex flex-wrap items-end gap-2">
          {filterable.map((f) => (
            <label key={f.name} className="flex flex-col gap-0.5 text-[12px] text-muted">
              {f.label}
              <DebouncedInput
                className="sb-field w-40 max-w-full"
                value={query.facets[f.name] ?? ''}
                onCommit={(v) => setFacet(f.name, v, true)}
              />
            </label>
          ))}
        </div>
      )}
      {strayFacets.length > 0 && (
        <Group label="Applied facets">
          {strayFacets.map(([name, value]) => (
            <button
              key={name}
              type="button"
              className={`${chipClass(true)} ${CHIP_WRAP}`}
              aria-pressed
              aria-label={`Remove ${name} ${value}`}
              onClick={() => setFacet(name, '', false)}
            >
              {name}: {value}
            </button>
          ))}
        </Group>
      )}
    </div>
  )
}
