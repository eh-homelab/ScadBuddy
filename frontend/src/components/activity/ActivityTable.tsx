import type { Activity, ActivityFacets } from '../../api/types'
import { formatDuration, timeAgo } from '../../lib/format'
import { statusLabel } from '../prints/status'
import { principalLabel } from './labels'

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

export function ActivityTable({ items, facets, onOpen }: Props) {
  return (
    <div className="overflow-x-auto">
    <table className="w-full text-left text-[13px]" aria-label="Activity">
      <thead className="text-[12px] text-muted">
        <tr>
          <th className="py-1 pr-3 font-normal">Status</th>
          <th className="py-1 pr-3 font-normal">What</th>
          <th className="py-1 pr-3 font-normal">Started by</th>
          <th className="py-1 pr-3 font-normal">Started</th>
          <th className="py-1 font-normal">Took</th>
        </tr>
      </thead>
      <tbody>
        {nested(items).map(({ item, child }) => (
          <tr
            key={item.id}
            data-testid={`activity-row-${item.id}`}
            className="border-t border-line align-top"
          >
            <td className="py-1.5 pr-3">{statusLabel(item.status)}</td>
            <td className={`py-1.5 pr-3 break-words ${child ? 'pl-5' : ''}`}>
              <button type="button" className="text-left hover:underline" onClick={() => onOpen?.(item.id)}>
                {item.title}
              </button>
            </td>
            <td className="max-w-40 break-all py-1.5 pr-3 text-muted">{principalLabel(item.principal, facets)}</td>
            <td className="py-1.5 pr-3 text-muted">{item.created_at ? timeAgo(item.created_at) : ''}</td>
            <td className="sb-num py-1.5 text-muted">{duration(item)}</td>
          </tr>
        ))}
      </tbody>
    </table>
    </div>
  )
}
