import { useCallback, useEffect, useRef, useState } from 'react'
import { ApiError } from '../../../api/client'
import { claimNfcJob, listPendingNfcJobs, postNfcJobResult, type NfcJob } from '../../../api/filament'
import { headlineOf, writeWithBackup, type WriteOutcome, type WriteStep } from '../../../lib/tagWrite'
import { recordsSha256, type NfcSession, type WireRecord } from '../../../lib/webNfc'
import { Button } from '@/components/ui/button'
import { cn } from '@/lib/utils'

/** How often the page asks for pending jobs, while it is on screen (Global Constraints). */
const JOB_POLL_MS = 2_000

type Phase = 'arming' | 'tap' | WriteStep

const PHASE_TEXT: Record<Phase, string> = {
  arming: 'Arming…',
  tap: 'Hold a tag to the phone',
  backing_up: 'Backing up the tag…',
  writing: 'Writing…',
  reading_back: 'Hold the tag still to check it.',
  tap_again: 'Lift the tag and tap it again to check it.',
}

const CHANGED = 'The job changed. Nothing was written.'
const STOPPED_WAITING = 'The agent stopped waiting.'
/** The reason a run is stopped with at 0:00. */
const EXPIRED = 'expired'

const codeOf = (cause: unknown) => (cause instanceof ApiError ? cause.problem.code : undefined)
const detailOf = (cause: unknown) => (cause instanceof ApiError ? cause.detail : cause instanceof Error ? cause.message : String(cause))

/** `m:ss` of what is left. */
function clockOf(ms: number): string {
  const seconds = Math.ceil(ms / 1000)
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`
}

function bytesOf(hex: string): Uint8Array {
  return new Uint8Array(hex.match(/../g)?.map((pair) => parseInt(pair, 16)) ?? [])
}

function decoderOf(encoding: string | null | undefined): TextDecoder {
  try {
    return new TextDecoder(encoding ?? 'utf-8')
  } catch {
    return new TextDecoder('utf-8')
  }
}

const isJson = (record: WireRecord) => /^application\/([\w.-]+\+)?json$/i.test(record.media_type ?? '')

/** The text a person reads for a text or JSON record (JSON laid out); null for any other record. */
function textOf(record: WireRecord): string | null {
  if (record.record_type === 'text') return decoderOf(record.encoding).decode(bytesOf(record.data_hex))
  if (!isJson(record)) return null
  const text = new TextDecoder('utf-8').decode(bytesOf(record.data_hex))
  try {
    return JSON.stringify(JSON.parse(text), null, 2)
  } catch {
    return text
  }
}

/** The error a write job ends with when the tag does not hold what was sent (G4: the agent never waits out its timeout). */
function errorOf(outcome: Exclude<WriteOutcome, { status: 'checked' }>): string {
  if (outcome.status === 'mismatch') return 'the tag does not hold what was sent'
  if (outcome.status === 'other_tag') return 'a different tag answered'
  return outcome.message
}

/**
 * #7273 G4 — an agent's NFC job on the scan page. It asks for pending jobs every 2 s while the
 * page is on screen and shows the oldest: what it wants, every record it would write, and the
 * time left. "Arm" claims the job and runs it on the next tap (a write backs the tag up first,
 * `writeWithBackup`); "Decline" claims it and ends it with an error. While a job runs, it holds
 * the session's claim, so the page's own read handler gets no taps.
 */
export function NfcJobBanner({ session, onAnnounce }: { session: NfcSession; onAnnounce?: (text: string) => void }) {
  const [pending, setPending] = useState<NfcJob[]>([])
  const [gone, setGone] = useState<string[]>([])
  const [run, setRun] = useState<{ job: NfcJob; phase: Phase } | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [hash, setHash] = useState<{ id: string; value: string } | null>(null)
  const [now, setNow] = useState(() => Date.now())
  const stop = useRef<AbortController | null>(null)
  const announce = useRef(onAnnounce)
  useEffect(() => {
    announce.current = onAnnounce
  })

  const reload = useCallback(() => {
    listPendingNfcJobs().then(setPending, () => {})
  }, [])

  useEffect(() => {
    let timer: ReturnType<typeof setInterval> | null = null
    const visible = () => document.visibilityState === 'visible'
    const on = () => {
      reload()
      timer ??= setInterval(reload, JOB_POLL_MS)
    }
    const off = () => {
      if (timer !== null) clearInterval(timer)
      timer = null
    }
    const changed = () => (visible() ? on() : off())
    if (visible()) on()
    document.addEventListener('visibilitychange', changed)
    return () => {
      document.removeEventListener('visibilitychange', changed)
      off()
    }
  }, [reload])

  useEffect(() => () => stop.current?.abort(), [])

  const job = run?.job ?? pending.find((one) => !gone.includes(one.id)) ?? null
  const left = job ? Date.parse(job.expires_at) - now : 0
  const expired = job !== null && left <= 0

  const shownId = job?.id ?? null
  useEffect(() => {
    if (!shownId) return
    setNow(Date.now())
    const timer = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(timer)
  }, [shownId])

  // A write is armed only with the hash of the records shown here (R22): computed once they render.
  useEffect(() => {
    if (!job?.records || hash?.id === job.id) return
    let live = true
    void recordsSha256(job.records).then((value) => live && setHash({ id: job.id, value }))
    return () => {
      live = false
    }
  }, [job, hash])

  // At 0:00 a running job stops: the server has ended it.
  useEffect(() => {
    if (expired && run) stop.current?.abort(EXPIRED)
  }, [expired, run])

  const drop = (id: string) => setGone((now) => [...now, id])
  const end = (id: string, text: string) => {
    setRun(null)
    drop(id)
    setNotice(text)
    announce.current?.(text)
    reload()
  }

  /** Claims `job` for this page; on a refusal, says why and answers false. */
  async function claim(target: NfcJob, sha: string | null): Promise<boolean> {
    try {
      await claimNfcJob(target.id, sha ? { records_sha256: sha } : {})
      return true
    } catch (cause) {
      setRun(null)
      const code = codeOf(cause)
      if (code === 'records_mismatch') {
        setNotice(CHANGED)
        setHash(null)
        reload()
      } else if (code === 'job_not_pending') {
        drop(target.id)
        setNotice(STOPPED_WAITING)
      } else setNotice(`ScadBuddy could not arm the job: ${detailOf(cause)}.`)
      announce.current?.(code === 'records_mismatch' ? CHANGED : code === 'job_not_pending' ? STOPPED_WAITING : 'The job was not armed.')
      return false
    }
  }

  /** Ends an armed job with an error; a refusal (the job ended already) changes nothing here. */
  const fail = (id: string, error: string) => postNfcJobResult(id, { error: error.slice(0, 500) }).catch(() => null)

  async function arm(target: NfcJob, sha: string | null) {
    const controller = new AbortController()
    stop.current = controller
    const { signal } = controller
    setNotice(null)
    setRun({ job: target, phase: 'arming' })
    // One consumer at a time: a read job holds the session itself, a write through `writeWithBackup`.
    const release = session.claim()
    if (!release) {
      setRun(null)
      setNotice('Another write is running on this page. Arm the job once it ends.')
      return
    }
    try {
      try {
        await session.start()
      } catch (cause) {
        setRun(null)
        setNotice(`NFC could not start: ${detailOf(cause)}. Nothing was armed.`)
        return
      }
      if (!(await claim(target, sha))) return
      setRun({ job: target, phase: 'tap' })
      if (target.kind === 'read') {
        let reading
        try {
          reading = await session.nextReading(signal)
        } catch {
          if (signal.reason === EXPIRED) end(target.id, 'Expired. Nothing was read.')
          else {
            await fail(target.id, 'the page closed before a tag was read')
            end(target.id, 'Stopped. Nothing was read.')
          }
          return
        }
        try {
          await postNfcJobResult(target.id, { uid: reading.uid || null, records: reading.records })
          end(target.id, 'Agent job done: the tag was read.')
        } catch (cause) {
          end(target.id, codeOf(cause) === 'job_not_armed' ? STOPPED_WAITING : `ScadBuddy did not take the reading: ${detailOf(cause)}.`)
        }
        return
      }
    } finally {
      release()
    }

    const records = target.records ?? []
    const outcome = await writeWithBackup({
      session,
      reason: 'agent_write',
      jobId: target.id,
      prepare: async () => records,
      confirm: (_now, backupId, _sent, back) => postNfcJobResult(target.id, { uid: back.uid, records: back.records, backup_id: backupId }).then(() => null),
      onStep: (phase) => !signal.aborted && setRun({ job: target, phase }),
      signal,
    })
    if (outcome.status === 'checked') {
      end(target.id, `Agent job done: ${headlineOf(outcome, false)}`)
      return
    }
    if (signal.reason === EXPIRED) {
      end(target.id, 'Expired. Nothing was bound.')
      return
    }
    // A failed write ends the job at once, so the agent never waits out its time.
    await fail(target.id, errorOf(outcome))
    end(target.id, headlineOf(outcome, false))
  }

  async function decline(target: NfcJob, sha: string | null) {
    setNotice(null)
    setRun({ job: target, phase: 'arming' })
    if (!(await claim(target, sha))) return
    await fail(target.id, 'declined by the person')
    end(target.id, 'Declined. The agent was told.')
  }

  if (!job && !notice) return null
  const sha = job?.records ? (hash?.id === job.id ? hash.value : null) : null
  const ready = job !== null && !expired && !run && (job.kind === 'read' || sha !== null)
  const count = job?.records?.length ?? 0

  return (
    <section aria-label="Agent job" className="flex flex-col gap-3 rounded-xl border-2 border-primary bg-card p-4">
      {notice && (
        <div className="flex items-start justify-between gap-3">
          <p className="font-medium">{notice.replace(/^Agent job done: /, '')}</p>
          {!job && (
            <Button type="button" variant="ghost" size="sm" onClick={() => setNotice(null)}>
              Dismiss
            </Button>
          )}
        </div>
      )}
      {job && (
        <>
          <div className="flex items-baseline justify-between gap-3">
            <span className="font-semibold text-primary">Agent job</span>
            <span className={cn('tabular-nums', expired ? 'font-semibold text-warn' : 'text-muted-foreground')}>{expired ? 'Expired' : `Expires in ${clockOf(left)}`}</span>
          </div>
          <h2 className="text-base font-semibold tracking-tight">{job.kind === 'read' ? 'Agent wants to read a tag' : `Agent wants to write ${count} ${count === 1 ? 'record' : 'records'}`}</h2>
          {job.records && (
            <ul aria-label="Records to write" className="flex flex-col gap-2">
              {job.records.map((record, at) => {
                const text = textOf(record)
                return (
                  <li key={at} className="flex flex-col gap-1 rounded-lg bg-muted px-3 py-2">
                    <span className="flex flex-wrap items-baseline gap-x-2">
                      <span className="font-medium">{record.record_type}</span>
                      {record.media_type && <span className="text-muted-foreground">{record.media_type}</span>}
                      <span className="ml-auto tabular-nums text-muted-foreground">{record.data_hex.length / 2} bytes</span>
                    </span>
                    {text !== null && <pre className="max-h-32 overflow-auto font-mono text-[12px] leading-snug break-words whitespace-pre-wrap">{text}</pre>}
                  </li>
                )
              })}
            </ul>
          )}
          {run ? (
            <p className="text-base font-medium">{PHASE_TEXT[run.phase]}</p>
          ) : (
            <div className="grid grid-cols-2 gap-3">
              <Button type="button" className="h-11" disabled={!ready} onClick={() => void arm(job, sha)}>
                Arm
              </Button>
              <Button type="button" variant="outline" className="h-11" disabled={!ready} onClick={() => void decline(job, sha)}>
                Decline
              </Button>
            </div>
          )}
          <p className="text-muted-foreground">
            {job.kind === 'read' ? 'Nothing is read until you arm it.' : 'Nothing is written until you arm it. The tag is backed up first and never locked.'}
          </p>
        </>
      )}
    </section>
  )
}
