import { ArchiveIcon, ArchiveRestoreIcon, PencilIcon } from 'lucide-react'
import { Link } from 'react-router'
import type { LocationNode, Spool } from '../../api/filament'
import { Button } from '@/components/ui/button'
import { spoolPath } from './spoolsQuery'

export interface LocationTreeProps {
  nodes: LocationNode[]
  /** Each place's name for its kind. */
  kinds: ReadonlyMap<string, string>
  /** The unarchived spools in each place, by place id. */
  spoolsAt: ReadonlyMap<string, Spool[]>
  onEdit: (node: LocationNode) => void
  onArchive: (node: LocationNode, archived: boolean) => void
}

/** What a spool is called in a place's list: its number and colour. */
const spoolLabel = (spool: Spool) => `#${spool.number} ${spool.effective?.colour_name ?? spool.names?.material ?? ''}`.trim()

function Place({ node, ...tree }: Omit<LocationTreeProps, 'nodes'> & { node: LocationNode }) {
  // A tray is the printer's: the registry and Bambuddy name it, so it is not edited here.
  const tray = node.printer_id != null
  const archived = node.archived_at != null
  const spools = tree.spoolsAt.get(node.id) ?? []
  return (
    <li data-place={node.id}>
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 py-1.5">
        <span className={archived ? 'font-medium text-muted-foreground line-through' : 'font-medium'}>{node.name}</span>
        <span className="text-muted-foreground">{tree.kinds.get(node.kind_id) ?? ''}</span>
        {node.identifier && <span className="text-muted-foreground">{node.identifier}</span>}
        <span
          aria-label={`${node.spool_count} ${node.spool_count === 1 ? 'spool' : 'spools'}`}
          className="rounded-full bg-muted px-2 text-[12px] tabular-nums text-muted-foreground"
        >
          {node.spool_count}
          {node.capacity != null ? ` / ${node.capacity}` : ''}
        </span>
        {tray && <span className="text-[12px] text-muted-foreground">AMS tray, read only</span>}
        {archived && <span className="text-[12px] text-muted-foreground">Archived</span>}
        {!tray && (
          <span className="ml-auto flex gap-1">
            <Button type="button" size="xs" variant="ghost" aria-label={`Edit ${node.name}`} onClick={() => tree.onEdit(node)}>
              <PencilIcon /> Edit
            </Button>
            <Button
              type="button"
              size="xs"
              variant="ghost"
              aria-label={`${archived ? 'Restore' : 'Archive'} ${node.name}`}
              onClick={() => tree.onArchive(node, !archived)}
            >
              {archived ? <ArchiveRestoreIcon /> : <ArchiveIcon />} {archived ? 'Restore' : 'Archive'}
            </Button>
          </span>
        )}
      </div>
      {spools.length > 0 && (
        <ul className="flex flex-wrap gap-x-3 gap-y-0.5 pb-1.5 pl-4 text-[12px]">
          {spools.map((spool) => (
            <li key={spool.id}>
              <Link to={spoolPath(spool)} className="underline-offset-4 hover:underline">
                {spoolLabel(spool)}
              </Link>
            </li>
          ))}
        </ul>
      )}
      {(node.children ?? []).length > 0 && (
        <ul className="ml-4 border-l pl-3">
          {(node.children ?? []).map((child) => (
            <Place key={child.id} node={child} {...tree} />
          ))}
        </ul>
      )}
    </li>
  )
}

/** #6262 — the places spools are kept, nested under their parents, each with its spools. */
export function LocationTree({ nodes, ...tree }: LocationTreeProps) {
  if (nodes.length === 0) return <p className="text-[13px] text-muted-foreground">No places yet. Add a room or a shelf to start.</p>
  return (
    <ul aria-label="Locations" className="rounded-md border px-3 py-1 text-[13px]">
      {nodes.map((node) => (
        <Place key={node.id} node={node} {...tree} />
      ))}
    </ul>
  )
}
