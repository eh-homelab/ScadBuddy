import { CameraIcon, CameraOffIcon, ExternalLinkIcon, ImageUpIcon, RotateCwIcon, SearchIcon, Trash2Icon } from 'lucide-react'
import { useId, useRef, useState } from 'react'
import { Link } from 'react-router'
import { ApiError } from '../../../api/client'
import { type CodeScan, type ScannedCode, decodeScanAgain, deleteScan, scanImageUrl } from '../../../api/scan'
import { codeLabel } from '../../../lib/resolvedCode'
import { isScadBuddyCode, scanSearchPath } from '../../../lib/scanSearch'
import { codeFromScan } from '../../../lib/unknownSpools'
import { CodeCard } from './CodeCard'
import { formatName, useScanCamera } from './scanCamera'
import { Button } from '@/components/ui/button'
import { cn } from '@/lib/utils'

const timeOf = (at: string) => new Date(at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })

/** What a scan's row says it found. */
function outcomeOf(scan: CodeScan): string {
  if (scan.error === 'image_too_large') return 'Photo too large to read'
  if (scan.error) return 'Photo could not be read'
  if (scan.codes.length === 0) return 'No code found'
  const first = scan.codes[0]!
  const named = first.resolved ? codeLabel(first.resolved) : formatName(first.format)
  return scan.codes.length > 1 ? `${named} and ${scan.codes.length - 1} more` : named
}

function failureOf(cause: unknown, doing: string): string {
  if (cause instanceof ApiError) return `${doing}: ${cause.detail}`
  if (cause instanceof Error && cause.message) return cause.message
  return `${doing}. Try again.`
}

/** An https address a code holds, which a tap may open; null for any other text. */
function httpsLink(text: string): string | null {
  try {
    const url = new URL(text)
    return url.protocol === 'https:' ? url.href : null
  } catch {
    return null
  }
}

/**
 * #8155 — "Scan code": the rear camera's live preview with a shutter, or a chosen photo when
 * the browser gives no camera. The server reads every QR code, Data Matrix, EAN, UPC and Code
 * 128 in the still and says what each names: a spool label opens its spool, a place label its
 * place, and any other text shows with its format. Each scan keeps its photo.
 */
export function ScanCode() {
  const camera = useScanCamera()
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [openId, setOpenId] = useState<string | null>(null)
  const file = useRef<HTMLInputElement>(null)
  const fileId = useId()
  if (!camera) return null
  const { state, reason, scans, start, stop, bindVideo, capture, scanFile, replace, forget } = camera

  async function run(take: () => Promise<{ scan: CodeScan } | null>) {
    if (busy) return
    setBusy(true)
    setError(null)
    try {
      const taken = await take()
      if (taken) setOpenId(taken.scan.id)
    } catch (cause) {
      setError(failureOf(cause, 'The scan did not go through'))
    } finally {
      setBusy(false)
    }
  }

  const opened = scans.find((one) => one.id === openId) ?? null
  const live = state === 'on' || state === 'starting'

  return (
    <section aria-labelledby="scan-code" className="flex flex-col gap-3">
      <div className="flex items-baseline justify-between gap-3">
        <h2 id="scan-code" className="font-medium">
          Scan code
        </h2>
        {live && (
          <Button type="button" variant="ghost" size="sm" onClick={stop}>
            <CameraOffIcon /> Stop camera
          </Button>
        )}
      </div>

      {live ? (
        <div className="relative aspect-[4/3] overflow-hidden rounded-2xl bg-black">
          <video ref={bindVideo} muted playsInline aria-label="Camera preview" className="size-full object-cover" />
          {/* The frame a label goes in: brackets at its corners. */}
          <div aria-hidden="true" className="pointer-events-none absolute inset-[14%]">
            {['top-0 left-0 border-t-2 border-l-2 rounded-tl-lg', 'top-0 right-0 border-t-2 border-r-2 rounded-tr-lg', 'bottom-0 left-0 border-b-2 border-l-2 rounded-bl-lg', 'right-0 bottom-0 border-r-2 border-b-2 rounded-br-lg'].map((corner) => (
              <span key={corner} className={cn('absolute size-8 border-white/90', corner)} />
            ))}
          </div>
          {busy && <div aria-hidden="true" className="absolute inset-0 bg-white/30 motion-safe:animate-pulse" />}
          <div className="absolute inset-x-0 bottom-3 flex justify-center">
            <button
              type="button"
              disabled={state !== 'on' || busy}
              onClick={() => void run(() => capture('shutter'))}
              className="flex size-16 items-center justify-center rounded-full border-4 border-white/90 bg-white/20 text-white backdrop-blur-sm transition-transform active:scale-95 disabled:opacity-50 focus-visible:outline-3 focus-visible:outline-offset-2 focus-visible:outline-white"
            >
              <span className="sr-only">{busy ? 'Reading the photo' : 'Scan code'}</span>
              <span aria-hidden="true" className="size-11 rounded-full bg-white" />
            </button>
          </div>
        </div>
      ) : (
        <div className="flex flex-col gap-2 rounded-2xl border border-dashed border-border p-4">
          {reason ? <p className="text-warn">{reason}</p> : <p className="text-muted-foreground">Point the camera at a label's QR code or a box's barcode.</p>}
          <div className="flex flex-wrap gap-2">
            {state !== 'unavailable' && (
              <Button type="button" className="h-11" onClick={() => void start()}>
                <CameraIcon /> Start camera
              </Button>
            )}
            <Button type="button" variant="outline" className="h-11" disabled={busy} onClick={() => file.current?.click()}>
              <ImageUpIcon /> {busy ? 'Reading the photo…' : 'Choose a photo'}
            </Button>
          </div>
        </div>
      )}
      <input
        ref={file}
        id={fileId}
        type="file"
        accept="image/*"
        capture="environment"
        aria-label="Photo of a code"
        className="sr-only"
        tabIndex={-1}
        onChange={(event) => {
          const chosen = event.target.files?.[0]
          event.target.value = ''
          if (chosen) void run(() => scanFile(chosen))
        }}
      />

      {error && (
        <p role="alert" className="text-warn">
          {error}
        </p>
      )}
      {opened && <ScanDetail key={opened.id} scan={opened} onChange={replace} onGone={(id) => (forget(id), setOpenId(null))} />}

      {scans.length > 0 && (
        <ul aria-label="Codes scanned" className="flex flex-col divide-y divide-border rounded-lg border border-border">
          {scans.map((scan) => (
            <li key={scan.id}>
              <button
                type="button"
                aria-current={scan.id === openId ? 'true' : undefined}
                onClick={() => setOpenId(scan.id)}
                className="grid min-h-14 w-full grid-cols-[auto_auto_1fr] items-center gap-3 px-3 py-2 text-left hover:bg-muted focus-visible:outline-2 focus-visible:outline-ring aria-[current=true]:bg-muted"
              >
                <img src={scanImageUrl(scan.id)} alt="" className="size-10 rounded-md bg-muted object-cover" />
                <span className="tabular-nums text-muted-foreground">{timeOf(scan.at)}</span>
                <span className="min-w-0 truncate font-medium">{outcomeOf(scan)}</span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}

/** One scan: its photo, each code it read, and what it names. */
function ScanDetail({ scan, onChange, onGone }: { scan: CodeScan; onChange: (scan: CodeScan) => void; onGone: (id: string) => void }) {
  const [busy, setBusy] = useState<'decode' | 'delete' | null>(null)
  const [error, setError] = useState<string | null>(null)

  async function act(kind: 'decode' | 'delete') {
    setBusy(kind)
    setError(null)
    try {
      if (kind === 'decode') onChange(await decodeScanAgain(scan.id))
      else {
        await deleteScan(scan.id)
        onGone(scan.id)
      }
    } catch (cause) {
      setError(failureOf(cause, kind === 'decode' ? 'The photo was not read again' : 'The scan was not deleted'))
    } finally {
      setBusy(null)
    }
  }

  return (
    <article aria-label={`Scan at ${timeOf(scan.at)}`} className="flex flex-col gap-3 rounded-xl border border-border bg-card p-3">
      <img src={scanImageUrl(scan.id)} alt={`The photo scanned at ${timeOf(scan.at)}`} className="max-h-64 w-full rounded-lg bg-muted object-contain" />
      {scan.codes.length === 0 ? (
        <p className="font-medium">{outcomeOf(scan)}. Hold the code flat and in the frame, then scan again.</p>
      ) : (
        <ul aria-label="Codes in this photo" className="flex flex-col gap-2">
          {scan.codes.map((code, index) => (
            <li key={`${code.format}:${index}`}>
              <ScannedCodeView code={code} />
            </li>
          ))}
        </ul>
      )}
      {scan.location && (
        <p className="text-muted-foreground tabular-nums">
          Taken at {scan.location.lat.toFixed(5)}, {scan.location.lon.toFixed(5)}
          {scan.location.accuracy_m !== null && ` (within ${Math.round(scan.location.accuracy_m)} m)`}
        </p>
      )}
      {error && (
        <p role="alert" className="text-warn">
          {error}
        </p>
      )}
      <div className="flex flex-wrap gap-2">
        <Button type="button" variant="outline" className="h-11" disabled={busy !== null} onClick={() => void act('decode')}>
          <RotateCwIcon /> {busy === 'decode' ? 'Reading again…' : 'Read again'}
        </Button>
        <Button type="button" variant="ghost" className="h-11" disabled={busy !== null} onClick={() => void act('delete')}>
          <Trash2Icon /> Delete scan
        </Button>
      </div>
    </article>
  )
}

/** A code that names a thing opens it; any other text shows with its format, and an https address opens only on a tap. */
function ScannedCodeView({ code }: { code: ScannedCode }) {
  if (code.resolved) return <CodeCard resolved={code.resolved} />
  const link = httpsLink(code.text)
  // A text with the shape of a ScadBuddy code names no spool here; a search would find nothing (#8372).
  const searchable = !isScadBuddyCode(code.text) && codeFromScan(code.text) === null
  return (
    <div className="flex flex-col gap-1 rounded-lg bg-muted/60 p-3">
      <span className="text-muted-foreground">{formatName(code.format)}</span>
      <p className="break-all font-medium">{code.text}</p>
      {link && (
        <a href={link} target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-1 self-start text-primary underline-offset-2 hover:underline">
          <ExternalLinkIcon className="size-4" /> Open this address
        </a>
      )}
      {searchable && (
        <Link to={scanSearchPath(code.text)} className="inline-flex items-center gap-1 self-start break-all text-primary underline-offset-2 hover:underline">
          <SearchIcon className="size-4 shrink-0" /> Search spools for {code.text}
        </Link>
      )}
    </div>
  )
}
