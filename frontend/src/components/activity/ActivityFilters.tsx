import { useEffect, useRef, useState } from 'react'
import { ChipGroup as Group, Count, DebouncedInput, SearchField } from '../records/FilterBar'
import { CHIP, shownChips } from '../records/filterChips'
import { DEFAULT_ACTIVITY_QUERY, isActivityFiltered, joinList, splitList, type ActivityQuery } from '../../lib/activityQuery'
import type { ActivityFacets } from '../../api/types'
import { statusLabel } from '../prints/status'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Label } from '@/components/ui/label'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { ToggleGroup, ToggleGroupItem } from '@/components/ui/toggle-group'
import { XIcon } from 'lucide-react'
import { principalLabel, SINCE_OPTIONS, subjectLabel } from './labels'

interface Props {
  query: ActivityQuery
  facets: ActivityFacets | undefined
  /** `typing` is a change made keystroke by keystroke: it replaces the history entry. */
  onChange: (next: ActivityQuery, options?: { typing?: boolean }) => void
}

/** Radix Select takes no empty value: this stands for "no `since`". */
const ANY_TIME = 'any'

/** The values of one facet as chips, plus any selected value the facets do not list. */
type Counted = ActivityFacets['subjects'][number]

function valueChips(
  listed: Counted[],
  selected: string,
): Array<Omit<Counted, 'count'> & { count: number | null }> {
  const extra = selected && !listed.some((l) => l.value === selected) ? [{ value: selected, count: null }] : []
  return [...listed, ...extra]
}

/** The filters above the table, on the records `FilterBar` parts (#4249); every choice comes from the facets endpoint. */
export function ActivityFilters({ query, facets, onChange }: Props) {
  const [all, setAll] = useState<Record<string, boolean>>({})
  const showAll = (group: string) => (next: boolean) => setAll((was) => ({ ...was, [group]: next }))
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
  const shownKinds = shownChips(kinds, (k) => k.key, selectedKinds, all.kind === true)
  const shownPrincipals = shownChips(principals, (p) => p.value, [query.principal], all.principal === true)
  const shownTools = shownChips(tools, (t) => t.value, [query.tool], all.tool === true)
  const shownSubjects = shownChips(subjects, (s) => s.value, [query.subject], all.subject === true)

  return (
    <div className="flex flex-col gap-2.5" aria-label="Activity filters" role="search">
      <div className="flex flex-wrap items-center gap-2">
        <SearchField
          placeholder="Search activity"
          value={query.q}
          onChange={(q) => onChange({ ...latest.current, q }, { typing: true })}
        />
        <Select
          value={sinceKnown ? query.since : ANY_TIME}
          onValueChange={(since) => onChange({ ...query, since: since === ANY_TIME ? '' : since })}
        >
          <SelectTrigger aria-label="Since">
            <SelectValue />
          </SelectTrigger>
          <SelectContent position="popper" align="start">
            <SelectItem value={ANY_TIME}>Any time</SelectItem>
            {SINCE_OPTIONS.map((o) => (
              <SelectItem key={o.value} value={o.value}>
                {o.label}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        {isActivityFiltered(query) && (
          <Button variant="ghost" size="sm" onClick={() => onChange(DEFAULT_ACTIVITY_QUERY)}>
            Clear filters
          </Button>
        )}
      </div>
      {kinds.length > 0 && (
        <Group label="Kind" hidden={kinds.length - shownKinds.length} all={all.kind} onAll={showAll('kind')}>
          <ToggleGroup
            type="multiple"
            variant="outline"
            size="sm"
            spacing={1.5}
            aria-label="Kind"
            className="flex-wrap"
            value={selectedKinds}
            onValueChange={(next) => setKind(joinList(next))}
          >
            {shownKinds.map((k) => (
              <ToggleGroupItem key={k.key} value={k.key} className={CHIP}>
                {k.label}
              </ToggleGroupItem>
            ))}
          </ToggleGroup>
        </Group>
      )}
      {statuses.length > 0 && (
        <Group label="Status">
          <ToggleGroup
            type="multiple"
            variant="outline"
            size="sm"
            spacing={1.5}
            aria-label="Status"
            className="flex-wrap"
            value={selectedStatuses}
            onValueChange={(next) => onChange({ ...query, status: joinList(next) })}
          >
            {statuses.map((s) => (
              <ToggleGroupItem key={s} value={s} className={CHIP}>
                {statusLabel(s)}
              </ToggleGroupItem>
            ))}
          </ToggleGroup>
        </Group>
      )}
      {principals.length > 0 && (
        <Group
          label="Started by"
          hidden={principals.length - shownPrincipals.length}
          all={all.principal}
          onAll={showAll('principal')}
        >
          <ToggleGroup
            type="single"
            variant="outline"
            size="sm"
            spacing={1.5}
            aria-label="Started by"
            className="flex-wrap"
            value={query.principal}
            onValueChange={(principal) => onChange({ ...query, principal })}
          >
            {shownPrincipals.map((p) => (
              <ToggleGroupItem key={p.value} value={p.value} className={CHIP}>
                {principalLabel(p.value, facets)}
                <Count count={p.count} />
              </ToggleGroupItem>
            ))}
          </ToggleGroup>
        </Group>
      )}
      {tools.length > 0 && (
        <Group label="Tool" hidden={tools.length - shownTools.length} all={all.tool} onAll={showAll('tool')}>
          <ToggleGroup
            type="single"
            variant="outline"
            size="sm"
            spacing={1.5}
            aria-label="Tool"
            className="flex-wrap"
            value={query.tool}
            onValueChange={(tool) => onChange({ ...query, tool })}
          >
            {shownTools.map((t) => (
              <ToggleGroupItem key={t.value} value={t.value} className={CHIP}>
                {t.value}
                <Count count={t.count} />
              </ToggleGroupItem>
            ))}
          </ToggleGroup>
        </Group>
      )}
      {subjects.length > 0 && (
        <Group
          label="Subject"
          hidden={subjects.length - shownSubjects.length}
          all={all.subject}
          onAll={showAll('subject')}
        >
          <ToggleGroup
            type="single"
            variant="outline"
            size="sm"
            spacing={1.5}
            aria-label="Subject"
            className="flex-wrap"
            value={query.subject}
            onValueChange={(subject) => onChange({ ...query, subject })}
          >
            {shownSubjects.map((s) => (
              <ToggleGroupItem key={s.value} value={s.value} className={CHIP} title={s.value}>
                {/* Unclipped until the facets load (#4003): two lines at most meanwhile, with no
                    clamp once subjectLabel is doing its own clipping, so the two never double up. */}
                <span className={facets?.subject_name_chars === undefined ? 'line-clamp-2' : undefined}>
                  {subjectLabel(s, facets?.subject_name_chars)}
                </span>
                <Count count={s.count} />
              </ToggleGroupItem>
            ))}
          </ToggleGroup>
        </Group>
      )}
      {filterable.length > 0 && (
        <div className="flex flex-wrap items-end gap-2">
          {filterable.map((f) => (
            <div key={f.name} className="flex w-40 max-w-full flex-col gap-1.5">
              <Label htmlFor={`activity-facet-${f.name}`} className="text-xs font-normal text-muted-foreground">
                {f.label}
              </Label>
              <DebouncedInput
                id={`activity-facet-${f.name}`}
                value={query.facets[f.name] ?? ''}
                onCommit={(v) => setFacet(f.name, v, true)}
              />
            </div>
          ))}
        </div>
      )}
      {strayFacets.length > 0 && (
        <div role="group" aria-label="Applied facets" className="flex flex-wrap items-center gap-1.5">
          <span aria-hidden="true" className="text-xs text-muted-foreground">
            Applied facets
          </span>
          {strayFacets.map(([name, value]) => (
            <Badge key={name} variant="secondary" className="h-auto max-w-full gap-0.5 py-0.5 pr-0.5 break-all whitespace-normal">
              {name}: {value}
              <Button
                variant="ghost"
                size="icon-xs"
                className="size-4 rounded-full"
                aria-label={`Remove ${name} ${value}`}
                onClick={() => setFacet(name, '', false)}
              >
                <XIcon />
              </Button>
            </Badge>
          ))}
        </div>
      )}
    </div>
  )
}
