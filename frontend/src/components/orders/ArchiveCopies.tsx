import { CheckIcon } from 'lucide-react'
import { useEffect, useEffectEvent, useState } from 'react'
import { api } from '../../api/client'
import type { ArchiveCopy, ArchiveOffer, AttemptArchives, Fulfilment } from '../../api/types'
import { Button } from '@/components/ui/button'
import { refusalText } from './refusal'

/** The row reads the archives again this often while an import is still due (spec 2026-11-05 §4.4). */
export const ARCHIVE_READ_MS = 10_000
/** It reads again at most this many times after it mounts or a copy, then offers the copy buttons. */
export const ARCHIVE_READS = 6
/** Bambuddy writes the timelapse minutes after the print ends; after this long, it has none. */
const TIMELAPSE_WAIT_MS = 60 * 60_000

/** Whether the archive offers a medium the attempt has not attached yet, and the import did not refuse as too large. */
function due(archive: ArchiveOffer): boolean {
  return (
    ((archive.finish_photos ?? []).length > 0 && !archive.finished_attachment_id && !archive.finished_too_large) ||
    (Boolean(archive.timelapse) && !archive.timelapse_attachment_id && !archive.timelapse_too_large)
  )
}

/** The attempt facts a read copies from the archives (`mirror_archives`). */
function facts(attempt: Fulfilment): string {
  return JSON.stringify([attempt.archive_ids ?? [], attempt.actual_seconds ?? null, attempt.actual_grams ?? null])
}

function attachedIds(read: AttemptArchives | null): string {
  return (read?.archives ?? []).flatMap((a) => [a.finished_attachment_id ?? '', a.timelapse_attachment_id ?? '']).join(',')
}

/**
 * #4253, #7236 (spec 2026-11-05 §4.4): a printed attempt's Bambuddy archives. The row reads
 * them when it mounts; that read starts the import of each archive's finish photo and
 * timelapse and answers what is attached now. No order event tells the page of an import
 * (F13), so while a medium is offered and not attached the row reads again, every
 * `ARCHIVE_READ_MS`, at most `ARCHIVE_READS` times; then it offers a copy of only what is
 * still missing. The page is told to reload when a read changed the attempt (times,
 * grams) or attached a file.
 */
export function ArchiveCopies({ orderId, itemId, attempt, onChanged }: { orderId: string; itemId: string; attempt: Fulfilment; onChanged: () => void }) {
  const [read, setRead] = useState<AttemptArchives | null>(null)
  // When the answer came: the timelapse states are as of that read.
  const [readAt, setReadAt] = useState(0)
  // Bumped to read again; `reads` counts the re-reads since the mount or the last copy.
  const [asked, setAsked] = useState(0)
  const [reads, setReads] = useState(0)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const answered = useEffectEvent((answer: AttemptArchives) => {
    const changed = facts(answer.attempt) !== facts(attempt) || (read !== null && attachedIds(answer) !== attachedIds(read))
    setRead(answer)
    setReadAt(Date.now())
    setError(null)
    if (changed) onChanged()
  })
  const failed = useEffectEvent((err: unknown) => setError(refusalText(err)))

  useEffect(() => {
    let live = true
    api.attemptArchives(orderId, itemId, attempt.id).then(
      (answer) => live && answered(answer),
      (err: unknown) => live && failed(err),
    )
    return () => {
      live = false
    }
  }, [orderId, itemId, attempt.id, asked])

  const waiting = read !== null && read.archives.some(due) && reads < ARCHIVE_READS
  useEffect(() => {
    if (!waiting) return
    const timer = setTimeout(() => {
      setReads((n) => n + 1)
      setAsked((n) => n + 1)
    }, ARCHIVE_READ_MS)
    return () => clearTimeout(timer)
  }, [waiting, reads])

  const again = () => {
    setReads(0)
    setAsked((n) => n + 1)
  }

  async function copy(archiveId: number, body: ArchiveCopy) {
    setBusy(true)
    setError(null)
    try {
      await api.copyArchiveMedia(orderId, itemId, attempt.id, archiveId, body)
      again()
    } catch (err) {
      setError(refusalText(err))
    } finally {
      setBusy(false)
    }
  }

  const label = `Attempt ${attempt.attempt}'s Bambuddy archives`
  if (!read) {
    return (
      <div className="flex w-full flex-wrap items-center gap-2 pl-3 text-xs" aria-label={label}>
        {error ? (
          <>
            <span role="alert" className="text-warn">
              {error}
            </span>
            <Button size="xs" variant="ghost" onClick={again}>
              Read again
            </Button>
          </>
        ) : (
          <span className="text-muted-foreground">Reading Bambuddy's archives…</span>
        )}
      </div>
    )
  }
  const completedLongAgo = (archive: ArchiveOffer) => archive.completed_at != null && readAt - Date.parse(archive.completed_at) >= TIMELAPSE_WAIT_MS
  return (
    <div className="flex w-full flex-col gap-1 pl-3 text-xs" aria-label={label}>
      {!read.linked && <span className="text-muted-foreground">Bambuddy is not linked.</span>}
      {read.linked && read.archives.length === 0 && <span className="text-muted-foreground">Bambuddy has not linked this print's archive yet.</span>}
      {read.archives.map((archive) => (
        <div key={archive.archive_id} className="flex flex-wrap items-center gap-x-3 gap-y-1">
          <span>
            Archive <span className="sb-num">{archive.archive_id}</span>
            {archive.status ? ` (${archive.status})` : ''}
          </span>
          <Medium
            attached={Boolean(archive.finished_attachment_id)}
            tooLarge={Boolean(archive.finished_too_large)}
            offered={(archive.finish_photos ?? []).length > 0}
            waiting={waiting}
            busy={busy}
            names={{
              attached: 'Finish photo attached',
              importing: 'Importing finish photo…',
              copy: 'Copy finish photo',
              tooLarge: 'Finish photo is over the upload limit; attach it by hand',
              none: 'no finish photo',
            }}
            onCopy={() => void copy(archive.archive_id, { what: 'finished_photo' })}
          />
          <Medium
            attached={Boolean(archive.timelapse_attachment_id)}
            tooLarge={Boolean(archive.timelapse_too_large)}
            offered={Boolean(archive.timelapse)}
            waiting={waiting}
            busy={busy}
            names={{
              attached: 'Timelapse attached',
              importing: 'Importing timelapse…',
              copy: 'Copy timelapse',
              tooLarge: 'Timelapse is over the upload limit; attach it by hand',
              // Spec §4.3: after an hour, "not in Bambuddy" covers both a video left on the
              // printer and one never recorded; the sweep still imports a late one.
              none: completedLongAgo(archive) ? 'no timelapse in Bambuddy for this print' : 'waiting for Bambuddy to process the timelapse',
            }}
            onCopy={() => void copy(archive.archive_id, { what: 'timelapse' })}
          />
        </div>
      ))}
      {error && (
        <span role="alert" className="text-warn">
          {error}
        </span>
      )}
    </div>
  )
}

/**
 * One medium of an archive: attached, refused as too large (#7281 review: the copy button
 * would be refused too, so none is offered), being imported, offered as a copy, or not there.
 */
function Medium({
  attached,
  tooLarge,
  offered,
  waiting,
  busy,
  names,
  onCopy,
}: {
  attached: boolean
  tooLarge: boolean
  offered: boolean
  waiting: boolean
  busy: boolean
  names: { attached: string; importing: string; copy: string; tooLarge: string; none: string }
  onCopy: () => void
}) {
  if (attached) {
    return (
      <span className="inline-flex items-center gap-1 text-primary">
        <CheckIcon className="size-3" aria-hidden="true" />
        {names.attached}
      </span>
    )
  }
  if (tooLarge) return <span className="text-warn">{names.tooLarge}</span>
  if (!offered) return <span className="text-muted-foreground">{names.none}</span>
  if (waiting) return <span className="text-muted-foreground">{names.importing}</span>
  return (
    <Button size="xs" variant="outline" disabled={busy} onClick={onCopy}>
      {names.copy}
    </Button>
  )
}
