import { Columns3Icon, DownloadIcon, ListFilterIcon, LayoutGridIcon, PlusIcon, Rows3Icon, UploadIcon, XIcon } from 'lucide-react'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link, useSearchParams } from 'react-router'
import { ApiError } from '../../api/client'
import { getSummary, listLocations, listSpools, listTypes, type InventorySummary, type LocationNode, type Spool, type SpoolType } from '../../api/filament'
import { BulkBar, type Place } from '../../components/filament/BulkBar'
import { SpoolCards } from '../../components/filament/SpoolCards'
import { SpoolImport } from '../../components/filament/SpoolImport'
import { SpoolsTable } from '../../components/filament/SpoolsTable'
import { SpoolTiles, type TileFilter } from '../../components/filament/SpoolTiles'
import { EXPORT_COLUMNS, spoolsCsv } from '../../components/filament/spoolsCsv'
import {
  COLUMN_IDS,
  COLUMN_TITLES,
  DEFAULT_COLUMNS,
  filterSpools,
  groupSpools,
  readQuery,
  readStoredColumns,
  sortSpools,
  storeColumns,
  writeQuery,
  type ColumnId,
  type SpoolsGroup,
  type SpoolsQuery,
  type SpoolsStatus,
} from '../../components/filament/spoolsQuery'
import { Button } from '@/components/ui/button'
import {
  DropdownMenu,
  DropdownMenuCheckboxItem,
  DropdownMenuContent,
  DropdownMenuLabel,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuSeparator,
  DropdownMenuSub,
  DropdownMenuSubContent,
  DropdownMenuSubTrigger,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { downloadBlob } from '../../lib/embed'
import { cn } from '@/lib/utils'

interface Loaded {
  spools: Spool[]
  summary: InventorySummary | null
  types: SpoolType[]
  places: Place[]
  /** The places that are a printer's tray. */
  trays: Set<string>
}

const STATUSES: { id: SpoolsStatus; title: string }[] = [
  { id: 'active', title: 'Active' },
  { id: 'archived', title: 'Archived' },
  { id: 'all', title: 'All' },
]
const GROUPS: { id: SpoolsGroup; title: string }[] = [
  { id: 'none', title: 'No grouping' },
  { id: 'material', title: 'Material' },
  { id: 'brand', title: 'Brand' },
  { id: 'type', title: 'Type' },
  { id: 'location', title: 'Location' },
]

/** Each filter a chip shows and clears. */
const CHIPS: { key: keyof SpoolsQuery; title: (q: SpoolsQuery) => string }[] = [
  { key: 'needsType', title: (q) => (q.needsType ? 'Needs type' : 'Has a type') },
  { key: 'lowStock', title: (q) => (q.lowStock ? 'Low stock' : 'Not low') },
  { key: 'used', title: (q) => (q.used ? 'Used' : 'Unused') },
  { key: 'fresh', title: (q) => (q.fresh ? 'New' : 'Not new') },
  { key: 'loaded', title: (q) => (q.loaded ? 'Loaded' : 'Not loaded') },
  { key: 'material', title: (q) => `Material: ${q.material}` },
  { key: 'brand', title: (q) => `Brand: ${q.brand}` },
  { key: 'insertType', title: (q) => `Insert: ${q.insertType}` },
  { key: 'location', title: (q) => `Location: ${q.location}` },
]

/** The filters picked by name: the spool's names they match, and the query key each sets. */
const NAME_FILTERS = [
  { key: 'material', title: 'Material', name: (spool: Spool) => spool.names?.material },
  { key: 'brand', title: 'Brand', name: (spool: Spool) => spool.names?.brand },
  { key: 'insertType', title: 'Insert type', name: (spool: Spool) => spool.names?.insert_type },
  { key: 'location', title: 'Location', name: (spool: Spool) => spool.names?.location },
] as const

/** The distinct names in use, in alphabetical order (any case counts once, as the filter matches). */
function namesIn(spools: Spool[], name: (spool: Spool) => string | null | undefined): string[] {
  const found = new Map<string, string>()
  for (const spool of spools) {
    const one = name(spool)
    if (one && !found.has(one.toUpperCase())) found.set(one.toUpperCase(), one)
  }
  return [...found.values()].sort((a, b) => a.localeCompare(b))
}

function flatten(nodes: LocationNode[], trail: string[] = []): { places: Place[]; trays: string[] } {
  const places: Place[] = []
  const trays: string[] = []
  for (const node of nodes) {
    const path = [...trail, node.name]
    // A tray is not a place to move a spool to: loading one is the tray's job (the Locations tab says so too).
    if (node.printer_id) trays.push(node.id)
    else places.push({ id: node.id, path: path.join(' / ') })
    const below = flatten(node.children ?? [], path)
    places.push(...below.places)
    trays.push(...below.trays)
  }
  return { places, trays }
}

const message = (cause: unknown) => (cause instanceof ApiError ? cause.detail : 'The spools could not be loaded. Try again.')

/**
 * #6262 — the Spools tab: the inventory in tiles, then every spool as a dense table or as
 * cards, filtered, grouped and sorted from the URL (`spoolsQuery.ts`). Select spools to
 * archive, restore, delete, move or type them; export what is shown as CSV, or import a
 * CSV (`SpoolImport`).
 */
export function SpoolsTab() {
  const [params, setParams] = useSearchParams()
  const q = useMemo(() => readQuery(params, readStoredColumns() ?? DEFAULT_COLUMNS), [params])
  const [data, setData] = useState<Loaded | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [selected, setSelected] = useState<ReadonlySet<string>>(new Set())
  const [report, setReport] = useState<string | null>(null)
  const [importing, setImporting] = useState(false)

  /** Each load takes a number; only the newest one's answer is used. */
  const latest = useRef(0)
  const load = useCallback(async () => {
    const mine = ++latest.current
    try {
      const [spools, summary, types, tree] = await Promise.all([listSpools({ includeArchived: true }), getSummary(), listTypes(), listLocations()])
      if (mine !== latest.current) return
      const { places, trays } = flatten(tree)
      setData({ spools, summary, types, places, trays: new Set(trays) })
      setError(null)
    } catch (cause) {
      if (mine === latest.current) setError(message(cause))
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  const update = (next: Partial<SpoolsQuery>) => {
    const merged = { ...q, ...next }
    for (const key of Object.keys(next) as (keyof SpoolsQuery)[]) if (next[key] === undefined) delete merged[key]
    setParams(writeQuery(merged), { replace: true })
  }

  const typeCodes = useMemo(() => new Map((data?.types ?? []).map((type) => [type.id, type.code])), [data])
  const shown = useMemo(() => (data ? sortSpools(filterSpools(data.spools, q, data.trays), q.sort) : []), [data, q])
  const groups = useMemo(() => groupSpools(shown, q.group, typeCodes), [shown, q.group, typeCodes])
  const chosen = shown.filter((spool) => selected.has(spool.id))

  const select = (ids: string[], on: boolean) =>
    setSelected((current) => {
      const next = new Set(current)
      for (const id of ids) {
        if (on) next.add(id)
        else next.delete(id)
      }
      return next
    })

  const setColumns = (columns: ColumnId[]) => {
    storeColumns(columns)
    update({ columns })
  }

  const tileFilter: TileFilter = { lowStock: q.lowStock, needsType: q.needsType, material: q.material, loaded: q.loaded }
  const filterFromTile = (filter: TileFilter) =>
    update({ lowStock: undefined, needsType: undefined, material: undefined, loaded: undefined, used: undefined, fresh: undefined, ...filter })

  const exportCsv = async () => {
    try {
      const text = spoolsCsv(shown, EXPORT_COLUMNS, typeCodes)
      await downloadBlob(async () => new Blob([text], { type: 'text/csv;charset=utf-8' }), `spools-${new Date().toISOString().slice(0, 10)}.csv`)
    } catch (cause) {
      setReport(cause instanceof Error ? cause.message : 'The download did not start.')
    }
  }

  const chips = CHIPS.filter((chip) => q[chip.key] !== undefined)

  return (
    <div className="space-y-3">
      {data?.summary && <SpoolTiles summary={data.summary} active={tileFilter} onFilter={filterFromTile} />}

      <div className="flex flex-wrap items-center gap-2">
        <div role="group" aria-label="Status" className="inline-flex rounded-md border p-0.5">
          {STATUSES.map((status) => (
            <button
              key={status.id}
              type="button"
              aria-pressed={q.status === status.id}
              onClick={() => update({ status: status.id })}
              className={cn('rounded-sm px-2 py-0.5 text-[13px] text-muted-foreground hover:text-foreground', q.status === status.id && 'bg-muted text-foreground')}
            >
              {status.title}
            </button>
          ))}
        </div>
        <div role="group" aria-label="View" className="inline-flex rounded-md border p-0.5">
          <button
            type="button"
            aria-pressed={q.view === 'table'}
            aria-label="Table"
            onClick={() => update({ view: 'table' })}
            className={cn('rounded-sm p-1 text-muted-foreground hover:text-foreground', q.view === 'table' && 'bg-muted text-foreground')}
          >
            <Rows3Icon className="size-4" />
          </button>
          <button
            type="button"
            aria-pressed={q.view === 'cards'}
            aria-label="Cards"
            onClick={() => update({ view: 'cards' })}
            className={cn('rounded-sm p-1 text-muted-foreground hover:text-foreground', q.view === 'cards' && 'bg-muted text-foreground')}
          >
            <LayoutGridIcon className="size-4" />
          </button>
        </div>
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button size="sm" variant="outline">
              <ListFilterIcon /> Filter
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="start">
            {NAME_FILTERS.map((filter) => {
              const names = namesIn(data?.spools ?? [], filter.name)
              const picked = q[filter.key]
              // A link may differ in case from the list's name; the filter matches in any case.
              const current = names.find((name) => name.toUpperCase() === picked?.toUpperCase()) ?? picked ?? ''
              return (
                <DropdownMenuSub key={filter.key}>
                  <DropdownMenuSubTrigger>{filter.title}</DropdownMenuSubTrigger>
                  <DropdownMenuSubContent className="max-h-72 overflow-y-auto">
                    <DropdownMenuRadioGroup value={current} onValueChange={(value) => update({ [filter.key]: value || undefined })}>
                      <DropdownMenuRadioItem value="">Any {filter.title.toLowerCase()}</DropdownMenuRadioItem>
                      {names.map((name) => (
                        <DropdownMenuRadioItem key={name} value={name}>
                          {name}
                        </DropdownMenuRadioItem>
                      ))}
                    </DropdownMenuRadioGroup>
                  </DropdownMenuSubContent>
                </DropdownMenuSub>
              )
            })}
            <DropdownMenuSeparator />
            <DropdownMenuLabel>Use</DropdownMenuLabel>
            <DropdownMenuRadioGroup
              value={q.used ? 'used' : q.fresh ? 'new' : ''}
              onValueChange={(value) => update({ used: value === 'used' ? true : undefined, fresh: value === 'new' ? true : undefined })}
            >
              <DropdownMenuRadioItem value="">Any</DropdownMenuRadioItem>
              <DropdownMenuRadioItem value="used">Used: some filament gone</DropdownMenuRadioItem>
              <DropdownMenuRadioItem value="new">New: full and never printed</DropdownMenuRadioItem>
            </DropdownMenuRadioGroup>
          </DropdownMenuContent>
        </DropdownMenu>
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button size="sm" variant="outline">
              Group: {GROUPS.find((group) => group.id === (q.group ?? 'none'))!.title}
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="start">
            <DropdownMenuRadioGroup value={q.group ?? 'none'} onValueChange={(value) => update({ group: value === 'none' ? undefined : (value as SpoolsGroup) })}>
              {GROUPS.map((group) => (
                <DropdownMenuRadioItem key={group.id} value={group.id}>
                  {group.title}
                </DropdownMenuRadioItem>
              ))}
            </DropdownMenuRadioGroup>
          </DropdownMenuContent>
        </DropdownMenu>
        {q.view === 'table' && (
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button size="sm" variant="outline">
                <Columns3Icon /> Columns
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="start">
              <DropdownMenuLabel>Columns shown</DropdownMenuLabel>
              {COLUMN_IDS.map((column) => (
                <DropdownMenuCheckboxItem
                  key={column}
                  checked={q.columns.includes(column)}
                  disabled={column === 'number'}
                  onSelect={(event) => event.preventDefault()}
                  onCheckedChange={(on) => setColumns(COLUMN_IDS.filter((id) => (id === column ? on : q.columns.includes(id))))}
                >
                  {column === 'number' ? 'Spool number' : COLUMN_TITLES[column]}
                </DropdownMenuCheckboxItem>
              ))}
            </DropdownMenuContent>
          </DropdownMenu>
        )}
        <div className="ml-auto flex gap-2">
          <Button size="sm" asChild>
            <Link to="/filament/spools/new">
              <PlusIcon /> New spool
            </Link>
          </Button>
          <Button size="sm" variant="outline" disabled={!shown.length} onClick={() => void exportCsv()}>
            <DownloadIcon /> Export CSV
          </Button>
          <Button size="sm" variant="outline" onClick={() => setImporting(true)}>
            <UploadIcon /> Import CSV
          </Button>
        </div>
      </div>

      {chips.length > 0 && (
        <ul aria-label="Filters" className="flex flex-wrap gap-1.5">
          {chips.map((chip) => (
            <li key={chip.key}>
              <button
                type="button"
                onClick={() => update({ [chip.key]: undefined })}
                aria-label={`Clear filter ${chip.title(q)}`}
                className="inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[12px] hover:bg-muted"
              >
                {chip.title(q)} <XIcon className="size-3" />
              </button>
            </li>
          ))}
        </ul>
      )}

      {chosen.length > 0 && data && (
        <BulkBar
          selected={chosen}
          places={data.places}
          types={data.types}
          onClear={() => setSelected(new Set())}
          onDone={(text) => {
            setReport(text)
            setSelected(new Set())
            void load()
          }}
        />
      )}
      {report && (
        <p role="status" className="text-[13px]">
          {report}
        </p>
      )}
      {error && (
        <p role="alert" className="text-[13px] text-destructive">
          {error}
        </p>
      )}

      {data && !shown.length && (
        <p className="rounded-md border px-3 py-6 text-center text-[13px] text-muted-foreground">
          {data.spools.length ? 'No spool matches these filters.' : 'No spools yet. Add a spool, or import a CSV.'}
        </p>
      )}
      {data && shown.length > 0 && q.view === 'table' && (
        <SpoolsTable
          groups={groups}
          columns={q.columns.includes('number') ? q.columns : ['number', ...q.columns]}
          sort={q.sort}
          onSort={(sort) => update({ sort })}
          selected={selected}
          onSelect={select}
          typeCodes={typeCodes}
        />
      )}
      {data && shown.length > 0 && q.view === 'cards' && <SpoolCards groups={groups} selected={selected} onSelect={select} />}

      <SpoolImport
        open={importing}
        onOpenChange={setImporting}
        onImported={(done) => {
          const made = done.rows.filter((row) => row.action === 'create').length
          setReport(`Imported: ${made} added, ${done.rows.length - made} changed.`)
          void load()
        }}
      />
    </div>
  )
}
