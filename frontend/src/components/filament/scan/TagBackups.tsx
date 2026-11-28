import { ChevronDownIcon, HistoryIcon } from 'lucide-react'
import { useState } from 'react'
import { listTagBackups, type TagBackup } from '../../../api/filament'
import { headlineOf, restoreBackup } from '../../../lib/tagWrite'
import { useAsync } from '../../../lib/useAsync'
import type { NfcSession } from '../../../lib/webNfc'
import { WriteFlow } from './WriteTag'
import { Button } from '@/components/ui/button'
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible'

const REASONS: Record<TagBackup['reason'], string> = {
  spool_write: 'before a spool write',
  agent_write: 'before an agent write',
  restore: 'before a restore',
  place_write: 'before a place write',
  clear: 'before a clear',
}

/** The route's default page: a full page means there are more. */
const PAGE = 50

const timeOf = (at: string) => new Date(at).toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' })
const countOf = (n: number) => (n === 1 ? '1 record' : `${n} records`)

/**
 * #7273 G7 — a tag's backups on the scan page, newest first: what it held before each write.
 * "Restore" writes a backup's records back, itself backed up first; the page checks the
 * read-back and posts it as a read, and binds nothing (R6).
 */
export function TagBackups({
  session,
  tagId,
  refresh = 0,
  busy = false,
  onRunning,
  onAnnounce,
}: {
  session: NfcSession
  tagId: string
  refresh?: number
  /** Another write runs on this page: no restore starts until it ends. */
  busy?: boolean
  /** True while a restore run is under way. */
  onRunning?: (running: boolean) => void
  onAnnounce?: (text: string) => void
}) {
  const [restored, setRestored] = useState(0)
  const list = useAsync(() => listTagBackups(tagId), [tagId, refresh, restored])
  const [restoring, setRestoring] = useState<TagBackup | null>(null)
  const rows = list.data ?? []

  return (
    <Collapsible className="flex flex-col gap-2">
      <CollapsibleTrigger asChild>
        <Button type="button" variant="ghost" className="group h-11 justify-between px-2">
          <span className="inline-flex items-center gap-2">
            <HistoryIcon aria-hidden="true" />
            {list.data ? `Backups (${rows.length >= PAGE ? `${PAGE}+` : rows.length})` : 'Backups'}
          </span>
          <ChevronDownIcon aria-hidden="true" className="transition-transform group-data-[state=open]:rotate-180 motion-reduce:transition-none" />
        </Button>
      </CollapsibleTrigger>
      <CollapsibleContent className="flex flex-col gap-3">
        {list.error && (
          <p role="alert" className="text-warn">
            The backups could not be read: {list.error.message}
          </p>
        )}
        {list.data && rows.length === 0 && <p className="text-muted-foreground">No backups yet. Each write keeps one first.</p>}
        {rows.length > 0 && (
          <ul aria-label="Backups" className="flex flex-col divide-y divide-border rounded-lg border border-border">
            {rows.map((row) => (
              <li key={row.id} className="grid grid-cols-[1fr_auto] items-center gap-3 px-3 py-2">
                <div id={`backup-${row.id}`} className="flex min-w-0 flex-col">
                  <span className="tabular-nums">{timeOf(row.at)}</span>
                  <span className="text-muted-foreground">
                    {REASONS[row.reason]}, {countOf(row.records.length)}
                  </span>
                  <span className="truncate text-muted-foreground">{row.actor}</span>
                </div>
                <Button type="button" variant="outline" className="h-11" aria-describedby={`backup-${row.id}`} disabled={busy || restoring !== null} onClick={() => setRestoring(row)}>
                  Restore
                </Button>
              </li>
            ))}
          </ul>
        )}
        {restoring && (
          <WriteFlow
            key={restoring.id}
            session={session}
            label="Restore a backup"
            mode="restore"
            run={({ onStep, onClaimed, signal }) => restoreBackup({ session, tagId, backupId: restoring.id, onStep, onClaimed, signal })}
            onSettled={(outcome) => {
              setRestored((count) => count + 1)
              onAnnounce?.(headlineOf(outcome, 'restore'))
            }}
            onRunning={onRunning}
            onClose={() => setRestoring(null)}
          />
        )}
      </CollapsibleContent>
    </Collapsible>
  )
}
