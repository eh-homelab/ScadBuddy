import { ArrowLeftRightIcon, CalendarClockIcon, Columns3Icon, DownloadIcon, ListFilterIcon, RefreshCwIcon, LayoutGridIcon, PlusIcon, Rows3Icon, UploadIcon, XIcon } from 'lucide-react'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router'
import { ApiError } from '../../api/client'
import { getInventory, getSummary, listLocations, listSpools, listTags, listTypes, syncAmsWeights, type InventorySummary, type LocationNode, type Spool, type SpoolType } from '../../api/filament'
import { BulkBar, type Place } from '../../components/filament/BulkBar'
import { FilamentSearchBox } from '../../components/filament/FilamentSearchBox'
import { trayChecks, type TrayCheck } from '../../components/filament/CheckTray'
import { SpoolCards } from '../../components/filament/SpoolCards'
import { SpoolForecast } from '../../components/filament/SpoolForecast'
import { SpoolImport } from '../../components/filament/SpoolImport'
import { SpoolsTable } from '../../components/filament/SpoolsTable'
import { SpoolTiles, type TileFilter } from '../../components/filament/SpoolTiles'
import { TypeProposals } from '../../components/filament/TypeProposals'
import { EXPORT_COLUMNS, spoolsCsv } from '../../components/filament/spoolsCsv'
import {
  COLUMN_IDS,
  COLUMN_TITLES,
  DEFAULT_COLUMNS,
  filterSpools,
  groupSpools,
  readQuery,
  readStoredColumns,
  rememberedSpoolsView,
  rememberingSpoolsView,
  rememberSpoolsView,
  setRememberingSpoolsView,
  sortSpools,
  storeColumns,
  writeQuery,
  type ColumnId,
  type SpoolsGroup,
  type SpoolsQuery,
  type SpoolsStatus,
} from '../../components/filament/spoolsQuery'
import { Button } from '@/components/ui/button'
import { Switch } from '@/components/ui/switch'
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
import { readKindPropertyTexts } from '../../components/entities/api'
import { loadTypeSkus, spoolSearchDoc, tagsBySpool, traysBySpool, type PropertyText } from '../../lib/filamentSearch'
import { toast } from '../../lib/toast'
import { syncText } from './syncText'
import { cn } from '@/lib/utils'
import { Checkbox } from '@/components/ui/checkbox'
import { filamentLinks } from '../../lib/filamentLinks'

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
const CHIPS: { key: keyof SpoolsQuery; title: (q: SpoolsQuery, place: string) => string }[] = [
  { key: 'needsType', title: (q) => (q.needsType ? 'Needs type' : 'Has a type') },
  { key: 'lowStock', title: (q) => (q.lowStock ? 'Low stock' : 'Not low') },
  { key: 'used', title: (q) => (q.used ? 'Used' : 'Unused') },
  { key: 'fresh', title: (q) => (q.fresh ? 'New' : 'Not new') },
  { key: 'loaded', title: (q) => (q.loaded ? 'Loaded' : 'Not loaded') },
  { key: 'incomplete', title: (q) => (q.incomplete ? 'Incomplete' : 'Complete') },
  { key: 'material', title: (q) => `Material: ${q.material}` },
  { key: 'brand', title: (q) => `Brand: ${q.brand}` },
  { key: 'insertType', title: (q) => `Insert: ${q.insertType}` },
  { key: 'location', title: (q) => `Location: ${q.location}` },
  { key: 'place', title: (_q, place) => `Location: ${place}` },
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
 * #6262 — the Spools tab: the inventory in tiles, then every spool as a dense table, as
 * cards or as a forecast (#6269: when each runs out, and the types to reorder), filtered, grouped and sorted from the URL (`spoolsQuery.ts`).
 * "Remember my view" makes a visit with no query open the last view (R-T8-10). Select spools to
 * archive, restore, delete, move or type them; export what is shown as CSV, or import a
 * CSV (`SpoolImport`). On the Needs-type filter, `TypeProposals` proposes their types (#6701).
 */
export function SpoolsTab() {
  const [params, setParams] = useSearchParams()
  const [restore, setRestore] = useState(() => (params.size === 0 ? rememberedSpoolsView() : null))
  const [keeping, setKeeping] = useState(rememberingSpoolsView)
  const q = useMemo(() => {
    const read = readQuery(params, readStoredColumns() ?? DEFAULT_COLUMNS)
    return params.size === 0 && restore ? { ...read, view: restore } : read
  }, [params, restore])

  useEffect(() => {
    if (restore === null) return
    setRestore(null)
    if (params.size === 0) setParams(writeQuery(q), { replace: true })
  }, [restore, params, setParams, q])
  const [data, setData] = useState<Loaded | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [selected, setSelected] = useState<ReadonlySet<string>>(new Set())
  const [report, setReport] = useState<string | null>(null)
  const [importing, setImporting] = useState(false)
  const [syncing, setSyncing] = useState(false)
  const [syncError, setSyncError] = useState<string | null>(null)
  const [checks, setChecks] = useState<ReadonlyMap<string, TrayCheck>>(new Map())
  /** #6894: the tray each spool is in, and each type's SKUs, for the search; read after the list. */
  const [trayNames, setTrayNames] = useState<ReadonlyMap<string, string>>(new Map())
  const [skus, setSkus] = useState<ReadonlyMap<string, string[]>>(new Map())
  /** #7151: each spool's open tag keys, so the search finds a spool by its tag. */
  const [tagKeys, setTagKeys] = useState<ReadonlyMap<string, string[]>>(new Map())
  const [propertyTexts, setPropertyTexts] = useState<ReadonlyMap<string, PropertyText[]>>(new Map())
  /** #8372: each spool type's property values, so a spool is found by its type's barcode. */
  const [typePropertyTexts, setTypePropertyTexts] = useState<ReadonlyMap<string, PropertyText[]>>(new Map())
  /** #8372: which reads have settled (a failure settles), so a scan-made search decides once all have. */
  const [settled, setSettled] = useState({ list: false, properties: false, typeProperties: false })
  const settle = useCallback(
    (read: keyof typeof settled) => setSettled((current) => (current[read] ? current : { ...current, [read]: true })),
    [],
  )
  const navigate = useNavigate()

  /** Each load takes a number; only the newest one's answer is used. */
  const latest = useRef(0)
  const load = useCallback(async () => {
    const mine = ++latest.current
    // #7026: each spool's property values, searched as text, read on every load so an edit
    // shows; beside the list, never holding it back. A failed read keeps the map it has
    // (empty when the first read fails): the search still runs on everything else.
    readKindPropertyTexts('spool')
      .then(
        (found) => {
          if (mine === latest.current) setPropertyTexts(found)
        },
        () => {},
      )
      .finally(() => {
        if (mine === latest.current) settle('properties')
      })
    // #8372: the types' values too, read and kept the same way.
    readKindPropertyTexts('spool_type')
      .then(
        (found) => {
          if (mine === latest.current) setTypePropertyTexts(found)
        },
        () => {},
      )
      .finally(() => {
        if (mine === latest.current) settle('typeProperties')
      })
    try {
      const [spools, summary, types, tree] = await Promise.all([listSpools({ includeArchived: true }), getSummary(), listTypes(), listLocations()])
      if (mine !== latest.current) return
      const { places, trays } = flatten(tree)
      setData({ spools, summary, types, places, trays: new Set(trays) })
      setError(null)
    } catch (cause) {
      if (mine === latest.current) setError(message(cause))
    }
    // A load that a newer one replaced settles nothing: its answers were dropped.
    if (mine === latest.current) settle('list')
    // The trays read Bambuddy too, so they come after the list and never hold it back;
    // without them, no spool is flagged (#6713).
    try {
      const view = await getInventory()
      if (mine === latest.current) {
        setChecks(trayChecks(view))
        setTrayNames(traysBySpool(view))
      }
    } catch {
      if (mine === latest.current) {
        setChecks(new Map())
        setTrayNames(new Map())
      }
    }
  }, [settle])

  useEffect(() => {
    let live = true
    // Without the suppliers' products a search still runs on everything but the SKU.
    loadTypeSkus().then(
      (found) => {
        if (live) setSkus(found)
      },
      () => {},
    )
    // Without the tags a search still runs on everything but them.
    listTags().then(
      (found) => {
        if (live) setTagKeys(tagsBySpool(found))
      },
      () => {},
    )
    return () => {
      live = false
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  const update = (next: Partial<SpoolsQuery>) => {
    const merged = { ...q, ...next }
    for (const key of Object.keys(next) as (keyof SpoolsQuery)[]) if (next[key] === undefined) delete merged[key]
    setParams(writeQuery(merged), { replace: true })
    if (next.view) rememberSpoolsView(next.view)
  }

  const typeCodes = useMemo(() => new Map((data?.types ?? []).map((type) => [type.id, type.code])), [data])
  const shown = useMemo(() => {
    if (!data) return []
    const searchDoc = (spool: Spool) =>
      spoolSearchDoc(spool, {
        typeCode: spool.type_id ? typeCodes.get(spool.type_id) : null,
        skus: spool.type_id ? skus.get(spool.type_id) : undefined,
        tray: trayNames.get(spool.id),
        tags: tagKeys.get(spool.id),
        properties: propertyTexts.get(spool.id),
        typeProperties: spool.type_id ? typePropertyTexts.get(spool.type_id) : undefined,
      })
    return sortSpools(filterSpools(data.spools, q, data.trays, searchDoc), q.sort)
  }, [data, q, typeCodes, skus, trayNames, tagKeys, propertyTexts, typePropertyTexts])
  const groups = useMemo(() => groupSpools(shown, q.group, typeCodes), [shown, q.group, typeCodes])
  const chosen = shown.filter((spool) => selected.has(spool.id))

  // #8372: a scan-made search (`from=scan`) waits for the list and both property reads. It does not
  // wait for the SKU, tag or tray reads. Then it
  // decides once: one shown spool opens in the list's place in the history. Else `from` goes
  // out of the address, so the decision does not run again and a later edit of the search never opens a spool.
  const scanning = params.get('from') === 'scan'
  const scanReady = settled.list && settled.properties && settled.typeProperties
  useEffect(() => {
    if (!scanning || !scanReady) return
    if (shown.length === 1) {
      void navigate(filamentLinks.spool(shown[0]!.id), { replace: true })
      return
    }
    setParams(
      (current) => {
        const next = new URLSearchParams(current)
        next.delete('from')
        return next
      },
      { replace: true },
    )
  }, [scanning, scanReady, shown, navigate, setParams])
  const lookingUp = scanning && !scanReady

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

  /** Books each loaded spool's AMS weight, says what it did and what it skipped, and loads the list again. */
  const syncWeights = async () => {
    setSyncing(true)
    try {
      setSyncError(null)
      toast(syncText(await syncAmsWeights()))
      await load()
    } catch (cause) {
      const detail = cause instanceof ApiError ? cause.detail : 'Try again.'
      setSyncError(detail)
      toast({ title: 'The weights could not be synced.', detail })
    } finally {
      setSyncing(false)
    }
  }

  const chips = CHIPS.filter((chip) => q[chip.key] !== undefined)
  /** The `place` filter's path (a tray's, from a spool in it), so two places of one name read apart. */
  const placeName = (q.place && (data?.places.find((one) => one.id === q.place)?.path ?? data?.spools.find((spool) => spool.location_id === q.place)?.names?.location)) || 'one place'

  return (
    <div className="space-y-3">
      {data?.summary && <SpoolTiles summary={data.summary} active={tileFilter} onFilter={filterFromTile} />}

      <div className="flex flex-wrap items-center gap-2">
        <FilamentSearchBox
          label="Search spools"
          placeholder="Search: #87, brand, colour, hex, SKU, place, tag  /"
          value={q.q ?? ''}
          onChange={(text) => update({ q: text || undefined })}
          className="w-full sm:w-80"
        />
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
          <button
            type="button"
            aria-pressed={q.view === 'forecast'}
            aria-label="Forecast"
            onClick={() => update({ view: 'forecast' })}
            className={cn('rounded-sm p-1 text-muted-foreground hover:text-foreground', q.view === 'forecast' && 'bg-muted text-foreground')}
          >
            <CalendarClockIcon className="size-4" />
          </button>
        </div>
        <label className="flex items-center gap-2 text-[12px] text-muted-foreground">
          <Checkbox checked={q.incomplete === true} aria-label="Only incomplete" onCheckedChange={(on) => update({ incomplete: on === true ? true : undefined })} />
          Incomplete
        </label>
        <label className="flex items-center gap-2 text-[12px] text-muted-foreground">
          <Switch
            size="sm"
            checked={keeping}
            onCheckedChange={(on) => {
              setKeeping(on)
              setRememberingSpoolsView(on, q.view)
            }}
          />
          Remember my view
        </label>
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
        <div className="ml-auto flex flex-wrap gap-2">
          <Button size="sm" asChild>
            <Link to={filamentLinks.newSpool}>
              <PlusIcon /> New spool
            </Link>
          </Button>
          <Button size="sm" variant="outline" asChild>
            <Link to={filamentLinks.swap}>
              <ArrowLeftRightIcon /> Swap spools
            </Link>
          </Button>
          <Button size="sm" variant="outline" disabled={syncing} onClick={() => void syncWeights()}>
            <RefreshCwIcon className={syncing ? 'animate-spin motion-reduce:animate-none' : undefined} /> {syncing ? 'Syncing weights…' : 'Sync weights from AMS'}
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
                aria-label={`Clear filter ${chip.title(q, placeName)}`}
                className="inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[12px] hover:bg-muted"
              >
                {chip.title(q, placeName)} <XIcon className="size-3" />
              </button>
            </li>
          ))}
        </ul>
      )}

      {/* #6701: on the Needs-type filter, the types ScadBuddy proposes for those spools. */}
      {q.needsType === true && data && (
        <TypeProposals
          key={data.spools.filter((spool) => !spool.type_id && !spool.archived_at).length}
          onDone={(text) => {
            setReport(text)
            void load()
          }}
        />
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
      {syncError && (
        <p role="alert" className="text-[13px] text-destructive">
          The weights could not be synced. {syncError}
        </p>
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

      {lookingUp && (
        <p role="status" className="rounded-md border px-3 py-6 text-center text-[13px] text-muted-foreground">
          {q.q ? `Looking for “${q.q}”…` : 'Looking for the scanned code…'}
        </p>
      )}
      {data && !shown.length && !lookingUp && (
        <p className="rounded-md border px-3 py-6 text-center text-[13px] text-muted-foreground">
          {!data.spools.length ? 'No spools yet. Add a spool, or import a CSV.' : q.q ? `No spool matches “${q.q}” with these filters.` : 'No spool matches these filters.'}
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
          checks={checks}
        />
      )}
      {data && shown.length > 0 && q.view === 'cards' && <SpoolCards groups={groups} selected={selected} onSelect={select} checks={checks} />}
      {data && q.view === 'forecast' && <SpoolForecast spools={shown} />}

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
