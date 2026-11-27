import { useEffect, useState } from 'react'
import { api } from '../../api/client'
import type { LocationDetail } from '../../api/filament'
import { writePlaceTag } from '../../lib/placeTag'
import type { WriteOutcome } from '../../lib/tagWrite'
import type { NfcSession } from '../../lib/webNfc'
import { labelBase, locationPayload, settingsLabelBase } from './labelSheet'
import { WriteFlow } from './scan/WriteTag'
import { Button } from '@/components/ui/button'

type Base = { state: 'reading' } | { state: 'failed' } | { state: 'read'; base: string }

/**
 * #7168 R6 — "Write tag" on a place: one tap writes the place label's URL to the tag, backed up
 * first and read back (`lib/placeTag.ts`). The label base is the labels page's: Settings'
 * label address, else an https public URL, else this page's origin. While Settings cannot be
 * read, nothing is written: the page's own origin may be the wrong host for the tag.
 */
export function WritePlaceTag({
  session,
  place,
  onSettled,
  onClose = () => {},
}: {
  session: NfcSession
  place: LocationDetail
  onSettled?: (outcome: WriteOutcome) => void
  onClose?: () => void
}) {
  const [base, setBase] = useState<Base>({ state: 'reading' })
  const [reads, setReads] = useState(0)

  useEffect(() => {
    let live = true
    setBase({ state: 'reading' })
    api.getSettings().then(
      (settings) => live && setBase({ state: 'read', base: labelBase(settingsLabelBase(settings)) }),
      () => live && setBase({ state: 'failed' }),
    )
    return () => {
      live = false
    }
  }, [reads])

  if (base.state === 'reading') return <p className="text-muted-foreground">Reading the label address…</p>
  if (base.state === 'failed') {
    return (
      <div className="flex flex-col gap-3">
        <p role="alert" className="text-warn">
          The label address in Settings could not be read, so nothing was written.
        </p>
        <div className="grid grid-cols-2 gap-3">
          <Button type="button" className="h-11" onClick={() => setReads((count) => count + 1)}>
            Try again
          </Button>
          <Button type="button" variant="outline" className="h-11" onClick={onClose}>
            Cancel
          </Button>
        </div>
      </div>
    )
  }

  const url = locationPayload(base.base, place.code)
  return (
    <div className="flex flex-col gap-2">
      <WriteFlow session={session} label="Place tag" run={(hooks) => writePlaceTag(session, place, base.base, hooks)} onSettled={onSettled} onClose={onClose} />
      <p className="px-1 text-muted-foreground">
        A tap on the tag opens <span className="font-medium text-foreground">{place.name}</span> at <span className="break-all font-mono text-[0.8125rem] text-foreground">{url}</span>
      </p>
    </div>
  )
}
