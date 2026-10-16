import type { Activity, ActivityFacets } from '../../api/types'
import { formatDuration, timeAgo } from '../../lib/format'
import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import { principalLabel } from './labels'
import { StatusBadge } from './StatusBadge'

interface Props {
  items: Activity[]
  facets: ActivityFacets | undefined
  /** Opens a row's detail (the drawer). */
  onOpen?: (id: string) => void
}

/** Parents first, each followed by its loaded children; a child whose parent is not loaded stands alone. */
function nested(items: Activity[]): Array<{ item: Activity; child: boolean }> {
  const ids = new Set(items.map((i) => i.id))
  const out: Array<{ item: Activity; child: boolean }> = []
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

const HEAD = 'h-9 px-3 font-medium whitespace-nowrap'
const CELL = 'px-3 py-2'

export function ActivityTable({ items, facets, onOpen }: Props) {
  return (
    <Card className="gap-0 py-0">
      <div className="overflow-x-auto">
        <table className="w-full text-left text-[13px]" aria-label="Activity">
          <thead className="bg-muted/50 text-xs text-muted-foreground">
            <tr>
              <th className={HEAD}>Status</th>
              <th className={HEAD}>What</th>
              <th className={HEAD}>Started by</th>
              <th className={HEAD}>Started</th>
              <th className={HEAD}>Took</th>
            </tr>
          </thead>
          <tbody>
            {nested(items).map(({ item, child }) => (
              <tr
                key={item.id}
                data-testid={`activity-row-${item.id}`}
                className="border-t border-border align-top transition-colors hover:bg-muted/40"
              >
                <td className={CELL}>
                  <StatusBadge status={item.status} />
                </td>
                <td className={`${CELL} break-words ${child ? 'pl-8' : ''}`}>
                  <Button
                    variant="link"
                    className="h-auto p-0 text-left font-medium whitespace-normal text-foreground"
                    onClick={() => onOpen?.(item.id)}
                  >
                    {item.title}
                  </Button>
                </td>
                <td className={`${CELL} max-w-40 break-all text-muted-foreground`}>
                  {principalLabel(item.principal, facets)}
                </td>
                <td className={`${CELL} whitespace-nowrap text-muted-foreground`}>
                  {item.created_at ? timeAgo(item.created_at) : ''}
                </td>
                <td className={`${CELL} sb-num text-muted-foreground`}>{duration(item)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Card>
  )
}
