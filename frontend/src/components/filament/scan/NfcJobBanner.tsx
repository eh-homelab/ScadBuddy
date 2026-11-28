import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { ApiError } from '../../../api/client'
import { cancelNfcJob, claimNfcJob, getNfcJob, listPendingNfcJobs, postNfcJobResult, type NfcJob } from '../../../api/filament'
import { headlineOf, writeWithBackup, type WriteOutcome, type WriteStep } from '../../../lib/tagWrite'
import { decodeText, notWritable, posterRecords, recordsSha256, visibleText, type NfcSession, type PosterRecord, type WireRecord } from '../../../lib/webNfc'
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

/** What Cancel job says for the state the cancel answered: a job that had ended another way is not "cancelled". */
const CANCEL_TEXT: Partial<Record<NfcJob['state'], string>> = {
  cancelled: 'Cancelled. The agent was told.',
  done: 'The job had already ended: the agent has its result.',
  failed: 'The job had already ended: the agent was told it failed.',
  expired: 'The job had already expired. Nothing was cancelled.',
}
const CHANGED = 'The job changed. Nothing was written.'
const STOPPED_WAITING = 'The agent stopped waiting.'
/** The reason a run is stopped with at 0:00. */
const EXPIRED = 'expired'
/** The reason a run is stopped with when the server ended the job another way (R23: the agent cancelled it). */
const ENDED = 'ended'
/** The reason a run is stopped with when the server ended a begun write `no_read_back` (no read-back within 10 minutes of its backup). */
const NO_READ_BACK = 'no_read_back'
const NO_READ_BACK_TEXT = 'No read-back came in time. The tag may hold the new records; its backup can restore it.'
/**
 * The steps from the backup on: the write has begun (or its backup may already be kept), so 0:00 stops
 * nothing and the server takes the result whenever it comes (#7419 review).
 */
const WRITE_BEGUN: ReadonlySet<Phase> = new Set(['backing_up', 'writing', 'reading_back', 'tap_again'])
/** How many bytes of a record the page shows as hex; it says how many more there are. */
const HEX_PREVIEW_BYTES = 64

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

const isJson = (record: WireRecord) => /^application\/([\w.-]+\+)?json$/i.test(record.media_type ?? '')

/** The encodings an NDEF text record can use (NFC Forum Text RTD: UTF-8 or UTF-16), by their Web NFC names. */
const TEXT_ENCODINGS = new Set(['utf-8', 'utf-16', 'utf-16le', 'utf-16be'])

/** Up to `HEX_PREVIEW_BYTES` of `bytes` as spaced hex, and how many more there are. */
function hexPreview(bytes: Uint8Array): { text: string; more: number } {
  const shown = bytes.subarray(0, HEX_PREVIEW_BYTES)
  return { text: Array.from(shown, (byte) => byte.toString(16).padStart(2, '0')).join(' '), more: bytes.length - shown.length }
}

/** What a person reads for one record: its text or URL, each record a smart poster holds, or its first bytes as hex and how many more there are. */
type Preview = { kind: 'text'; text: string } | { kind: 'hex'; text: string; more: number } | { kind: 'poster'; records: PosterRecord[] }

/**
 * R22: every record is shown before Arm. It is text only where the text is exactly what was sent: a
 * text record in an NDEF encoding, a URL (as text, never a link), JSON as sent (laid out only when
 * that changes no value). A smart poster shows each record it holds. Any other record, or bytes
 * that are not valid text, is shown as hex; a record with no bytes says so. Text shows bidi, control
 * and zero-width characters as escapes (`visibleText`), so what is approved reads as a reader shows it.
 */
function previewOf(record: WireRecord): Preview {
  const bytes = bytesOf(record.data_hex)
  if (bytes.length === 0) return { kind: 'hex', text: '', more: 0 }
  let text: string | null = null
  if (record.record_type === 'text') {
    const encoding = (record.encoding ?? 'utf-8').toLowerCase()
    if (TEXT_ENCODINGS.has(encoding)) text = decodeText(bytes, encoding)
  } else if (record.record_type === 'url' || record.record_type === 'absolute-url') text = decodeText(bytes, 'utf-8')
  else if (record.record_type === 'smart-poster') {
    const records = posterRecords(record.data_hex)
    if (records) return { kind: 'poster', records }
  } else if (isJson(record)) {
    text = decodeText(bytes, 'utf-8')
    if (text !== null) {
      try {
        const parsed: unknown = JSON.parse(text)
        if (JSON.stringify(parsed) === text.trim()) text = JSON.stringify(parsed, null, 2)
      } catch {
        // Not JSON after all: it is shown as sent.
      }
    }
  }
  if (text !== null) return { kind: 'text', text: visibleText(text) }
  return { kind: 'hex', ...hexPreview(bytes) }
}

/** The error a write job ends with when it did not write what was sent (G4: the agent never waits out its timeout). */
function errorOf(outcome: Exclude<WriteOutcome, { status: 'checked' | 'mismatch' }>): string {
  if (outcome.status === 'other_tag') return 'a different tag answered'
  return outcome.message
}

/** A preview's hex: its bytes, how many more there are, or that it has none. */
function HexText({ text, more }: { text: string; more: number }) {
  return (
    <p className="font-mono text-[12px] leading-snug break-all text-muted-foreground">
      {text || <span className="font-sans">No data</span>}
      {more > 0 && <span className="font-sans"> and {more} more bytes</span>}
    </p>
  )
}

/** Runs `tick` at once and every 2 s while the page is on screen, and again when it comes back on screen; none while it is hidden. */
function useWhileVisible(tick: (() => void) | null) {
  useEffect(() => {
    if (!tick) return
    let timer: ReturnType<typeof setInterval> | null = null
    const visible = () => document.visibilityState === 'visible'
    const on = () => {
      tick()
      timer ??= setInterval(tick, JOB_POLL_MS)
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
  }, [tick])
}

/** At most `limit` characters of `text`, cut between code points (the backend counts code points). */
const clip = (text: string, limit: number) => Array.from(text).slice(0, limit).join('')

/**
 * #7273 G4 — an agent's NFC job on the scan page. It asks for pending jobs every 2 s while the
 * page is on screen and shows the oldest: what it wants, every record it would write, and the
 * time left. "Arm" claims the job and runs it on the next tap (a write backs the tag up first,
 * `writeWithBackup`); "Decline" claims it and ends it with an error; "Cancel job" ends it as
 * cancelled without claiming it, so it works on a job Arm refuses too. While a job runs, it holds
 * the session's claim, so the page's own read handler gets no taps.
 */
export function NfcJobBanner({ session, onAnnounce }: { session: NfcSession; onAnnounce?: (text: string) => void }) {
  const [pending, setPending] = useState<NfcJob[]>([])
  const [gone, setGone] = useState<string[]>([])
  const [run, setRun] = useState<{ job: NfcJob; phase: Phase } | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [hash, setHash] = useState<{ key: string; value: string } | null>(null)
  const [now, setNow] = useState(() => Date.now())
  /** The job whose cancel waits for its answer: only that job's buttons are held. */
  const [cancelling, setCancelling] = useState<string | null>(null)
  /** The running job's id and the controller that stops its run. */
  const stop = useRef<{ id: string; controller: AbortController } | null>(null)
  const announce = useRef(onAnnounce)
  useEffect(() => {
    announce.current = onAnnounce
  })

  const reload = useCallback(() => {
    listPendingNfcJobs().then(setPending, () => {})
  }, [])

  useWhileVisible(reload)

  // R23: the agent can cancel an armed job. While one runs, the page asks for its state at the same
  // cadence; once it is not armed, the run stops, posts nothing and frees the session.
  const armedId = run && run.phase !== 'arming' ? run.job.id : null
  const watch = useMemo(() => {
    if (!armedId) return null
    return () => {
      const running = stop.current
      if (running?.id !== armedId) return
      getNfcJob(armedId).then(
        (now) => {
          if (now.state !== 'armed') running.controller.abort(now.state === 'expired' ? EXPIRED : now.result?.code === NO_READ_BACK ? NO_READ_BACK : ENDED)
          // The page follows the job's current `expires_at`: arming moved it.
          else setRun((shown) => (shown?.job.id === now.id ? { ...shown, job: now } : shown))
        },
        () => {},
      )
    }
  }, [armedId])
  useWhileVisible(watch)

  useEffect(() => () => stop.current?.controller.abort(), [])

  const job = run?.job ?? pending.find((one) => !gone.includes(one.id)) ?? null
  const left = job ? Date.parse(job.expires_at) - now : 0
  // Once the write has begun, 0:00 ends nothing: the server waits for its result.
  const begun = run !== null && WRITE_BEGUN.has(run.phase)
  const expired = job !== null && left <= 0 && !begun

  const shownId = job?.id ?? null
  /** The job on screen, for an answer that comes after the banner moved on. */
  const shown = useRef<string | null>(null)
  useEffect(() => {
    shown.current = shownId
  })
  useEffect(() => {
    if (!shownId) return
    setNow(Date.now())
    const timer = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(timer)
  }, [shownId])

  // A write is armed only with the hash of the records shown here (R22): computed once they render,
  // and again whenever the records differ (a reload after `records_mismatch`). It hangs on the
  // records' text, not the job: each poll answers a new job object, which must not start it again.
  const recordsKey = job?.records ? JSON.stringify(job.records) : null
  useEffect(() => {
    if (recordsKey === null || hash?.key === recordsKey) return
    let live = true
    void recordsSha256(JSON.parse(recordsKey) as WireRecord[]).then((value) => live && setHash({ key: recordsKey, value }))
    return () => {
      live = false
    }
  }, [recordsKey, hash])

  // At 0:00 a running job stops before its write begins: the server has ended it.
  useEffect(() => {
    if (expired && run) stop.current?.controller.abort(EXPIRED)
  }, [expired, run])

  const drop = (id: string) => setGone((now) => [...now, id])
  const say = (text: string) => {
    setNotice(text)
    announce.current?.(text)
  }
  const end = (id: string, text: string) => {
    setRun(null)
    drop(id)
    say(text)
    reload()
  }

  /** Claims `job` for this page and answers it as armed (its `expires_at` moved); on a refusal, says why and answers null. */
  async function claim(target: NfcJob, sha: string | null): Promise<NfcJob | null> {
    try {
      return await claimNfcJob(target.id, sha ? { records_sha256: sha } : {})
    } catch (cause) {
      setRun(null)
      const code = codeOf(cause)
      if (code === 'records_mismatch') {
        setNotice(CHANGED)
        reload()
      } else if (code === 'job_not_pending') {
        drop(target.id)
        setNotice(STOPPED_WAITING)
      } else setNotice(`ScadBuddy could not arm the job: ${detailOf(cause)}.`)
      announce.current?.(code === 'records_mismatch' ? CHANGED : code === 'job_not_pending' ? STOPPED_WAITING : 'The job was not armed.')
      return null
    }
  }

  /** Ends an armed job with an error; a refusal (the job ended already) changes nothing here. */
  const fail = (id: string, error: string) => postNfcJobResult(id, { error: clip(error, 500) }).catch(() => null)

  async function arm(target: NfcJob, sha: string | null) {
    const controller = new AbortController()
    stop.current = { id: target.id, controller }
    const { signal } = controller
    setNotice(null)
    setRun({ job: target, phase: 'arming' })
    // One consumer at a time: a read job holds the session itself, a write through `writeWithBackup`.
    const release = session.claim()
    if (!release) {
      setRun(null)
      say('Another write is running on this page. Arm the job once it ends.')
      return
    }
    try {
      try {
        await session.start()
      } catch (cause) {
        setRun(null)
        say(`NFC could not start: ${detailOf(cause)}. Nothing was armed.`)
        return
      }
      const armed = await claim(target, sha)
      if (!armed) return
      setRun({ job: armed, phase: 'tap' })
      if (target.kind === 'read') {
        let reading
        try {
          reading = await session.nextReading(signal)
        } catch {
          if (signal.reason === EXPIRED) end(target.id, 'Expired. Nothing was read.')
          else if (signal.reason === ENDED) end(target.id, STOPPED_WAITING)
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
          if (codeOf(cause) === 'job_not_armed') end(target.id, STOPPED_WAITING)
          else {
            // The job is still armed: end it now, so the agent does not wait out its time.
            const text = `ScadBuddy did not take the reading: ${detailOf(cause)}.`
            await fail(target.id, text)
            end(target.id, text)
          }
        }
        return
      }
    } finally {
      release()
    }

    // A job whose records Web NFC would refuse is never armed (`refused`).
    const records = target.records ?? []
    const outcome = await writeWithBackup({
      session,
      reason: 'agent_write',
      jobId: target.id,
      prepare: async () => records,
      confirm: (_now, backupId, _sent, back) => postNfcJobResult(target.id, { uid: back.uid, records: back.records, backup_id: backupId }).then(() => null),
      onStep: (phase) => !signal.aborted && setRun((shown) => (shown?.job.id === target.id ? { ...shown, phase } : { job: target, phase })),
      signal,
    })
    if (outcome.status === 'checked') {
      end(target.id, `Agent job done: ${headlineOf(outcome, 'write')}`)
      return
    }
    if (signal.reason === NO_READ_BACK) {
      end(target.id, NO_READ_BACK_TEXT)
      return
    }
    if (signal.reason === EXPIRED) {
      end(target.id, 'Expired. Nothing was bound.')
      return
    }
    if (signal.reason === ENDED) {
      end(target.id, STOPPED_WAITING)
      return
    }
    // A failed write ends the job at once, so the agent never waits out its time. A read-back that
    // differs goes to ScadBuddy as it is: the job fails with what the tag holds (`finish_job`).
    if (outcome.status === 'mismatch') {
      const { uid, records: held } = outcome.readBack
      const posted = await postNfcJobResult(target.id, { uid: uid || null, records: held, backup_id: outcome.backupId }).then(
        () => true,
        () => false,
      )
      if (!posted) await fail(target.id, 'the tag does not hold what was sent')
    } else await fail(target.id, errorOf(outcome))
    end(target.id, headlineOf(outcome, 'write'))
  }

  async function decline(target: NfcJob, sha: string | null) {
    setNotice(null)
    setRun({ job: target, phase: 'arming' })
    if (!(await claim(target, sha))) return
    await fail(target.id, 'declined by the person')
    end(target.id, 'Declined. The agent was told.')
  }

  /**
   * R23 from the page: ends a pending job as cancelled; it is never armed, so nothing is read or
   * written. The route answers a job that had already ended unchanged, so the words follow its state.
   */
  async function cancel(target: NfcJob) {
    setNotice(null)
    setCancelling(target.id)
    let text: string
    try {
      const ended = await cancelNfcJob(target.id)
      text = CANCEL_TEXT[ended.state] ?? 'Cancelled. The agent was told.'
      // A cancel never runs a job, so it resets no run: another job may be running by now.
      drop(target.id)
      reload()
    } catch (cause) {
      text =
        codeOf(cause) === 'write_begun'
          ? 'Another page has begun writing this job. It ends with that write’s result.'
          : `ScadBuddy could not cancel the job: ${detailOf(cause)}.`
    } finally {
      setCancelling((now) => (now === target.id ? null : now))
    }
    // Once the poll moved on to another job, the outcome is announced, not shown on that job.
    if (shown.current === target.id || shown.current === null) say(text)
    else announce.current?.(text)
  }

  if (!job && !notice) return null
  const sha = recordsKey !== null && hash?.key === recordsKey ? hash.value : null
  const ready = job !== null && !expired && !run && (job.kind === 'read' || sha !== null)
  // A record Web NFC would refuse is named as the job is shown: it can be cancelled or declined, never armed.
  const refused = job?.kind === 'write' ? notWritable(job.records ?? []) : null
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
            <span className={cn('tabular-nums', expired ? 'font-semibold text-warn' : 'text-muted-foreground')}>
              {expired ? 'Expired' : begun && left <= 0 ? 'Finishing the write' : `Expires in ${clockOf(left)}`}
            </span>
          </div>
          <h2 className="text-base font-semibold tracking-tight">{job.kind === 'read' ? 'Agent wants to read a tag' : `Agent wants to write ${count} ${count === 1 ? 'record' : 'records'}`}</h2>
          {job.records && (
            <ul aria-label="Records to write" className="flex flex-col gap-2">
              {job.records.map((record, at) => {
                const preview = previewOf(record)
                return (
                  <li key={at} className="flex flex-col gap-1 rounded-lg bg-muted px-3 py-2">
                    <span className="flex flex-wrap items-baseline gap-x-2">
                      <span className="font-medium">{record.record_type}</span>
                      {record.media_type && <span className="text-muted-foreground">{record.media_type}</span>}
                      {/* The id is written to the tag too, so it is shown. */}
                      {record.id && <span className="min-w-0 break-all text-muted-foreground">id {record.id}</span>}
                      <span className="ml-auto tabular-nums text-muted-foreground">{record.data_hex.length / 2} bytes</span>
                    </span>
                    {preview.kind === 'text' ? (
                      <pre className="max-h-32 overflow-auto font-mono text-[12px] leading-snug break-words whitespace-pre-wrap">{preview.text}</pre>
                    ) : preview.kind === 'poster' ? (
                      <ul aria-label="Records in the smart poster" className="flex flex-col gap-1.5 border-l-2 border-border pl-3">
                        {preview.records.map((inner, index) => (
                          <li key={index} className="flex flex-col gap-0.5">
                            <span className="font-medium">{inner.recordType}</span>
                            {inner.text !== null ? (
                              <pre className="max-h-24 overflow-auto font-mono text-[12px] leading-snug break-words whitespace-pre-wrap">{visibleText(inner.text) || 'No text'}</pre>
                            ) : (
                              <HexText {...hexPreview(bytesOf(inner.hex))} />
                            )}
                          </li>
                        ))}
                      </ul>
                    ) : (
                      <HexText text={preview.text} more={preview.more} />
                    )}
                  </li>
                )
              })}
            </ul>
          )}
          {refused && !run && <p className="font-medium text-warn">This job cannot be written from this page: {refused}. Cancel or decline it to tell the agent.</p>}
          {run ? (
            <p className="text-base font-medium">{PHASE_TEXT[run.phase]}</p>
          ) : (
            <div className="grid grid-cols-2 gap-3">
              <Button type="button" className="h-11" disabled={!ready || refused !== null || cancelling === job.id} onClick={() => void arm(job, sha)}>
                Arm
              </Button>
              <Button type="button" variant="outline" className="h-11" disabled={!ready || cancelling === job.id} onClick={() => void decline(job, sha)}>
                Decline
              </Button>
              <Button type="button" variant="ghost" className="col-span-2" disabled={expired || cancelling === job.id} onClick={() => void cancel(job)}>
                Cancel job
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
