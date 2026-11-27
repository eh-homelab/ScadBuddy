import { CopyIcon, NfcIcon } from 'lucide-react'
import { useCallback, useEffect, useId, useRef, useState, useSyncExternalStore } from 'react'
import { useNavigate } from 'react-router'
import { ApiError } from '../../api/client'
import { postTagRead, type TagReadResult } from '../../api/filament'
import { BambuFields, type BambuTag } from '../../components/filament/scan/BambuFields'
import { CodeCard } from '../../components/filament/scan/CodeCard'
import { KnownTag } from '../../components/filament/scan/KnownTag'
import { NfcJobBanner } from '../../components/filament/scan/NfcJobBanner'
import { DecodedFields, NewTag } from '../../components/filament/scan/NewTag'
import type { HeldPhotos } from '../../components/filament/scan/PhotoButtons'
import { filamentLinks } from '../../lib/filamentLinks'
import { codeLabel, codeTarget } from '../../lib/resolvedCode'
import { takeMifare } from '../../lib/nativeMifare'
import { NfcSession, nfcSupport, type Reading, type WireRecord } from '../../lib/webNfc'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Switch } from '@/components/ui/switch'
import { cn } from '@/lib/utils'

/** One read of this session: the tap and what the server answered. */
interface Entry {
  key: number
  at: Date
  uid: string
  records: WireRecord[]
  result: TagReadResult
  held: HeldPhotos
}

const timeOf = (at: Date) => at.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })
const outcomeOf = (result: TagReadResult) =>
  result.spool ? `Spool #${result.spool.number}` : result.resolved ? codeLabel(result.resolved) : result.next === 'none' ? 'No serial number' : 'New tag'
/** Where a read opens, when it opens something: the spool's page, or the thing a code named. */
const targetOf = (result: TagReadResult): string | null =>
  result.next === 'open_spool' && result.spool ? filamentLinks.spool(result.spool.id) : result.next === 'open_code' && result.resolved ? codeTarget(result.resolved) : null
/** What the live region says of a read, for a person who cannot see the list change. */
const announcementOf = (uid: string, result: TagReadResult) =>
  uid ? `Read ${uid}: ${outcomeOf(result).replace(/^\w/, (first) => first.toLowerCase())}` : 'Read a tag with no serial number'
const UNREADABLE = 'This tag could not be read. Hold it to the phone again.'

/** "Open what I scan at once", remembered on this phone: `"1"` when on; off when unset or storage is blocked. */
export const OPEN_AT_ONCE_KEY = 'scadbuddy.scan.openAtOnce'
function readOpenAtOnce(): boolean {
  try {
    return localStorage.getItem(OPEN_AT_ONCE_KEY) === '1'
  } catch {
    return false
  }
}
function writeOpenAtOnce(on: boolean) {
  try {
    if (on) localStorage.setItem(OPEN_AT_ONCE_KEY, '1')
    else localStorage.removeItem(OPEN_AT_ONCE_KEY)
  } catch {
    // A blocked storage keeps the switch for this page only.
  }
}

function scanRefusal(cause: unknown): string {
  if (cause instanceof DOMException && cause.name === 'NotAllowedError') return "Allow NFC for this site in Chrome's settings, then tap Scan again."
  if (cause instanceof DOMException && cause.name === 'NotSupportedError') return 'This phone has no NFC reader, or NFC is off. Turn NFC on in the phone’s settings, then tap Scan again.'
  return `NFC could not start${cause instanceof Error && cause.message ? `: ${cause.message}` : ''}. Tap Scan again.`
}

/**
 * #7273 — the phone page (spec 2026-11-23-web-nfc-tags-design.md, screens 1 to 4). One Web NFC
 * scan runs from the first "Scan" until the page goes; each tap is posted to `POST /tags/reads`
 * and listed newest first, and the newest opens: its spool (screen 2), a new tag (screen 3), or a
 * tag with no serial number, or the thing a code on a tag or label names (`CodeCard`, #6955). An
 * agent's NFC job shows on top until the person arms or declines it (`NfcJobBanner`). With "Open
 * what I scan at once" on, a read that opens something navigates there, and the page starts
 * the scan by itself when it opens. A browser without Web NFC gets the page's address to open
 * elsewhere.
 */
export function ScanPage() {
  const [support] = useState(nfcSupport)
  return (
    <div className="mx-auto flex min-h-full w-full max-w-md min-w-0 flex-col gap-5 px-4 pt-4 text-[13px]">
      <div className="flex flex-col gap-1">
        <h1 className="text-lg font-semibold tracking-tight">Scan</h1>
        <p className="text-muted-foreground">Hold a tag or a label to the phone: a spool's tag, a place's tag, or a label with a code such as L-12 or S-142.</p>
      </div>
      {support === 'ok' ? <Scanner /> : <Unsupported embedded={support === 'embedded'} />}
    </div>
  )
}

/** Screen 4: the page cannot scan here. */
function Unsupported({ embedded }: { embedded: boolean }) {
  const href = window.location.href
  const [copied, setCopied] = useState<boolean | null>(null)
  return (
    <div className="flex flex-col gap-4 pb-4">
      <div className="flex flex-col gap-1">
        <p className="text-base font-medium">Open this page in Chrome on Android</p>
        {embedded && <p>Open it in its own tab, not inside Bambuddy: the browser lets only a page in its own tab use NFC.</p>}
        <p className="text-muted-foreground">Copy this address, then open it on the phone.</p>
      </div>
      <div className="flex gap-2">
        <Input readOnly value={href} aria-label="This page's address" className="h-11 text-base" onFocus={(event) => event.target.select()} />
        <Button
          type="button"
          variant="outline"
          className="h-11"
          onClick={() =>
            void navigator.clipboard.writeText(href).then(
              () => setCopied(true),
              () => setCopied(false),
            )
          }
        >
          <CopyIcon /> Copy
        </Button>
      </div>
      <p aria-live="polite" className={cn(copied === false && 'text-warn')}>
        {copied === true && 'Copied'}
        {copied === false && 'The address was not copied. Select it and copy it by hand.'}
      </p>
      <section aria-label="Turn NFC on" className="flex flex-col gap-1">
        <h2 className="font-medium">Turn NFC on</h2>
        <ol className="list-decimal space-y-1 pl-5">
          <li>On the phone, open Settings and search for “NFC”.</li>
          <li>Turn NFC on.</li>
          <li>Open this page in Chrome and tap Scan. Chrome asks once to allow NFC for the site.</li>
        </ol>
      </section>
    </div>
  )
}

/** Every read of this UID, with the change; the others as they are. */
const forTag = (entries: Entry[], uid: string, change: Partial<Entry>) => entries.map((one) => (one.uid === uid ? { ...one, ...change } : one))

/** Screens 1 to 3. */
function Scanner() {
  const [session] = useState(() => new NfcSession())
  // The scan may be started here, by an agent job's Arm or by a write: the button follows the session.
  const scanning = useSyncExternalStore(
    useCallback((changed: () => void) => session.onActiveChange(changed), [session]),
    () => session.active,
  )
  const [starting, setStarting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [entries, setEntries] = useState<Entry[]>([])
  const [openKey, setOpenKey] = useState<number | null>(null)
  const [announcement, setAnnouncement] = useState('')
  const nextKey = useRef(0)
  const [openAtOnce, setOpenAtOnce] = useState(readOpenAtOnce)
  const openAtOnceId = useId()
  const navigate = useNavigate()
  // The reading handler is bound once (below); these refs give it the current switch and navigate.
  const openAtOnceRef = useRef(openAtOnce)
  const navigateRef = useRef(navigate)
  useEffect(() => {
    openAtOnceRef.current = openAtOnce
    navigateRef.current = navigate
  }, [openAtOnce, navigate])

  useEffect(() => {
    const nfc = session
    // The session never changes (`useState`'s initialiser), so this runs once.
    // A read's answer that lands after the page is gone (a tap while an earlier tap's answer
    // navigated away) is dropped, and the page navigates at most once: two taps in flight
    // would otherwise each navigate, the second over the first's page.
    let alive = true
    let navigated = false
    const off = nfc.onReading((reading: Reading) => {
      // In the ScadBuddy app a Bambu tag's blocks come with the tap; the server decodes them (#8148).
      // A server that does not decode them yet (core's area off, 503 mifare_needs_core), or
      // that cannot take these (core's 422 mifare_unreadable or mifare_invalid, a 7-byte UID
      // say, both before it records anything),
      // still finds the tag by its UID and records, as before. Any other refusal is the answer:
      // core may have recorded the read, so a second post would record the tap twice.
      const mifare = takeMifare(reading.uid)
      const plain = { uid: reading.uid || null, tag_type: null, records: reading.records }
      const read = mifare
        ? postTagRead({ ...mifare, records: [] }).catch((cause: unknown) => {
            if (cause instanceof ApiError && ((cause.status === 422 && (cause.problem.code === 'mifare_unreadable' || cause.problem.code === 'mifare_invalid')) || (cause.status === 503 && cause.problem.code === 'mifare_needs_core'))) return postTagRead(plain)
            throw cause
          })
        : postTagRead(plain)
      read.then(
        (result) => {
          if (!alive || navigated) return
          const key = (nextKey.current += 1)
          setEntries((now) => [{ key, at: new Date(), uid: reading.uid, records: reading.records, result, held: {} }, ...now])
          setOpenKey(key)
          setError(null)
          // The read's number makes each announcement new: a live region says nothing when its text stays the same.
          setAnnouncement(`${announcementOf(reading.uid, result)} (read ${key})`)
          const target = openAtOnceRef.current ? targetOf(result) : null
          if (target) {
            navigated = true
            void navigateRef.current(target)
          }
        },
        (cause: unknown) => {
          if (alive) setError(`ScadBuddy could not look up the tag: ${cause instanceof ApiError ? cause.detail : 'try the tag again'}.`)
        },
      )
    })
    const offError = nfc.onReadingError(() => setError(UNREADABLE))
    return () => {
      alive = false
      off()
      offError()
      nfc.stop()
    }
  }, [session])

  async function start() {
    if (scanning || starting) return
    setStarting(true)
    setError(null)
    try {
      await session.start()
    } catch (cause) {
      setError(scanRefusal(cause))
    } finally {
      setStarting(false)
    }
  }

  // With the switch on, the page scans as soon as it opens (coming back from a thing it opened
  // included); a refusal is worded as the button's would be, and the button stays to try again.
  const autoStarted = useRef(false)
  useEffect(() => {
    if (autoStarted.current || !readOpenAtOnce()) return
    autoStarted.current = true
    void start()
    // Once, on mount: `start` reads the session, which never changes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const setOpenAtOnceAndRemember = (on: boolean) => {
    setOpenAtOnce(on)
    writeOpenAtOnce(on)
  }

  const update = (key: number, change: Partial<Entry>) => setEntries((now) => now.map((one) => (one.key === key ? { ...one, ...change } : one)))
  /**
   * A new answer for one read (a bind, a weighing) is the answer for every read of the same tag
   * this session: an older read of it must not offer "Create" again, nor show old grams.
   */
  const answer = (key: number, result: TagReadResult) =>
    setEntries((now) => {
      const uid = now.find((one) => one.key === key)?.uid
      return uid ? forTag(now, uid, { result }) : now.map((one) => (one.key === key ? { ...one, result } : one))
    })
  /**
   * A checked write from the opened read `from`: every read of the written UID takes the
   * answer and the records it holds now. A write onto another tag (a rebind) opens that tag's
   * newest read, or adds the tag as a read when the list has none, and the tag `from` read is
   * looked up again, as it no longer holds the spool.
   */
  const written = (from: Entry, uid: string, records: WireRecord[], result: TagReadResult) => {
    if (from.uid === uid) {
      setEntries((now) => forTag(now, uid, { records, result }))
      return
    }
    // `entries` is newest first, so the first read of the UID is its newest.
    const listed = entries.find((one) => one.uid === uid)
    if (listed) {
      setEntries((now) => forTag(now, uid, { records, result }))
      setOpenKey(listed.key)
    } else {
      const key = (nextKey.current += 1)
      setEntries((now) => [{ key, at: new Date(), uid, records, result, held: {} }, ...now])
      setOpenKey(key)
    }
    // A lookup of a tag read before, not a tap: no scan history row.
    postTagRead({ uid: from.uid || null, tag_type: null, records: from.records, tap: false }).then(
      (old) => setEntries((now) => forTag(now, from.uid, { result: old })),
      (cause: unknown) => setError(`ScadBuddy could not look up the old tag again: ${cause instanceof ApiError ? cause.detail : 'read it again'}.`),
    )
  }
  const opened = entries.find((one) => one.key === openKey) ?? null

  return (
    <>
      {error && (
        <p role="alert" className="text-warn">
          {error}
        </p>
      )}
      <p role="status" aria-label="Last read" className="sr-only">
        {announcement}
      </p>
      <NfcJobBanner session={session} onAnnounce={setAnnouncement} />
      {opened?.result.bambu && <BambuFields bambu={opened.result.bambu as BambuTag} />}
      {opened && (
        <ReadView
          key={opened.key}
          entry={opened}
          session={session}
          onChange={(change) => update(opened.key, change)}
          onResult={(result) => answer(opened.key, result)}
          onWritten={(uid, records, result) => written(opened, uid, records, result)}
          onAnnounce={setAnnouncement}
        />
      )}
      <div className="flex items-center gap-3">
        <Switch id={openAtOnceId} checked={openAtOnce} onCheckedChange={setOpenAtOnceAndRemember} />
        <label htmlFor={openAtOnceId} className="font-medium">
          Open what I scan at once
        </label>
      </div>
      <section aria-labelledby="scan-reads" className="flex flex-col gap-2">
        <h2 id="scan-reads" className="font-medium">
          This session's reads
        </h2>
        {entries.length === 0 ? (
          <p className="text-muted-foreground">{scanning ? 'Hold a tag to the back of the phone.' : 'Tap Scan, then hold a tag to the back of the phone.'}</p>
        ) : (
          <ul aria-label="This session's reads" className="flex flex-col divide-y divide-border rounded-lg border border-border">
            {entries.map((entry) => (
              <li key={entry.key}>
                <button
                  type="button"
                  aria-current={entry.key === openKey ? 'true' : undefined}
                  onClick={() => setOpenKey(entry.key)}
                  className="grid min-h-12 w-full grid-cols-[auto_1fr_auto] items-center gap-3 px-3 py-2 text-left hover:bg-muted focus-visible:outline-2 focus-visible:outline-ring aria-[current=true]:bg-muted"
                >
                  <span className="tabular-nums text-muted-foreground">{timeOf(entry.at)}</span>
                  <span className="min-w-0 truncate tabular-nums">{entry.uid || '—'}</span>
                  <span className="font-medium">{outcomeOf(entry.result)}</span>
                </button>
              </li>
            ))}
          </ul>
        )}
      </section>
      <div className="sticky bottom-0 mt-auto -mx-4 bg-gradient-to-t from-background from-70% to-transparent px-4 pt-6 pb-[max(1rem,env(safe-area-inset-bottom))]">
        <button
          type="button"
          disabled={starting}
          onClick={() => void start()}
          className={cn(
            'relative flex h-16 w-full items-center justify-center gap-3 rounded-2xl text-base font-semibold transition-colors focus-visible:outline-3 focus-visible:outline-offset-2 focus-visible:outline-ring',
            scanning ? 'border-2 border-primary bg-primary/10 text-foreground' : 'bg-primary text-primary-foreground hover:bg-primary/90',
          )}
        >
          <span className="relative flex size-6 items-center justify-center" aria-hidden="true">
            {scanning && <span className="absolute inset-0 rounded-full bg-primary/40 motion-safe:animate-ping" />}
            <NfcIcon className="relative size-6" />
          </span>
          {scanning ? 'Scanning — hold a tag to the phone' : 'Scan'}
        </button>
      </div>
    </>
  )
}

/** The opened read: screen 2 (with "Write tag", screens 5 and 6), screen 3, or a tag with no serial number. */
function ReadView({
  entry,
  session,
  onChange,
  onResult,
  onWritten,
  onAnnounce,
}: {
  entry: Entry
  session: NfcSession
  onChange: (change: Partial<Entry>) => void
  onResult: (result: TagReadResult) => void
  onWritten: (uid: string, records: WireRecord[], result: TagReadResult) => void
  onAnnounce: (text: string) => void
}) {
  const { result } = entry
  if (result.next === 'open_spool' && result.spool) {
    return (
      <KnownTag
        spool={result.spool}
        tag={result.tag}
        session={session}
        held={entry.held}
        onHeld={(held) => onChange({ held })}
        onSpool={(spool) => onResult({ ...result, spool })}
        onWritten={onWritten}
        onAnnounce={onAnnounce}
      />
    )
  }
  if (result.next === 'open_code' && result.resolved) {
    return <CodeCard resolved={result.resolved} />
  }
  if (result.next === 'bind_or_create') {
    return (
      <NewTag
        uid={entry.uid}
        records={entry.records}
        result={result}
        held={entry.held}
        onHeld={(held) => onChange({ held })}
        onBound={onResult}
      />
    )
  }
  return (
    <div className="flex flex-col gap-3">
      {result.decoded && <DecodedFields decoded={result.decoded} />}
      <p className="font-medium">This tag gave no serial number, so ScadBuddy cannot keep it.</p>
    </div>
  )
}
