import { ExternalLinkIcon, NfcIcon } from 'lucide-react'
import { useState } from 'react'
import { Link } from 'react-router'
import type { Spool, TagReadResult, TagRow } from '../../../api/filament'
import { filamentLinks } from '../../../lib/filamentLinks'
import type { NfcSession, WireRecord } from '../../../lib/webNfc'
import { RgbaSwatch } from '../RgbaSwatch'
import { PhotoButtons, type HeldPhotos } from './PhotoButtons'
import { TagBackups } from './TagBackups'
import { WeightField } from './WeightField'
import { WriteTag } from './WriteTag'
import { Button } from '@/components/ui/button'

/** A spool's name line: its material and subtype. */
const materialOf = (spool: Spool) => [spool.names?.material, spool.names?.subtype].filter(Boolean).join(' ')

/** A spool's card on the scan page: what the person checks against the spool in their hand. */
export function SpoolCard({ spool }: { spool: Spool }) {
  const name = `Spool #${spool.number}`
  const rgba = spool.effective?.rgba ?? spool.rgba
  const colour = spool.effective?.colour_name ?? spool.colour_name
  const material = materialOf(spool)
  return (
    <section aria-label={name} className="flex items-center gap-4 rounded-xl border border-border bg-card p-4">
      {rgba && <RgbaSwatch rgba={rgba} size="lg" gramsLeft={spool.grams_left} labelWeight={spool.effective?.label_weight_g} />}
      <div className="flex min-w-0 flex-col gap-0.5">
        <h2 className="text-lg font-semibold tracking-tight tabular-nums">{name}</h2>
        {material && <p className="break-words">{material}</p>}
        {(spool.names?.brand || colour) && <p className="break-words text-muted-foreground">{[spool.names?.brand, colour].filter(Boolean).join(', ')}</p>}
        {spool.grams_left != null && <p className="tabular-nums">{Math.round(spool.grams_left)} g left</p>}
      </div>
    </section>
  )
}

/**
 * #7273 screen 2 — a tag a spool holds: its card, the two label photos, the weight, then
 * "Open spool" and "Write tag" (screen 5, with the page's scan session), and the tag's backups.
 */
export function KnownTag({
  spool,
  tag,
  session,
  held,
  onHeld,
  onSpool,
  onWritten,
  onAnnounce,
}: {
  spool: Spool
  /** The tag the page read. */
  tag?: TagRow | null
  /** The page's scan session; without one there is no NFC to write with. */
  session?: NfcSession
  held?: HeldPhotos
  onHeld?: (held: HeldPhotos) => void
  /** The spool read again after a change on this screen (a weighing). */
  onSpool?: (spool: Spool) => void
  /** A checked write: the UID as the phone read it, what the tag holds now, and the server's answer for that tag. */
  onWritten?: (uid: string, records: WireRecord[], result: TagReadResult) => void
  /** A sentence for the page's live region. */
  onAnnounce?: (text: string) => void
}) {
  const [writing, setWriting] = useState(false)
  /** Counts the writes run here, so the backups are read again. */
  const [writes, setWrites] = useState(0)
  return (
    <div className="flex flex-col gap-5">
      <SpoolCard spool={spool} />
      <PhotoButtons spool={{ id: spool.id, type_id: spool.type_id }} held={held} onHeld={onHeld} />
      <WeightField key={spool.id} spool={spool} onSaved={onSpool} />
      <div className="grid grid-cols-2 gap-3">
        <Button asChild variant="outline" className="h-11">
          <Link to={filamentLinks.spool(spool.id)}>
            <ExternalLinkIcon /> Open spool
          </Link>
        </Button>
        <Button type="button" variant="outline" className="h-11" disabled={!session || writing} onClick={() => setWriting(true)}>
          <NfcIcon /> Write tag
        </Button>
      </div>
      {session && writing && (
        <WriteTag
          session={session}
          spool={spool}
          tag={tag}
          onWritten={onWritten}
          onAnnounce={onAnnounce}
          onSettled={() => setWrites((count) => count + 1)}
          onClose={() => setWriting(false)}
        />
      )}
      {session && tag && <TagBackups session={session} tagId={tag.id} refresh={writes} onAnnounce={onAnnounce} />}
    </div>
  )
}
