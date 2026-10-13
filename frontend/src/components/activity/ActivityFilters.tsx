import { useEffect, useRef, useState } from 'react'
import type { ActivityQuery } from '../../lib/activityQuery'
import type { ActivityFacets } from '../../api/types'
import { statusLabel } from '../prints/status'
import { chipClass, principalLabel, SINCE_OPTIONS, toggleValue } from './labels'

interface Props {
  query: ActivityQuery
  facets: ActivityFacets | undefined
  onChange: (next: ActivityQuery) => void
}

function Group({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div role="group" aria-label={label} className="flex flex-wrap items-center gap-1.5">
      <span className="text-[12px] text-muted">{label}</span>
      {children}
    </div>
  )
}

/** The filters above the table; every choice comes from the facets endpoint. */
export function ActivityFilters({ query, facets, onChange }: Props) {
  const [text, setText] = useState(query.q)
  const sent = useRef(query.q)
  const latest = useRef(query)
  useEffect(() => {
    latest.current = query
  })
  // The URL changed from outside (Back/Forward): follow it.
  useEffect(() => {
    if (query.q !== sent.current) {
      sent.current = query.q
      setText(query.q)
    }
  }, [query.q])
  useEffect(() => {
    if (text === sent.current) return
    const timer = setTimeout(() => {
      sent.current = text
      onChange({ ...latest.current, q: text })
    }, 250)
    return () => clearTimeout(timer)
  }, [text, onChange])

  const selectedKinds = query.kind ? query.kind.split(',') : []
  const single = selectedKinds.length === 1 ? facets?.kinds.find((k) => k.key === selectedKinds[0]) : undefined
  const filterable = single?.facets.filter((f) => f.filterable) ?? []

  function setKind(next: string) {
    // A kind's own facets mean nothing once the kind is not alone.
    const alone = next.split(',').filter(Boolean).length === 1
    onChange({ ...query, kind: next, facets: alone ? query.facets : {} })
  }

  return (
    <div className="flex flex-col gap-2" aria-label="Activity filters" role="search">
      <div className="flex flex-wrap items-center gap-2">
        <input
          type="search"
          className="sb-field max-w-xs"
          placeholder="Search activity"
          aria-label="Search activity"
          value={text}
          onChange={(e) => setText(e.target.value)}
        />
        <select
          className="sb-field w-auto"
          aria-label="Since"
          value={query.since}
          onChange={(e) => onChange({ ...query, since: e.target.value })}
        >
          <option value="">Any time</option>
          {SINCE_OPTIONS.map((o) => (
            <option key={o.value} value={o.value}>
              {o.label}
            </option>
          ))}
        </select>
      </div>
      {facets && facets.kinds.length > 0 && (
        <Group label="Kind">
          {facets.kinds.map((k) => (
            <button
              key={k.key}
              type="button"
              className={chipClass(selectedKinds.includes(k.key))}
              aria-pressed={selectedKinds.includes(k.key)}
              onClick={() => setKind(toggleValue(query.kind, k.key))}
            >
              {k.label}
            </button>
          ))}
        </Group>
      )}
      {facets && facets.statuses.length > 0 && (
        <Group label="Status">
          {facets.statuses.map((s) => {
            const on = query.status.split(',').includes(s)
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
      {facets && facets.principals.length > 0 && (
        <Group label="Started by">
          {facets.principals.map((p) => (
            <button
              key={p.value}
              type="button"
              className={chipClass(query.principal === p.value)}
              aria-pressed={query.principal === p.value}
              onClick={() => onChange({ ...query, principal: query.principal === p.value ? '' : p.value })}
            >
              {principalLabel(p.value, facets)} <span className="sb-num text-muted">{p.count}</span>
            </button>
          ))}
        </Group>
      )}
      {facets && facets.tools.length > 0 && (
        <Group label="Tool">
          {facets.tools.map((t) => (
            <button
              key={t.value}
              type="button"
              className={chipClass(query.tool === t.value)}
              aria-pressed={query.tool === t.value}
              onClick={() => onChange({ ...query, tool: query.tool === t.value ? '' : t.value })}
            >
              {t.value} <span className="sb-num text-muted">{t.count}</span>
            </button>
          ))}
        </Group>
      )}
      {facets && facets.subjects.length > 0 && (
        <Group label="Subject">
          {facets.subjects.map((s) => (
            <button
              key={s.value}
              type="button"
              className={chipClass(query.subject === s.value)}
              aria-pressed={query.subject === s.value}
              onClick={() => onChange({ ...query, subject: query.subject === s.value ? '' : s.value })}
            >
              {s.value} <span className="sb-num text-muted">{s.count}</span>
            </button>
          ))}
        </Group>
      )}
      {filterable.length > 0 && (
        <div className="flex flex-wrap items-end gap-2">
          {filterable.map((f) => (
            <label key={f.name} className="flex flex-col gap-0.5 text-[12px] text-muted">
              {f.label}
              <input
                className="sb-field w-40"
                value={query.facets[f.name] ?? ''}
                onChange={(e) => onChange({ ...query, facets: { ...query.facets, [f.name]: e.target.value } })}
              />
            </label>
          ))}
        </div>
      )}
    </div>
  )
}
