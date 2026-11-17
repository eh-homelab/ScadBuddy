import { ArrowDownIcon, ArrowUpIcon } from 'lucide-react'
import { Fragment } from 'react'
import { SpoolLink } from './SpoolLink'
import type { Spool } from '../../api/filament'
import { Badge } from '@/components/ui/badge'
import { Checkbox } from '@/components/ui/checkbox'
import { cn } from '@/lib/utils'
import { RgbaSwatch } from './RgbaSwatch'
import { COLUMN_TITLES, grams, hasUnknownMaterial, needsType, type ColumnId, type SpoolGroup } from './spoolsQuery'

const NUMERIC = new Set<ColumnId>(['grams_left', 'label_weight'])
const UNSORTABLE = new Set<ColumnId>(['type', 'note'])

const day = (at: string | null | undefined) => (at ? new Date(at).toLocaleDateString(undefined, { day: 'numeric', month: 'short', year: 'numeric' }) : '')

/**
 * "Needs type" (no type yet, the filter's meaning) or "Unknown material" (a type, but the
 * material Bambuddy could not name), each with the same way to fix it.
 */
export function NeedsType({ spool }: { spool: Spool }) {
  if (!needsType(spool) && !hasUnknownMaterial(spool)) return null
  return (
    <span className="inline-flex items-center gap-1.5">
      <Badge variant="outline" className="font-normal text-muted-foreground">
        {needsType(spool) ? 'Needs type' : 'Unknown material'}
      </Badge>
      <SpoolLink spool={spool} aria-label={`Set type for spool #${spool.number}`} className="text-[12px] font-medium underline-offset-4 hover:underline">
        Set type
      </SpoolLink>
    </span>
  )
}

function cell(spool: Spool, column: ColumnId, typeCodes: ReadonlyMap<string, string>) {
  switch (column) {
    case 'number':
      return (
        <span className="inline-flex flex-wrap items-center gap-2">
          <SpoolLink spool={spool} className="font-medium underline-offset-4 hover:underline">
            #{spool.number}
          </SpoolLink>
          <NeedsType spool={spool} />
        </span>
      )
    case 'colour':
      return spool.effective?.colour_name ?? ''
    case 'material':
    case 'brand':
    case 'subtype':
    case 'location':
      return spool.names?.[column] ?? ''
    case 'type':
      return spool.type_id ? (typeCodes.get(spool.type_id) ?? '') : ''
    case 'grams_left':
      return grams(spool.grams_left)
    case 'label_weight':
      return grams(spool.effective?.label_weight_g)
    case 'last_used':
      return day(spool.last_used_at)
    case 'note':
      return <span className="line-clamp-1 max-w-64 text-muted-foreground">{spool.note ?? ''}</span>
  }
}

interface Props {
  groups: SpoolGroup[]
  columns: ColumnId[]
  sort: string
  onSort: (sort: string) => void
  selected: ReadonlySet<string>
  onSelect: (ids: string[], on: boolean) => void
  typeCodes: ReadonlyMap<string, string>
}

/**
 * #6262 — the spools as a dense table: a checkbox and the spool chip (its `#RRGGBBAA`
 * over a checker, its grams as a ring) lead every row, then the chosen columns. Text is
 * left-aligned, numbers right-aligned in tabular figures; a header sorts.
 */
export function SpoolsTable({ groups, columns, sort, onSort, selected, onSelect, typeCodes }: Props) {
  const all = groups.flatMap((group) => group.spools.map((spool) => spool.id))
  const chosen = all.filter((id) => selected.has(id)).length
  const descending = sort.startsWith('-')
  const sortedBy = descending ? sort.slice(1) : sort
  return (
    <div className="overflow-x-auto rounded-md border">
      <table aria-label="Spools" className="w-full text-left text-[13px]">
        <thead className="bg-muted/50 text-muted-foreground">
          <tr>
            <th className="w-8 px-2 py-1.5">
              <Checkbox
                aria-label="Select every spool shown"
                checked={chosen > 0 && chosen === all.length ? true : chosen > 0 ? 'indeterminate' : false}
                onCheckedChange={(on) => onSelect(all, on === true)}
              />
            </th>
            <th className="w-7 px-1 py-1.5">
              <span className="sr-only">Colour chip</span>
            </th>
            {columns.map((column) => {
              const active = sortedBy === column
              const label = COLUMN_TITLES[column]
              return (
                <th
                  key={column}
                  scope="col"
                  aria-sort={active ? (descending ? 'descending' : 'ascending') : undefined}
                  className={cn('px-2 py-1.5 font-medium whitespace-nowrap', NUMERIC.has(column) && 'text-right')}
                >
                  {UNSORTABLE.has(column) ? (
                    label
                  ) : (
                    <button
                      type="button"
                      className={cn('inline-flex items-center gap-1 hover:text-foreground', active && 'text-foreground')}
                      onClick={() => onSort(active && !descending ? `-${column}` : column)}
                    >
                      {label}
                      {active && (descending ? <ArrowDownIcon className="size-3" /> : <ArrowUpIcon className="size-3" />)}
                    </button>
                  )}
                </th>
              )
            })}
          </tr>
        </thead>
        <tbody>
          {groups.map((group) => (
            <Fragment key={group.key}>
              {group.label && (
                <tr className="border-t bg-muted/30">
                  <th scope="rowgroup" colSpan={columns.length + 2} className="px-2 py-1 text-[12px] font-medium">
                    {group.label} <span className="font-normal text-muted-foreground tabular-nums">{group.spools.length}</span>
                  </th>
                </tr>
              )}
              {group.spools.map((spool) => (
                <tr
                  key={spool.id}
                  data-spool={spool.number}
                  className={cn('border-t hover:bg-muted/40', selected.has(spool.id) && 'bg-muted/60', spool.archived_at && 'text-muted-foreground')}
                >
                  <td className="px-2 py-1">
                    <Checkbox
                      aria-label={`Select spool #${spool.number}`}
                      checked={selected.has(spool.id)}
                      onCheckedChange={(on) => onSelect([spool.id], on === true)}
                    />
                  </td>
                  <td className="px-1 py-1">
                    <RgbaSwatch
                      rgba={spool.effective?.rgba ?? '#00000000'}
                      gramsLeft={spool.grams_left}
                      labelWeight={spool.effective?.label_weight_g}
                      className="align-middle"
                    />
                  </td>
                  {columns.map((column) => (
                    <td key={column} className={cn('px-2 py-1', NUMERIC.has(column) && 'text-right tabular-nums')}>
                      {cell(spool, column, typeCodes)}
                    </td>
                  ))}
                </tr>
              ))}
            </Fragment>
          ))}
        </tbody>
      </table>
    </div>
  )
}
