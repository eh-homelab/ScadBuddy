import type { Activity, ActivityFacets } from '../../api/types'
import { formatDuration, timeAgo } from '../../lib/format'
import { Button } from '@/components/ui/button'
import { DataTable, type Column } from '../records/DataTable'
import { principalLabel } from './labels'
import { StatusBadge } from './StatusBadge'

interface Props {
  items: Activity[]
  facets: ActivityFacets | undefined
  /** Opens a row's detail (the drawer). */
  onOpen?: (id: string) => void
}

type Row = { item: Activity; child: boolean }

/** Parents first, each followed by its loaded children; a child whose parent is not loaded stands alone. */
function nested(items: Activity[]): Row[] {
  const ids = new Set(items.map((i) => i.id))
  const out: Row[] = []
  for (const item of items) {
    if (item.parent_id !== null && ids.has(item.parent_id)) continue
    out.push({ item, child: false })
    for (const c of items) if (c.parent_id === item.id) out.push({ item: c, child: true })
  }
  return out
}

function duration(item: Activity): string {
  if (!item.created_at || !item.finished_at) return ''
  return formatDuration((Date.parse(item.finished_at) - Date.parse(item.created_at)) / 1000)
}

/** The Activity list: a `DataTable` (#4249) of the rows, children nested under their parents. */
export function ActivityTable({ items, facets, onOpen }: Props) {
  const columns: Column<Row>[] = [
    { key: 'status', header: 'Status', cell: ({ item }) => <StatusBadge status={item.status} /> },
    {
      key: 'what',
      header: 'What',
      className: ({ child }) => `break-words ${child ? 'pl-8' : ''}`,
      cell: ({ item }, open) => (
        <Button variant="link" className="h-auto p-0 text-left font-medium whitespace-normal text-foreground" onClick={open}>
          {item.title}
        </Button>
      ),
    },
    {
      key: 'by',
      header: 'Started by',
      className: 'max-w-40 break-all text-muted-foreground',
      cell: ({ item }) => principalLabel(item.principal, facets),
    },
    {
      key: 'started',
      header: 'Started',
      className: 'whitespace-nowrap text-muted-foreground',
      cell: ({ item }) => (item.created_at ? timeAgo(item.created_at) : ''),
    },
    { key: 'took', header: 'Took', className: 'sb-num text-muted-foreground', cell: ({ item }) => duration(item) },
  ]
  return (
    <DataTable
      label="Activity"
      columns={columns}
      rows={nested(items)}
      rowKey={({ item }) => item.id}
      rowTestId={({ item }) => `activity-row-${item.id}`}
      onOpen={onOpen ? ({ item }) => onOpen(item.id) : undefined}
    />
  )
}
