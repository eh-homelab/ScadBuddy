import type { InventorySummary } from '../../api/filament'
import { cn } from '@/lib/utils'
import { grams, type SpoolsQuery } from './spoolsQuery'

/** The filters a tile sets; every other filter is cleared with it. */
export type TileFilter = Pick<SpoolsQuery, 'lowStock' | 'needsType' | 'material' | 'loaded'>

interface Props {
  summary: InventorySummary
  active: TileFilter
  onFilter: (filter: TileFilter) => void
}

function Tile({ label, value, pressed, onClick, unit }: { label: string; value: string; pressed?: boolean; onClick?: () => void; unit?: string }) {
  const body = (
    <>
      <span className="block text-[12px] text-muted-foreground">{label}</span>
      <span className="block text-base font-semibold tabular-nums">
        {value}
        {unit && <span className="ml-0.5 text-[12px] font-normal text-muted-foreground">{unit}</span>}
      </span>
    </>
  )
  if (!onClick) return <div className="px-3 py-2">{body}</div>
  return (
    <button
      type="button"
      aria-pressed={pressed}
      onClick={onClick}
      className={cn('px-3 py-2 text-left hover:bg-muted/50 focus-visible:ring-[3px] focus-visible:ring-ring/50 focus-visible:outline-none', pressed && 'bg-muted')}
    >
      {body}
    </button>
  )
}

/**
 * #6262 — the inventory in a line, from `GET /filament/summary`: spools, grams left, the
 * five materials with most spools, loaded, low stock and needing a type. Every tile but
 * the grams sets its filter (and clears the others); pressed again, it clears it. No "consumed"
 * tile (#6269, plan Decision 11).
 */
export function SpoolTiles({ summary, active, onFilter }: Props) {
  const materials = [...summary.by_material].filter((one) => one.material).sort((a, b) => b.spools - a.spools).slice(0, 5)
  return (
    <div role="group" aria-label="Inventory" className="flex flex-wrap divide-x rounded-md border">
      <Tile label="Spools" value={String(summary.spools)} pressed={!active.lowStock && !active.needsType && !active.material && !active.loaded} onClick={() => onFilter({})} />
      <Tile label="Grams left" value={grams(summary.grams_left)} unit="g" />
      {materials.map((one) => (
        <Tile
          key={one.material}
          label={one.material!}
          value={String(one.spools)}
          pressed={active.material === one.material}
          onClick={() => onFilter(active.material === one.material ? {} : { material: one.material! })}
        />
      ))}
      <Tile label="Loaded" value={String(summary.loaded)} pressed={Boolean(active.loaded)} onClick={() => onFilter(active.loaded ? {} : { loaded: true })} />
      <Tile label="Low stock" value={String(summary.low_stock)} pressed={Boolean(active.lowStock)} onClick={() => onFilter(active.lowStock ? {} : { lowStock: true })} />
      <Tile label="Needs type" value={String(summary.needs_type)} pressed={Boolean(active.needsType)} onClick={() => onFilter(active.needsType ? {} : { needsType: true })} />
    </div>
  )
}
