import { useState } from 'react'
import { api } from '../../api/client'
import type { ArchiveCopy, AttemptArchives, Fulfilment } from '../../api/types'
import { Button } from '@/components/ui/button'
import { refusalText } from './refusal'

/**
 * #4253 (spec 2026-10-18 §8.2) — a printed attempt's Bambuddy archives, read on request,
 * each offering its finish photo and its timelapse as a one-click copy onto the attempt (listed among the item's
 * files). Reading them also copies what they say (times, grams) onto the attempt, so the
 * page is told to reload once they are read.
 */
export function ArchiveCopies({ orderId, itemId, attempt, onChanged }: { orderId: string; itemId: string; attempt: Fulfilment; onChanged: () => void }) {
  const [read, setRead] = useState<AttemptArchives | null>(null)
  const [busy, setBusy] = useState(false)
  const [copied, setCopied] = useState<Set<string>>(new Set())
  const [error, setError] = useState<string | null>(null)

  async function run(action: () => Promise<void>) {
    setBusy(true)
    setError(null)
    try {
      await action()
    } catch (err) {
      setError(refusalText(err))
    } finally {
      setBusy(false)
    }
  }

  const load = () =>
    run(async () => {
      const answer = await api.attemptArchives(orderId, itemId, attempt.id)
      setRead(answer)
      onChanged()
    })

  const copy = (archiveId: number, body: ArchiveCopy) =>
    run(async () => {
      await api.copyArchiveMedia(orderId, itemId, attempt.id, archiveId, body)
      setCopied((done) => new Set(done).add(`${archiveId}:${body.what}`))
      onChanged()
    })

  if (!read) {
    return (
      <span className="inline-flex flex-wrap items-center gap-2">
        <Button size="xs" variant="ghost" disabled={busy} onClick={() => void load()}>
          From Bambuddy…
        </Button>
        {error && (
          <span role="alert" className="text-xs text-warn">
            {error}
          </span>
        )}
      </span>
    )
  }
  return (
    <div className="flex w-full flex-col gap-1 pl-3 text-xs" aria-label={`Attempt ${attempt.attempt}'s Bambuddy archives`}>
      {!read.linked && <span className="text-muted-foreground">Bambuddy is not linked.</span>}
      {read.linked && read.archives.length === 0 && <span className="text-muted-foreground">Bambuddy has not linked this print's archive yet.</span>}
      {read.archives.map((archive) => {
        const photo = copied.has(`${archive.archive_id}:finished_photo`)
        const video = copied.has(`${archive.archive_id}:timelapse`)
        return (
          <div key={archive.archive_id} className="flex flex-wrap items-center gap-2">
            <span>
              Archive <span className="sb-num">{archive.archive_id}</span>
              {archive.status ? ` (${archive.status})` : ''}
            </span>
            {(archive.finish_photos ?? []).length > 0 ? (
              <Button size="xs" variant="outline" disabled={busy || photo} onClick={() => void copy(archive.archive_id, { what: 'finished_photo' })}>
                {photo ? 'Finish photo copied' : 'Copy finish photo'}
              </Button>
            ) : (
              <span className="text-muted-foreground">no finish photo</span>
            )}
            {archive.timelapse ? (
              <Button size="xs" variant="outline" disabled={busy || video} onClick={() => void copy(archive.archive_id, { what: 'timelapse' })}>
                {video ? 'Timelapse copied' : 'Copy timelapse'}
              </Button>
            ) : (
              <span className="text-muted-foreground">no timelapse (pull it from the print's page first)</span>
            )}
          </div>
        )
      })}
      {error && (
        <span role="alert" className="text-warn">
          {error}
        </span>
      )}
    </div>
  )
}
