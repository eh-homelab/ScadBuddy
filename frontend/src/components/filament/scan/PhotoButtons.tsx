import { CameraIcon, CheckIcon, RotateCcwIcon } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { entityApi } from '../../entities/api'
import { Button } from '@/components/ui/button'

/** The photo roles (Global Constraints): entity attachment `role`s. */
export type PhotoRole = 'filament_label' | 'spool_label'
/** Photos taken before the spool exists, kept until it does. */
export type HeldPhotos = Partial<Record<PhotoRole, File>>

const ROLES: { role: PhotoRole; label: string }[] = [
  { role: 'filament_label', label: 'Filament label' },
  { role: 'spool_label', label: 'Spool label' },
]

type Status = 'held' | 'saving' | 'saved' | 'failed'
interface Slot {
  file: File
  status: Status
  /** The entities this photo is already saved on, so a retry sends only the rest. */
  done: string[]
}

export interface PhotoButtonsProps {
  /** The spool the photos go on; `null` holds them (a new tag), and they go once it is set. */
  spool: { id: string; type_id: string | null } | null
  /** The held photos the page keeps across screens, and where it hears of a change to them. */
  held?: HeldPhotos
  onHeld?: (held: HeldPhotos) => void
}

/**
 * #7273 (G6) — two camera buttons. A photo is an attachment on `spool:<id>` with its role,
 * sent as a file in a multipart form (never base64); a filament label also goes on the
 * spool's type, `spool_type:<type_id>`, when it has one.
 */
export function PhotoButtons({ spool, held, onHeld }: PhotoButtonsProps) {
  const [slots, setSlots] = useState<Partial<Record<PhotoRole, Slot>>>(() =>
    Object.fromEntries(Object.entries(held ?? {}).map(([role, file]) => [role, { file, status: 'held', done: [] }])),
  )
  const sending = useRef(new Set<PhotoRole>())
  const heldRef = useRef<HeldPhotos>(held ?? {})

  function setHeld(role: PhotoRole, file: File | null) {
    const next = { ...heldRef.current }
    if (file) next[role] = file
    else delete next[role]
    heldRef.current = next
    onHeld?.(next)
  }

  async function send(role: PhotoRole, slot: Slot, to: NonNullable<PhotoButtonsProps['spool']>) {
    if (sending.current.has(role)) return
    sending.current.add(role)
    setSlots((now) => ({ ...now, [role]: { ...slot, status: 'saving' } }))
    const targets = [`spool:${to.id}`, ...(role === 'filament_label' && to.type_id ? [`spool_type:${to.type_id}`] : [])]
    const done = [...slot.done]
    try {
      for (const entity of targets) {
        if (done.includes(entity)) continue
        await entityApi.addAttachment(entity, slot.file, { role })
        done.push(entity)
      }
      setSlots((now) => ({ ...now, [role]: { file: slot.file, status: 'saved', done } }))
      setHeld(role, null)
    } catch {
      setSlots((now) => ({ ...now, [role]: { file: slot.file, status: 'failed', done } }))
    } finally {
      sending.current.delete(role)
    }
  }

  function pick(role: PhotoRole, file: File | undefined) {
    if (!file) return
    const slot: Slot = { file, status: 'held', done: [] }
    if (spool) void send(role, slot, spool)
    else {
      setSlots((now) => ({ ...now, [role]: slot }))
      setHeld(role, file)
    }
  }

  // Held photos go as soon as there is a spool to put them on.
  useEffect(() => {
    if (!spool) return
    for (const { role } of ROLES) {
      const slot = slots[role]
      if (slot?.status === 'held') void send(role, slot, spool)
    }
    // `send` and `slots` change on every render; only a new spool starts the held uploads.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [spool?.id])

  return (
    <div className="grid grid-cols-2 gap-3">
      {ROLES.map(({ role, label }) => (
        <PhotoSlot
          key={role}
          label={label}
          slot={slots[role] ?? null}
          onPick={(file) => pick(role, file)}
          onRetry={() => {
            const slot = slots[role]
            if (slot && spool) void send(role, slot, spool)
          }}
        />
      ))}
    </div>
  )
}

function usePreview(file: File | null): string | null {
  const [url, setUrl] = useState<string | null>(null)
  useEffect(() => {
    if (!file) {
      setUrl(null)
      return
    }
    const made = URL.createObjectURL(file)
    setUrl(made)
    return () => URL.revokeObjectURL?.(made)
  }, [file])
  return url
}

function PhotoSlot({ label, slot, onPick, onRetry }: { label: string; slot: Slot | null; onPick: (file: File | undefined) => void; onRetry: () => void }) {
  const preview = usePreview(slot?.file ?? null)
  return (
    <fieldset className="flex min-w-0 flex-col gap-1.5">
      <legend className="mb-1.5 font-medium">{label}</legend>
      <label className="relative flex aspect-[4/3] cursor-pointer flex-col items-center justify-center gap-1 overflow-hidden rounded-lg border border-dashed border-border bg-muted/40 text-muted-foreground focus-within:ring-2 focus-within:ring-ring hover:bg-muted">
        {preview ? (
          <img src={preview} alt={`${label} photo`} className="absolute inset-0 size-full object-cover" />
        ) : (
          <>
            <CameraIcon className="size-6" aria-hidden="true" />
            <span>Take photo</span>
          </>
        )}
        <input
          type="file"
          accept="image/*"
          capture="environment"
          aria-label={label}
          className="sr-only"
          onChange={(event) => {
            onPick(event.target.files?.[0])
            event.target.value = ''
          }}
        />
      </label>
      <p aria-live="polite" className="min-h-5 text-muted-foreground">
        {slot?.status === 'held' && 'Kept until the spool is made'}
        {slot?.status === 'saving' && 'Saving…'}
        {slot?.status === 'saved' && (
          <span className="inline-flex items-center gap-1 text-foreground">
            <CheckIcon className="size-3.5" aria-hidden="true" />
            Saved
          </span>
        )}
      </p>
      {slot?.status === 'failed' && (
        <div className="flex flex-col items-start gap-1">
          <p role="alert" className="text-warn">
            Not saved. Try again.
          </p>
          <Button type="button" size="sm" variant="outline" onClick={onRetry}>
            <RotateCcwIcon /> Try again
          </Button>
        </div>
      )}
    </fieldset>
  )
}
