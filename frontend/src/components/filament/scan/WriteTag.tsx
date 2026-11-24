import { CheckIcon, XIcon } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { listTags, spoolTagRecords, spoolTagWritten, type Spool, type TagReadResult, type TagRecords, type TagRow } from '../../../api/filament'
import { headlineOf, restoreBackup, writeWithBackup, type WriteOutcome, type WriteStep } from '../../../lib/tagWrite'
import type { NfcSession, WireRecord } from '../../../lib/webNfc'
import { Button } from '@/components/ui/button'
import { cn } from '@/lib/utils'

const FORMAT_NAMES = { openspool: 'OpenSpool', opentag3d: 'OpenTag3D' } as const
const STEPS = ['Read and back up', 'Write', 'Read back'] as const

/** The hooks a run reports through: the step it is on, the records it is about to write, and when it holds the session. */
export interface RunHooks {
  onStep: (step: WriteStep) => void
  onRecords: (records: TagRecords) => void
  /** Pass to `writeWithBackup`: the flow says it runs only once its run holds the session's claim. */
  onClaimed: () => void
  signal: AbortSignal
}

/** Where the run is: 0 to 2 for the step under way, 3 when every step is done. */
function stepIndexOf(step: WriteStep | null, outcome: WriteOutcome | null): number {
  if (outcome?.status === 'checked') return 3
  if (step === 'writing') return 1
  if (step === 'reading_back' || step === 'tap_again') return 2
  return 0
}

/**
 * #7273 screens 5 and 6 — one write or restore run: the three steps, ticked as the run moves
 * on, then its outcome. It runs once on mount, and again on "Write again".
 */
export function WriteFlow({
  session,
  label,
  restoring = false,
  run,
  onSettled,
  onRunning,
  onRestore,
  onClose,
}: {
  session: NfcSession
  /** The section's name: "Write tag", "Restore a backup". */
  label: string
  restoring?: boolean
  run: (hooks: RunHooks) => Promise<WriteOutcome>
  onSettled?: (outcome: WriteOutcome) => void
  /** True while a run is under way, false once it settles or stops: the parent allows one run at a time. */
  onRunning?: (running: boolean) => void
  /** Offered after a mismatch: write that backup's records back. */
  onRestore?: (backupId: string) => void
  onClose: () => void
}) {
  const [attempt, setAttempt] = useState(0)
  const [step, setStep] = useState<WriteStep | null>(null)
  const [records, setRecords] = useState<TagRecords | null>(null)
  const [outcome, setOutcome] = useState<WriteOutcome | null>(null)
  /** The run waits for the first tap: set just before `run`, which starts listening at once. */
  const [ready, setReady] = useState(false)
  const latest = useRef({ run, onSettled, onRunning })
  useEffect(() => {
    latest.current = { run, onSettled, onRunning }
  })
  const stop = useRef<AbortController | null>(null)

  useEffect(() => {
    const controller = new AbortController()
    stop.current = controller
    const live = () => !controller.signal.aborted
    setStep(null)
    setRecords(null)
    setOutcome(null)
    setReady(false)
    // The parent hears of a run only while it holds the session's claim: a run another one
    // refused must not tell the parent that runs ended while the other still goes on.
    let held = false
    const release = () => {
      if (!held) return
      held = false
      latest.current.onRunning?.(false)
    }
    const detail = (cause: unknown) => (cause instanceof Error && cause.message ? cause.message : String(cause))
    void (async () => {
      let done: WriteOutcome
      try {
        await session.start()
        if (live()) setReady(true)
        try {
          done = await latest.current.run({
            signal: controller.signal,
            onStep: (next) => live() && setStep(next),
            onRecords: (next) => live() && setRecords(next),
            onClaimed: () => {
              if (!live()) return
              held = true
              latest.current.onRunning?.(true)
            },
          })
        } catch (cause) {
          // The run may have stopped after its write: only the tag can say what it holds.
          done = { status: 'failed', message: `The write stopped with an error. Read the tag to see what it holds. (${detail(cause)})`, backupId: null }
        }
      } catch (cause) {
        done = { status: 'failed', message: `NFC could not start: ${detail(cause)}. Nothing was written.`, backupId: null }
      }
      if (!live()) return
      setOutcome(done)
      release()
      latest.current.onSettled?.(done)
    })()
    return () => {
      controller.abort()
      release()
    }
  }, [session, attempt])

  const at = stepIndexOf(step, outcome)
  const failed = outcome !== null && outcome.status !== 'checked'

  function detailOf(index: number) {
    if (index === 0 && at === 0 && !outcome && step === null) return ready ? 'Hold the tag still. If asked, lift it and tap again.' : 'Starting NFC…'
    if (index === 1 && records && !restoring) {
      const format = records.format ? FORMAT_NAMES[records.format] : null
      const warnings = records.warnings ?? []
      return (
        <>
          {format && <span className="block">{at > 1 ? `Written as ${format}` : `Writing as ${format}`}</span>}
          {warnings.length > 0 && <span className="block">Missing on the spool: {warnings.join(', ')}</span>}
        </>
      )
    }
    if (index === 2 && at === 2 && step === 'tap_again' && !outcome) return 'Lift the tag and tap it again to check it.'
    return null
  }

  return (
    <section aria-label={label} className="flex flex-col gap-4 rounded-xl border border-border bg-card p-4">
      <h2 className="text-base font-semibold tracking-tight">{label}</h2>
      <ol aria-label="Steps" className="flex flex-col">
        {STEPS.map((name, index) => {
          const done = index < at
          const current = index === at && !outcome
          const broke = index === at && failed
          const detail = detailOf(index)
          return (
            <li key={name} aria-current={current ? 'step' : undefined} className="relative grid grid-cols-[1.75rem_1fr] gap-x-3 pb-4 last:pb-0">
              {index < STEPS.length - 1 && <span aria-hidden="true" className={cn('absolute top-7 bottom-0 left-[0.8125rem] w-px', done ? 'bg-ok' : 'bg-border')} />}
              <span
                aria-hidden="true"
                className={cn(
                  'relative flex size-7 items-center justify-center rounded-full border text-[12px] font-semibold tabular-nums',
                  done && 'border-ok bg-ok text-background',
                  current && 'border-primary text-primary ring-4 ring-primary/15 motion-safe:animate-pulse',
                  broke && 'border-warn bg-warn text-background',
                  !done && !current && !broke && 'border-border text-muted-foreground',
                )}
              >
                {done ? <CheckIcon className="size-4" /> : broke ? <XIcon className="size-4" /> : index + 1}
              </span>
              <div className="flex min-w-0 flex-col gap-0.5 pt-1">
                <span className={cn('font-medium', !done && !current && !broke && 'text-muted-foreground')}>
                  {name}
                  {done && <span className="sr-only"> Done</span>}
                  {broke && <span className="sr-only"> Stopped here</span>}
                </span>
                {detail && <span className="text-muted-foreground">{detail}</span>}
              </div>
            </li>
          )
        })}
      </ol>

      {outcome ? (
        <Outcome outcome={outcome} restoring={restoring} onRestore={onRestore} />
      ) : (
        <p className="text-muted-foreground">The tag's current contents are saved before anything is written. If the backup fails, nothing is written. Tags are never locked.</p>
      )}

      <div className="grid grid-cols-2 gap-3">
        {outcome ? (
          <>
            <Button type="button" className="h-11" onClick={onClose}>
              Done
            </Button>
            <Button type="button" variant="outline" className="h-11" onClick={() => setAttempt((count) => count + 1)}>
              {restoring ? 'Restore again' : 'Write again'}
            </Button>
          </>
        ) : (
          <Button
            type="button"
            variant="outline"
            className="col-span-2 h-11"
            onClick={() => {
              stop.current?.abort()
              onClose()
            }}
          >
            Cancel
          </Button>
        )}
      </div>
    </section>
  )
}

function Outcome({ outcome, restoring, onRestore }: { outcome: WriteOutcome; restoring: boolean; onRestore?: (backupId: string) => void }) {
  const ok = outcome.status === 'checked'
  const spoolNumber = outcome.status === 'checked' ? outcome.result?.spool?.number : undefined
  return (
    <div className={cn('flex flex-col gap-1 border-l-4 py-1 pl-3', ok ? 'border-ok' : 'border-warn')}>
      <p className="text-base font-semibold">{headlineOf(outcome, restoring)}</p>
      {ok && !restoring && spoolNumber != null && <p>The tag holds exactly the record that was sent. It is now bound to spool #{spoolNumber}.</p>}
      {ok && restoring && <p>The tag holds the backup's records again.</p>}
      {outcome.status === 'mismatch' && (
        <>
          <p>Nothing was bound. Its old contents are in the backup. The tag may have moved away too soon, or it may be too small for the record.</p>
          {onRestore && (
            <Button type="button" variant="outline" className="mt-2 h-11 self-start" onClick={() => onRestore(outcome.backupId)}>
              Restore from the backup
            </Button>
          )}
        </>
      )}
      {(outcome.status === 'other_tag' || (outcome.status === 'failed' && outcome.backupId)) && <p>The backup is kept.</p>}
    </div>
  )
}

/** `clean_tag_uid`: hex digits only, the last 16, upper case. */
const cleanUid = (uid: string) => uid.replace(/[^0-9a-fA-F]/g, '').toUpperCase().slice(-16)

/** The id of the tag with this UID: the opened tag's when it is that one, else from the tag list. */
async function tagIdFor(uid: string, opened: TagRow | null | undefined): Promise<string> {
  const clean = cleanUid(uid)
  if (opened?.tag_uid === clean) return opened.id
  const found = (await listTags({ includeUnbound: true })).find((one) => one.tag_uid === clean)
  if (!found) throw new Error('ScadBuddy has no tag with this serial number')
  return found.id
}

/**
 * #7273 screen 5 — "Write tag" on a spool: one tap reads, backs up, writes the spool's records
 * in the format the server picks, and reads back (G2, G7). A mismatch offers the backup back.
 */
export function WriteTag({
  session,
  spool,
  tag,
  onWritten,
  onAnnounce,
  onSettled,
  onRunning,
  onClose,
}: {
  session: NfcSession
  spool: Spool
  /** The tag the page read, when it is known: a restore after a mismatch on it needs no lookup. */
  tag?: TagRow | null
  /** A checked write: the UID as the phone read it, what the tag holds now, and the server's answer for the tag. */
  onWritten?: (uid: string, records: WireRecord[], result: TagReadResult) => void
  onAnnounce?: (text: string) => void
  onSettled?: (outcome: WriteOutcome) => void
  /** True while a write or restore run is under way. */
  onRunning?: (running: boolean) => void
  onClose: () => void
}) {
  const [restoring, setRestoring] = useState<{ backupId: string; tagId: string } | null>(null)
  const [lookup, setLookup] = useState<string | null>(null)
  const written = useRef<{ uid: string; records: WireRecord[] }>({ uid: '', records: [] })

  const settled = (outcome: WriteOutcome, restore: boolean) => {
    onSettled?.(outcome)
    if (outcome.status === 'checked' && outcome.result) {
      onWritten?.(written.current.uid, written.current.records, outcome.result)
      const number = outcome.result.spool?.number
      onAnnounce?.(number != null ? `Written and checked: spool #${number}` : 'Written and checked')
    } else onAnnounce?.(headlineOf(outcome, restore))
  }

  if (restoring) {
    return (
      <WriteFlow
        key={`restore-${restoring.backupId}`}
        session={session}
        label="Restore the tag"
        restoring
        run={({ onStep, onClaimed, signal }) => restoreBackup({ session, tagId: restoring.tagId, backupId: restoring.backupId, onStep, onClaimed, signal })}
        onSettled={(outcome) => settled(outcome, true)}
        onRunning={onRunning}
        onClose={onClose}
      />
    )
  }
  return (
    <>
      <WriteFlow
        key="write"
        session={session}
        label="Write tag"
        run={({ onStep, onRecords, onClaimed, signal }) =>
          writeWithBackup({
            session,
            reason: 'spool_write',
            onStep,
            onClaimed,
            signal,
            prepare: async (now, backupId) => {
              written.current = { uid: now.uid, records: now.records }
              const answer = await spoolTagRecords(spool.id, { uid: now.uid, records: now.records, backup_id: backupId })
              onRecords(answer)
              return answer.records
            },
            confirm: (now, backupId, sent, back) => {
              written.current = { uid: now.uid, records: back.records }
              return spoolTagWritten(spool.id, { uid: now.uid, backup_id: backupId, sent, read_back: back.records })
            },
          })
        }
        onSettled={(outcome) => settled(outcome, false)}
        onRunning={onRunning}
        onRestore={(backupId) => {
          setLookup(null)
          tagIdFor(written.current.uid, tag).then(
            (tagId) => setRestoring({ backupId, tagId }),
            (cause: unknown) => setLookup(`The backup was not restored: ${cause instanceof Error ? cause.message : String(cause)}.`),
          )
        }}
        onClose={onClose}
      />
      {lookup && (
        <p role="alert" className="text-warn">
          {lookup}
        </p>
      )}
    </>
  )
}
