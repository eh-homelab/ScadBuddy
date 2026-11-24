import { ExternalLinkIcon, NfcIcon } from 'lucide-react'
import { Link } from 'react-router'
import type { Spool } from '../../../api/filament'
import { filamentLinks } from '../../../lib/filamentLinks'
import { RgbaSwatch } from '../RgbaSwatch'
import { PhotoButtons, type HeldPhotos } from './PhotoButtons'
import { WeightField } from './WeightField'
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
 * "Open spool" and "Write tag" (Task 6 enables the write).
 */
export function KnownTag({
  spool,
  held,
  onHeld,
  onSpool,
}: {
  spool: Spool
  held?: HeldPhotos
  onHeld?: (held: HeldPhotos) => void
  /** The spool read again after a change on this screen (a weighing). */
  onSpool?: (spool: Spool) => void
}) {
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
        <Button type="button" variant="outline" className="h-11" disabled>
          <NfcIcon /> Write tag
        </Button>
      </div>
    </div>
  )
}
