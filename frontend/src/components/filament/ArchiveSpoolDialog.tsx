import { useId, useState } from 'react'
import { ApiError } from '../../api/client'
import { archiveSpool, type ArchiveReason, type Spool } from '../../api/filament'
import { DECIMAL } from './spoolWeight'
import { useClosingPreview } from './useClosingPreview'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { RadioGroup, RadioGroupItem } from '@/components/ui/radio-group'

const REASONS: ReadonlyArray<{ value: ArchiveReason; label: string }> = [
  { value: 'finished', label: 'Finished' },
  { value: 'respooled', label: 'Respooled' },
  { value: 'damaged', label: 'Damaged' },
  { value: 'other', label: 'Other' },
]

const grams = (value: number) => `${Number(value.toFixed(1))} g`
const message = (cause: unknown) => (cause instanceof ApiError ? cause.detail : cause instanceof Error ? cause.message : 'The server could not be reached. Try again.')

/**
 * #7684 — the archive dialog (spec 2026-11-25 §5.4): a reason (none chosen until the person
 * picks one), an optional final weighing (the scale reading with the spool when its empty
 * weight is known, else the filament left), the closing the archive would record and what
 * disagrees with the reason, read as the inputs settle (`useClosingPreview`). "Respooled"
 * also offers "Archive and continue as a new spool": `onArchived` then carries the final
 * filament as the new spool's grams. A refused archive is said in the dialog; nothing closes.
 * The form is mounted with the dialog's content, so each opening starts afresh.
 */
export function ArchiveSpoolDialog({
  spool,
  emptyWeightG,
  open,
  onOpenChange,
  onArchived,
}: {
  spool: Spool
  /** The page's own reading of the empty weight; the preview's `empty_g` wins once read. */
  emptyWeightG: number | null
  open: boolean
  onOpenChange: (open: boolean) => void
  onArchived: (spool: Spool, continueWithGrams: number | null) => void
}) {
  const [busy, setBusy] = useState(false)
  return (
    <Dialog open={open} onOpenChange={(next) => !busy && onOpenChange(next)}>
      <DialogContent className="sm:max-w-md">
        <ArchiveForm spool={spool} emptyWeightG={emptyWeightG} busy={busy} setBusy={setBusy} onOpenChange={onOpenChange} onArchived={onArchived} />
      </DialogContent>
    </Dialog>
  )
}

function ArchiveForm({
  spool,
  emptyWeightG,
  busy,
  setBusy,
  onOpenChange,
  onArchived,
}: {
  spool: Spool
  emptyWeightG: number | null
  busy: boolean
  setBusy: (busy: boolean) => void
  onOpenChange: (open: boolean) => void
  onArchived: (spool: Spool, continueWithGrams: number | null) => void
}) {
  const [reason, setReason] = useState<ArchiveReason | null>(null)
  const [reading, setReading] = useState('')
  // #7909: whether the field meant a scale reading when the number in it was started.
  const [typedWithEmpty, setTypedWithEmpty] = useState<boolean | null>(null)
  const [error, setError] = useState<string | null>(null)
  const fieldId = useId()
  const trimmed = reading.trim()
  const readingOk = trimmed === '' || DECIMAL.test(trimmed)
  const value = trimmed !== '' && readingOk ? Number(trimmed) : null
  // The preview resolves the spool's effective empty weight (its insert's, else its type's),
  // as the archive will; the page's figure stands in until the first preview answers.
  const [previewEmptyG, setPreviewEmptyG] = useState<number | null | undefined>(undefined)
  const emptyG = previewEmptyG === undefined ? emptyWeightG : previewEmptyG
  const withEmpty = emptyG != null
  const finalReadingG = withEmpty ? value : null
  const finalGrams = withEmpty ? null : value
  const { preview, pending } = useClosingPreview(spool.id, reason, finalReadingG, finalGrams)
  if (preview && preview.empty_g !== previewEmptyG) setPreviewEmptyG(preview.empty_g)
  const closing = preview?.closing ?? null
  // #7909: the preview's answer can change what the number in the field means under a number
  // already typed; say so rather than let the label change alone. A number typed after the
  // change was typed under the label it has now, so nothing is said for it.
  const meaningChanged = typedWithEmpty !== null && typedWithEmpty !== withEmpty && trimmed !== ''
  function changeReading(next: string) {
    setReading(next)
    if (next.trim() === '') setTypedWithEmpty(null)
    else if (trimmed === '') setTypedWithEmpty(withEmpty)
  }

  async function archive(andContinue: boolean) {
    if (!reason || !readingOk || busy) return
    setBusy(true)
    setError(null)
    try {
      const body = { archived: true, reason, ...(finalReadingG != null ? { final_reading_g: finalReadingG } : {}), ...(finalGrams != null ? { final_grams: finalGrams } : {}) }
      const archived = await archiveSpool(spool.id, body)
      onArchived(archived, andContinue ? (archived.grams_left ?? preview?.final_filament_g ?? null) : null)
    } catch (cause) {
      setError(message(cause))
    } finally {
      setBusy(false)
    }
  }

  return (
    <>
      <DialogHeader>
        <DialogTitle>Archive spool #{spool.number}</DialogTitle>
        <DialogDescription>Say why it goes, and weigh it one last time if you can: the closing compares what the scale found with what the ledger expected.</DialogDescription>
      </DialogHeader>
      <div className="flex flex-col gap-4">
        <RadioGroup aria-label="Reason" value={reason ?? ''} onValueChange={(next) => setReason(next as ArchiveReason)} className="gap-2">
          {REASONS.map((one) => (
            <div key={one.value} className="flex items-center gap-2">
              <RadioGroupItem value={one.value} id={`${fieldId}-${one.value}`} />
              <Label htmlFor={`${fieldId}-${one.value}`}>{one.label}</Label>
            </div>
          ))}
        </RadioGroup>
        <div className="flex flex-col gap-1">
          <Label htmlFor={`${fieldId}-reading`}>{withEmpty ? 'Scale reading, with the spool (g)' : 'Filament left (g)'}</Label>
          <Input id={`${fieldId}-reading`} inputMode="decimal" value={reading} onChange={(event) => changeReading(event.target.value)} aria-invalid={readingOk ? undefined : true} className="max-w-40" />
          {emptyG != null && <p className="text-[13px] text-muted-foreground">The empty spool weighs {grams(emptyG)}; it is taken off the reading.</p>}
          {meaningChanged && (
            <p role="status" className="text-[13px] text-warn">
              {withEmpty ? `The spool's empty weight turned out to be ${grams(emptyG)}: the number is read as the scale's reading, with the spool.` : 'No empty weight is known for this spool after all: the number is read as the filament left, not as a scale reading.'}
            </p>
          )}
        </div>
        <div className="min-h-5 text-[13px]" aria-live="polite">
          {closing && (
            <p className="text-muted-foreground">
              {closing.final_filament_g != null ? `Filament at the end: ${grams(closing.final_filament_g)}.` : 'Filament at the end: not known.'}
              {closing.ledger_consumed_g != null && ` The ledger used ${grams(closing.ledger_consumed_g)}`}
              {closing.ledger_consumed_g != null && closing.scale_consumed_g != null && closing.drift_g != null
                ? `; the scale says ${grams(closing.scale_consumed_g)} were used, ${grams(Math.abs(closing.drift_g))} ${closing.drift_g < 0 ? 'less' : 'more'} than the ledger expected.`
                : closing.ledger_consumed_g != null
                  ? '.'
                  : ''}
            </p>
          )}
          {preview?.notices.map((notice) => (
            <p key={notice.code} role="status" className="text-warn">
              {notice.text}
            </p>
          ))}
          {!closing && pending && <p className="text-muted-foreground">Working out the closing…</p>}
        </div>
        {error && (
          <p role="alert" className="text-[13px] text-warn">
            {error}
          </p>
        )}
      </div>
      <DialogFooter>
        <Button type="button" variant="outline" disabled={busy} onClick={() => onOpenChange(false)}>
          Cancel
        </Button>
        {reason === 'respooled' && (
          <Button type="button" variant="outline" disabled={busy || !readingOk} onClick={() => void archive(true)}>
            Archive and continue as a new spool
          </Button>
        )}
        <Button type="button" disabled={busy || !reason || !readingOk} onClick={() => void archive(false)}>
          Archive
        </Button>
      </DialogFooter>
    </>
  )
}
