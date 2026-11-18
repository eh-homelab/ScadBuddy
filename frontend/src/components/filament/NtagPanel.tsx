import { useEffect, useState } from 'react'
import { ApiError } from '../../api/client'
import { cancelNtag, confirmNtag, getRfidStatus, writeNtag, type SpoolDetail } from '../../api/filament'
import { useAsync } from '../../lib/useAsync'
import { useLatest } from '../../lib/useLatest'
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/components/ui/alert-dialog'
import { Button } from '@/components/ui/button'
import { Label } from '@/components/ui/label'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'

const message = (cause: unknown) => (cause instanceof ApiError ? cause.detail : 'The server could not be reached. Try again.')

/** After a write, the check for the tag runs every 2 s for 60 s. */
const POLL_MS = 2000
const POLLS = 30

/** The reader a person picked last, kept in this browser. */
const DEVICE_KEY = 'scadbuddy.filament.ntagDevice'

function rememberedDevice(): string | null {
  try {
    return window.localStorage.getItem(DEVICE_KEY)
  } catch {
    return null
  }
}

function rememberDevice(id: string) {
  try {
    window.localStorage.setItem(DEVICE_KEY, id)
  } catch {
    // A browser that refuses storage still uses the choice for this visit.
  }
}

type Phase = 'idle' | 'waiting' | 'bound' | 'missed'

interface Props {
  spool: SpoolDetail
  /** The tag Bambuddy wrote is now the spool's: the page reads the spool again. */
  onBound?: () => void
}

/**
 * #6264 — writes an OpenTag3D NTAG for a spool through a SpoolBuddy reader. "Write NTAG"
 * queues the write for the spool's Bambuddy record and binds nothing; the panel then asks the
 * server every 2 s for 60 s whether Bambuddy's record now holds the written tag (`confirm`
 * binds it). A spool whose open tag was bound with `override` is replaced only after a
 * confirm; a spool with no Bambuddy record shows the server's reason and polls nothing.
 */
export function NtagPanel({ spool, onBound }: Props) {
  const status = useAsync(() => getRfidStatus(), [])
  const readers = (status.data?.devices ?? []).filter((device) => device.has_nfc)
  const [picked, setPicked] = useState<string | null>(rememberedDevice)
  const deviceId = readers.find((device) => device.device_id === picked)?.device_id ?? readers[0]?.device_id ?? null

  const [phase, setPhase] = useState<Phase>('idle')
  const [warnings, setWarnings] = useState<string[]>([])
  const [error, setError] = useState<string | null>(null)
  const [replacing, setReplacing] = useState(false)
  const [busy, setBusy] = useState(false)
  const bound = useLatest(onBound)

  useEffect(() => {
    if (phase !== 'waiting') return
    let stopped = false
    let timer: ReturnType<typeof setTimeout>
    let polls = 0
    const next = () => {
      timer = setTimeout(async () => {
        polls++
        try {
          const answer = await confirmNtag(spool.id)
          if (stopped) return
          if (answer.bound) {
            setPhase('bound')
            bound.current?.()
            return
          }
        } catch (cause) {
          if (stopped) return
          setError(message(cause))
          setPhase('idle')
          return
        }
        if (polls >= POLLS) setPhase('missed')
        else next()
      }, POLL_MS)
    }
    next()
    return () => {
      stopped = true
      clearTimeout(timer)
    }
  }, [phase, spool.id, bound])

  async function write(replace: boolean) {
    if (!deviceId) return
    setBusy(true)
    setError(null)
    setWarnings([])
    try {
      const queued = await writeNtag(spool.id, deviceId, replace)
      setWarnings(queued.warnings ?? [])
      setPhase('waiting')
    } catch (cause) {
      if (cause instanceof ApiError && cause.status === 409 && cause.problem.code === 'override_tag_open') setReplacing(true)
      else setError(message(cause))
    } finally {
      setBusy(false)
    }
  }

  async function cancel() {
    if (!deviceId) return
    setPhase('idle')
    try {
      await cancelNtag(spool.id, deviceId)
    } catch (cause) {
      setError(message(cause))
    }
  }

  return (
    <div className="flex flex-col gap-2" data-testid="ntag-panel">
      <div className="flex flex-wrap items-end gap-2">
        <div className="flex min-w-48 flex-col gap-1">
          <Label className="text-[13px]">Reader</Label>
          <Select
            value={deviceId ?? ''}
            onValueChange={(next) => {
              setPicked(next)
              rememberDevice(next)
            }}
            disabled={readers.length === 0 || phase === 'waiting'}
          >
            <SelectTrigger aria-label="Reader" className="w-full">
              <SelectValue placeholder={status.loading ? 'Looking for readers' : 'No reader with NFC'} />
            </SelectTrigger>
            <SelectContent>
              {readers.map((device) => (
                <SelectItem key={device.device_id} value={device.device_id}>
                  {device.hostname}
                  {device.online ? '' : ' (offline)'}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
        <Button type="button" size="sm" variant="outline" disabled={!deviceId || busy || phase === 'waiting'} onClick={() => void write(false)}>
          Write NTAG
        </Button>
        {phase === 'waiting' && (
          <Button type="button" size="sm" variant="ghost" onClick={() => void cancel()}>
            Cancel
          </Button>
        )}
      </div>

      {phase === 'waiting' && (
        <p role="status" className="text-[13px] text-muted-foreground">
          Hold a blank tag to the reader
        </p>
      )}
      {phase === 'bound' && (
        <p role="status" className="text-[13px]">
          Written and bound
        </p>
      )}
      {phase === 'missed' && (
        <p role="status" className="text-[13px] text-muted-foreground">
          Not seen yet
        </p>
      )}
      {warnings.length > 0 && (
        <ul className="list-disc pl-5 text-[13px] text-warn">
          {warnings.map((warning) => (
            <li key={warning}>{warning}</li>
          ))}
        </ul>
      )}
      {error && (
        <p role="alert" className="text-[13px] text-warn">
          {error}
        </p>
      )}

      <AlertDialog open={replacing} onOpenChange={setReplacing}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Replace the Bambu tag?</AlertDialogTitle>
            <AlertDialogDescription>This replaces the Bambu tag the AMS reads. Replace it?</AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Keep it</AlertDialogCancel>
            <AlertDialogAction onClick={() => void write(true)}>Replace</AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  )
}
