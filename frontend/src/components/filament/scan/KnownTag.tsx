import { EraserIcon, ExternalLinkIcon, NfcIcon } from 'lucide-react'
import { useState } from 'react'
import { Link } from 'react-router'
import type { Spool, TagReadResult, TagRow } from '../../../api/filament'
import { filamentLinks } from '../../../lib/filamentLinks'
import { clearable } from '../../../lib/tagWrite'
import type { NfcSession, TagInfo, WireRecord } from '../../../lib/webNfc'
import { RgbaSwatch } from '../RgbaSwatch'
import { PhotoButtons, type HeldPhotos } from './PhotoButtons'
import { TagBackups } from './TagBackups'
import { WeightField } from './WeightField'
import { ClearTag, WriteTag } from './WriteTag'
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
 * #8362 — the tag's chip and how much it holds, as the ScadBuddy app reads them ("NTAG215 ·
 * 504 bytes"); nothing in a browser, whose Web NFC tells neither. OpenSpool needs an NTAG215 or
 * NTAG216 (https://openspool.io/rfid.html, "Tag Selection"), so an NTAG213 says so.
 */
export function TagChipLine({ info }: { info?: TagInfo }) {
  if (!info || info.tech === 'mifare_classic') return null
  const bytes = info.user_bytes ?? info.max_ndef_bytes
  const parts = [info.chip, bytes != null ? `${bytes} bytes` : null].filter(Boolean)
  if (parts.length === 0 && info.writable !== false) return null
  return (
    <div className="flex flex-col gap-0.5">
      <p aria-label="Tag chip" className="tabular-nums text-muted-foreground">
        {parts.join(' · ')}
        {info.writable === false && <span className="text-warn">{parts.length > 0 ? ', read-only' : 'Read-only'}</span>}
      </p>
      {info.chip === 'NTAG213' && <p className="text-warn">OpenSpool does not support an NTAG213: use an NTAG215 or NTAG216 for an OpenSpool record.</p>}
    </div>
  )
}

/**
 * #7273 screen 2 — a tag a spool holds: its card, the two label photos, the weight, then
 * "Open spool" and "Write tag" (screen 5, with the page's scan session), and the tag's backups.
 */
export function KnownTag({
  spool,
  tag,
  tagInfo,
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
  /** What the ScadBuddy app read of the tag's chip (#8362). */
  tagInfo?: TagInfo
  /** The page's scan session; without one there is no NFC to write with. */
  session?: NfcSession
  held?: HeldPhotos
  onHeld?: (held: HeldPhotos) => void
  /** The spool read again after a change on this screen (a weighing). */
  onSpool?: (spool: Spool) => void
  /** A checked write or clear: the UID as the phone read it, what the tag holds now, and the server's answer for that tag. */
  onWritten?: (uid: string, records: WireRecord[], result: TagReadResult) => void
  /** A sentence for the page's live region. */
  onAnnounce?: (text: string) => void
}) {
  const [writing, setWriting] = useState(false)
  const [clearing, setClearing] = useState(false)
  /** Counts the writes run here, so the backups are read again. */
  const [writes, setWrites] = useState(0)
  /** A write or a restore is under way: one at a time, so every write and restore button waits. */
  const [running, setRunning] = useState(false)
  return (
    <div className="flex flex-col gap-5">
      <SpoolCard spool={spool} />
      <TagChipLine info={tagInfo} />
      <PhotoButtons spool={{ id: spool.id, type_id: spool.type_id }} held={held} onHeld={onHeld} />
      <WeightField key={spool.id} spool={spool} onSaved={onSpool} />
      <div className="grid grid-cols-2 gap-3">
        <Button asChild variant="outline" className="h-11">
          <Link to={filamentLinks.spool(spool.id)}>
            <ExternalLinkIcon /> Open spool
          </Link>
        </Button>
        <Button type="button" variant="outline" className="h-11" disabled={!session || writing || clearing || running} onClick={() => setWriting(true)}>
          <NfcIcon /> Write tag
        </Button>
        {tag && clearable(tag) && (
          <Button type="button" variant="ghost" className="col-span-2 h-11 text-muted-foreground" disabled={!session || writing || clearing || running} onClick={() => setClearing(true)}>
            <EraserIcon /> Clear tag
          </Button>
        )}
      </div>
      {session && writing && (
        <WriteTag
          session={session}
          spool={spool}
          tag={tag}
          onWritten={onWritten}
          onAnnounce={onAnnounce}
          onSettled={() => setWrites((count) => count + 1)}
          onRunning={setRunning}
          onClose={() => setWriting(false)}
        />
      )}
      {session && tag && clearing && (
        <ClearTag
          session={session}
          tag={tag}
          onCleared={onWritten}
          onAnnounce={onAnnounce}
          onSettled={() => setWrites((count) => count + 1)}
          onRunning={setRunning}
          onClose={() => setClearing(false)}
        />
      )}
      {session && tag && <TagBackups session={session} tagId={tag.id} refresh={writes} busy={running} onRunning={setRunning} onAnnounce={onAnnounce} />}
    </div>
  )
}
