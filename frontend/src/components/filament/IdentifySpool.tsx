import { CameraIcon, CameraOffIcon, ImageUpIcon, ListIcon, NfcIcon, QrCodeIcon } from 'lucide-react'
import { useEffect, useId, useMemo, useRef, useState, type ReactNode } from 'react'
import { ApiError } from '../../api/client'
import { getSpool, listSpools, postTagRead, resolveCode, type NdefRecord, type Spool, type TagReadResult } from '../../api/filament'
import { decodeScan, storeScanImage } from '../../api/scan'
import { spoolCode } from '../../lib/homeReturns'
import { codeFromScan } from '../../lib/unknownSpools'
import { NfcSession, nfcSupport } from '../../lib/webNfc'
import { DefinedFields } from '../entities/DefinedFields'
import { FILE_STILL_MAX, stillOf } from './scan/scanCamera'
import { useSpoolFromTag } from './scan/useSpoolFromTag'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { RadioGroup, RadioGroupItem } from '@/components/ui/radio-group'
import { ToggleGroup, ToggleGroupItem } from '@/components/ui/toggle-group'
import { RgbaSwatch } from './RgbaSwatch'

/**
 * #7167 — the three ways a person names a spool: an RFID tag held to the phone (only where Web
 * NFC is there), a code from a ScadBuddy label (typed, or read by the camera), or a pick from the
 * list. No way and no spool is chosen at first: ScadBuddy never assumes which spool it is.
 * `onIdentified` gets each spool the person names, and null as soon as none is named any more
 * (the way changes, the code is edited, a lookup, a photo or a tag read names no spool), so the
 * caller never acts on a spool the person has since replaced. An archived spool is never named.
 */

type Way = 'tag' | 'code' | 'hand'
type OnIdentified = (spool: Spool | null) => void

/** The spool, or why it cannot be the one: an archived spool is in no box. */
const usable = (spool: Spool): Spool | string => (spool.archived_at ? `${spoolCode(spool)} is archived.` : spool)

const reasonOf = (cause: unknown, fallback: string) => (cause instanceof ApiError ? cause.detail : cause instanceof Error && cause.message ? cause.message : fallback)

/** A label's QR text: `scadbuddy:spool:<id>` (`labelPayload`). */
const SPOOL_TEXT = /^scadbuddy:spool:([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$/i

/** The code a typed or scanned text names: `S-142` from `s142` or a label's URL; any other code (`L-4`) as written. */
function codeOf(text: string): string {
  const last = text.trim().split(/[?#]/)[0]!.split('/').filter(Boolean).pop() ?? ''
  return codeFromScan(text) ?? last.trim().toUpperCase()
}

/** The spool a label's text names, or the sentence that says why it names none. */
async function spoolFromText(text: string): Promise<Spool | string> {
  const direct = SPOOL_TEXT.exec(text.trim())
  if (direct) {
    try {
      return usable(await getSpool(direct[1]!.toLowerCase()))
    } catch (cause) {
      return cause instanceof ApiError && cause.status === 404 ? 'This label names a spool that is no longer here.' : reasonOf(cause, 'ScadBuddy could not look up the label. Try again.')
    }
  }
  const code = codeOf(text)
  if (!code) return 'Type a spool code, such as S-142.'
  try {
    const resolved = await resolveCode(code)
    if (resolved.kind !== 'spool') return `${resolved.code} is a place, not a spool.`
    if (resolved.archived) return `${resolved.code} is archived.`
    return usable(await getSpool(resolved.id))
  } catch (cause) {
    return cause instanceof ApiError && cause.status === 404 ? `No spool has the code ${code}.` : reasonOf(cause, 'ScadBuddy could not look up the code. Try again.')
  }
}

export function IdentifySpool({ onIdentified }: { onIdentified: OnIdentified }) {
  const [support] = useState(nfcSupport)
  const [way, setWay] = useState<Way | ''>('')
  const change = (next: Way | '') => {
    setWay(next)
    // Another way: the spool named the old way is no longer the one.
    onIdentified(null)
  }
  return (
    <div className="flex flex-col gap-3">
      <ToggleGroup
        type="single"
        variant="outline"
        aria-label="How do you name the spool?"
        value={way}
        onValueChange={(next) => change(next as Way | '')}
        className="flex-wrap"
      >
        {support === 'ok' && (
          <ToggleGroupItem value="tag">
            <NfcIcon /> Scan tag
          </ToggleGroupItem>
        )}
        <ToggleGroupItem value="code">
          <QrCodeIcon /> QR code
        </ToggleGroupItem>
        <ToggleGroupItem value="hand">
          <ListIcon /> Pick by hand
        </ToggleGroupItem>
      </ToggleGroup>
      {way === 'tag' && <TagWay onIdentified={onIdentified} onPickByHand={() => change('hand')} />}
      {way === 'code' && <CodeWay onIdentified={onIdentified} />}
      {way === 'hand' && <HandWay onIdentified={onIdentified} />}
    </div>
  )
}

function Note({ children }: { children: ReactNode }) {
  return <p className="text-[13px] text-muted-foreground">{children}</p>
}

/** Web NFC: one scan while the way is open; each tap posts the read to `POST /filament/tags/reads`. */
function TagWay({ onIdentified, onPickByHand }: { onIdentified: OnIdentified; onPickByHand: () => void }) {
  const [error, setError] = useState<string | null>(null)
  const [unknown, setUnknown] = useState<{ key: number; uid: string; records: NdefRecord[]; result: TagReadResult } | null>(null)
  const reads = useRef(0)
  const identify = useRef(onIdentified)
  useEffect(() => {
    identify.current = onIdentified
  }, [onIdentified])

  useEffect(() => {
    const session = new NfcSession()
    let alive = true
    const off = session.onReading((reading) => {
      postTagRead({ uid: reading.uid || null, tag_type: null, records: reading.records }).then(
        async (result) => {
          if (!alive) return
          setError(null)
          setUnknown(null)
          // A new read: until it names a spool, none is named.
          identify.current(null)
          const found =
            result.next === 'open_spool' && result.spool
              ? usable(result.spool)
              : result.next === 'open_code' && result.resolved
                ? await spoolFromText(result.resolved.code)
                : null
          if (!alive) return
          if (found !== null) return typeof found === 'string' ? setError(found) : identify.current(found)
          if (result.next === 'bind_or_create') return setUnknown({ key: (reads.current += 1), uid: reading.uid, records: reading.records, result })
          setError('This tag has no serial number. Pick the spool by hand.')
        },
        (cause: unknown) => {
          if (!alive) return
          identify.current(null)
          setError(`ScadBuddy could not look up the tag: ${reasonOf(cause, 'try the tag again')}.`)
        },
      )
    })
    const offError = session.onReadingError(() => setError('This tag could not be read. Hold it to the phone again.'))
    session.start().catch((cause: unknown) => {
      if (!alive) return
      setError(cause instanceof DOMException && cause.name === 'NotAllowedError' ? "Allow NFC for this site in Chrome's settings, then choose Scan tag again." : `NFC could not start: ${reasonOf(cause, 'try again')}.`)
    })
    return () => {
      alive = false
      off()
      offError()
      session.stop()
    }
  }, [])

  return (
    <div className="flex flex-col gap-2 rounded-lg border border-dashed px-3 py-2.5">
      <p className="flex items-center gap-2 text-[13px]">
        <NfcIcon aria-hidden="true" className="size-4 text-primary motion-safe:animate-pulse" />
        Hold the spool's tag to the phone.
      </p>
      {unknown && (
        <UnknownTag
          key={unknown.key}
          uid={unknown.uid}
          records={unknown.records}
          result={unknown.result}
          onMade={(spool) => {
            setUnknown(null)
            onIdentified(spool)
          }}
          onPickByHand={onPickByHand}
        />
      )}
      {error && (
        <p role="alert" className="text-[13px] text-warn">
          {error}
        </p>
      )}
    </div>
  )
}

/**
 * A tag no spool holds: make a spool from its draft (with the spool's required properties) and
 * bind the tag, as the scan page does (`useSpoolFromTag`). A failed bind keeps the made spool,
 * and "Bind to spool #N" binds that one: a retry makes no second spool.
 */
function UnknownTag({ uid, records, result, onMade, onPickByHand }: { uid: string; records: NdefRecord[]; result: TagReadResult; onMade: (spool: Spool) => void; onPickByHand: () => void }) {
  const { schema, defined, setDefined, fieldErrors, made, busy, error, create } = useSpoolFromTag(uid, records, result.draft)

  async function make() {
    const bound = await create()
    if (bound?.spool) onMade(bound.spool)
  }

  return (
    <div className="flex flex-col gap-2">
      <p className="text-[13px] font-medium">This tag is not on a spool yet.</p>
      {!made && result.draft && <DefinedFields schema={schema} values={defined} onChange={setDefined} errors={fieldErrors} />}
      <div className="flex flex-wrap items-center gap-2">
        {(made || result.draft) && (
          <Button type="button" size="sm" disabled={busy} onClick={() => void make()}>
            {made ? `Bind to spool #${made.number}` : 'Make a spool from this tag'}
          </Button>
        )}
        <Button type="button" size="sm" variant="link" className="px-0" onClick={onPickByHand}>
          Pick it by hand
        </Button>
      </div>
      {error && (
        <p role="alert" className="text-[13px] text-warn">
          {error}
        </p>
      )}
    </div>
  )
}

/** The Shape Detection API's detector, where the browser has one (Chrome on Android). */
interface BarcodeDetectorLike {
  detect(source: HTMLVideoElement): Promise<{ rawValue: string }[]>
}
type BarcodeDetectorClass = new (options?: { formats: string[] }) => BarcodeDetectorLike

const detectorClass = (): BarcodeDetectorClass | null => (window as unknown as { BarcodeDetector?: BarcodeDetectorClass }).BarcodeDetector ?? null

/** A typed code, always; the camera where `BarcodeDetector` exists, else a photo the server reads (#8155). */
function CodeWay({ onIdentified }: { onIdentified: OnIdentified }) {
  const [text, setText] = useState('')
  const [said, setSaid] = useState<string | null>(null)
  const [looking, setLooking] = useState(false)
  const [photoBusy, setPhotoBusy] = useState(false)
  const [camera, setCamera] = useState(false)
  const [photosOff, setPhotosOff] = useState(false)
  const [Detector] = useState(detectorClass)
  const photo = useRef<HTMLInputElement>(null)
  const ids = useId()

  /** The answer of a lookup: the spool, or the sentence why none, which also names none. */
  function answer(found: Spool | string) {
    if (typeof found === 'string') {
      setSaid(found)
      onIdentified(null)
    } else onIdentified(found)
  }

  async function look(raw: string) {
    setLooking(true)
    setSaid(null)
    onIdentified(null)
    const found = await spoolFromText(raw)
    setLooking(false)
    answer(found)
  }

  /** The photo drawn again as a JPEG of a known size (core takes JPEG, PNG or WebP; a phone may give HEIC). */
  async function jpegOf(file: File): Promise<Blob> {
    let bitmap: ImageBitmap
    try {
      bitmap = await createImageBitmap(file)
    } catch {
      throw new Error('This browser cannot open that photo. Type the code from the label.')
    }
    try {
      return await stillOf(bitmap, bitmap.width, bitmap.height, FILE_STILL_MAX)
    } finally {
      bitmap.close()
    }
  }

  async function readPhoto(file: File) {
    setPhotoBusy(true)
    setSaid(null)
    onIdentified(null)
    try {
      const stored = await storeScanImage(await jpegOf(file))
      const scan = await decodeScan({ key: stored.key, source: 'shutter', location: null })
      const first = scan.codes.find((code) => code.resolved?.kind === 'spool')
      if (!first?.resolved) {
        answer(scan.error ? 'The photo could not be read. Take it again, closer to the label.' : 'No spool label was found in the photo. Take it again, or type the code.')
        return
      }
      answer(await spoolFromText(first.resolved.code))
    } catch (cause) {
      // The core area is off: no photo route on this server.
      if (cause instanceof ApiError && cause.status === 404) {
        setPhotosOff(true)
        answer('This server reads no photos. Type the code from the label.')
      } else answer(cause instanceof ApiError ? `The photo did not go through: ${cause.detail}.` : reasonOf(cause, 'The photo did not go through. Try again.'))
    } finally {
      setPhotoBusy(false)
    }
  }

  return (
    <div className="flex flex-col gap-2 rounded-lg border border-dashed px-3 py-2.5">
      <form
        className="flex flex-wrap items-end gap-2"
        onSubmit={(event) => {
          event.preventDefault()
          if (text.trim()) void look(text)
        }}
      >
        <div className="flex flex-col gap-1">
          <Label htmlFor={`${ids}-code`}>Spool code</Label>
          <Input
            id={`${ids}-code`}
            value={text}
            placeholder="S-142"
            autoComplete="off"
            onChange={(event) => {
              setText(event.target.value)
              setSaid(null)
              // Another code: the spool the old one named is no longer named.
              onIdentified(null)
            }}
            className="w-40"
          />
        </div>
        <Button type="submit" variant="outline" disabled={looking || !text.trim()}>
          Find
        </Button>
        {Detector && !camera && (
          <Button type="button" variant="outline" onClick={() => {
              setCamera(true)
              onIdentified(null)
            }}
          >
            <CameraIcon /> Use camera
          </Button>
        )}
        {!Detector && !photosOff && (
          <>
            <Button type="button" variant="outline" disabled={photoBusy} onClick={() => photo.current?.click()}>
              <ImageUpIcon /> {photoBusy ? 'Reading the photo…' : 'Take a photo'}
            </Button>
            <input
              ref={photo}
              type="file"
              accept="image/*"
              capture="environment"
              aria-label="Photo of the label"
              className="sr-only"
              tabIndex={-1}
              onChange={(event) => {
                const file = event.target.files?.[0]
                event.target.value = ''
                if (file) void readPhoto(file)
              }}
            />
          </>
        )}
      </form>
      {camera && Detector && (
        <CameraReader
          Detector={Detector}
          onRead={(raw) => {
            setCamera(false)
            void look(raw)
          }}
          onStop={() => setCamera(false)}
          onFail={(reason) => {
            setCamera(false)
            setSaid(reason)
          }}
        />
      )}
      {said && (
        <p role="alert" className="text-[13px] text-warn">
          {said}
        </p>
      )}
    </div>
  )
}

/** The rear camera, read in the browser by `BarcodeDetector` until it finds a code; the camera stops then. */
function CameraReader({ Detector, onRead, onStop, onFail }: { Detector: BarcodeDetectorClass; onRead: (raw: string) => void; onStop: () => void; onFail: (reason: string) => void }) {
  const video = useRef<HTMLVideoElement>(null)
  const done = useRef({ onRead, onFail })
  useEffect(() => {
    done.current = { onRead, onFail }
  }, [onRead, onFail])

  useEffect(() => {
    let alive = true
    let stream: MediaStream | null = null
    let timer: ReturnType<typeof setTimeout> | undefined
    const stop = () => stream?.getTracks().forEach((track) => track.stop())
    const detector = new Detector({ formats: ['qr_code'] })
    async function tick() {
      if (!alive || !video.current) return
      try {
        const found = await detector.detect(video.current)
        const raw = found.find((one) => one.rawValue)?.rawValue
        if (raw && alive) {
          alive = false
          stop()
          done.current.onRead(raw)
          return
        }
      } catch {
        // A frame not ready yet: try the next one.
      }
      if (alive) timer = setTimeout(() => void tick(), 300)
    }
    navigator.mediaDevices
      .getUserMedia({ video: { facingMode: 'environment' } })
      .then((got) => {
        stream = got
        if (!alive) return stop()
        const element = video.current
        if (element) {
          element.srcObject = got
          try {
            void Promise.resolve(element.play()).catch(() => {})
          } catch {
            // The preview may stay still; the detector reads the frames anyway.
          }
        }
        void tick()
      })
      .catch((cause: unknown) => {
        if (alive) done.current.onFail(cause instanceof DOMException && cause.name === 'NotAllowedError' ? 'The camera is not allowed for this page. Type the code from the label, or allow the camera in the browser’s site settings.' : 'The camera did not start. Type the code from the label.')
      })
    return () => {
      alive = false
      clearTimeout(timer)
      stop()
    }
  }, [Detector])

  return (
    <div className="flex flex-col gap-2">
      <div className="relative aspect-[4/3] max-h-56 overflow-hidden rounded-lg bg-black">
        <video ref={video} muted playsInline aria-label="Camera preview" className="size-full object-cover" />
        <div aria-hidden="true" className="pointer-events-none absolute inset-[18%] rounded-md border-2 border-white/80" />
      </div>
      <Button type="button" variant="ghost" size="sm" className="self-start" onClick={onStop}>
        <CameraOffIcon /> Stop camera
      </Button>
    </div>
  )
}

const gramsText = (grams: number | null | undefined) => (grams == null ? 'grams not known' : `${Number(grams.toFixed(1))} g left`)

function searchText(spool: Spool): string {
  return [spoolCode(spool), `#${spool.number}`, spool.names?.material, spool.names?.subtype, spool.names?.brand, spool.effective?.colour_name ?? spool.colour_name, spool.names?.location]
    .filter(Boolean)
    .join(' ')
    .toLowerCase()
}

/** Every unarchived spool, found by its code, colour, material, brand or place; none picked until a click. */
function HandWay({ onIdentified }: { onIdentified: OnIdentified }) {
  const [spools, setSpools] = useState<Spool[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [search, setSearch] = useState('')
  const [picked, setPicked] = useState<string | null>(null)
  const ids = useId()

  useEffect(() => {
    let alive = true
    listSpools().then(
      (rows) => {
        if (alive) setSpools(rows)
      },
      (cause: unknown) => {
        if (alive) setError(`The spools could not be loaded: ${reasonOf(cause, 'try again')}.`)
      },
    )
    return () => {
      alive = false
    }
  }, [])

  const found = useMemo(() => {
    const words = search.trim().toLowerCase().split(/\s+/).filter(Boolean)
    return (spools ?? []).filter((spool) => words.every((word) => searchText(spool).includes(word)))
  }, [spools, search])

  return (
    <div className="flex flex-col gap-2">
      <Input type="search" aria-label="Find a spool" placeholder="Code, colour, material or place" value={search} onChange={(event) => setSearch(event.target.value)} />
      {error && (
        <p role="alert" className="text-[13px] text-warn">
          {error}
        </p>
      )}
      {spools === null && !error && <Note>Loading the spools…</Note>}
      {spools !== null && found.length === 0 && <Note>No spool matches. Check the code on its label.</Note>}
      <RadioGroup
        aria-label="Spools"
        value={picked ?? ''}
        onValueChange={(next) => {
          const spool = spools?.find((one) => one.id === next)
          if (!spool) return
          setPicked(spool.id)
          onIdentified(spool)
        }}
        className="max-h-56 gap-0.5 overflow-y-auto rounded-lg border p-1"
      >
        {found.map((spool) => {
          const rgba = spool.effective?.rgba ?? spool.rgba
          return (
            <div key={spool.id} className="flex items-center gap-2 rounded-md px-2 py-1 has-[[data-state=checked]]:bg-muted">
              <RadioGroupItem value={spool.id} id={`${ids}-${spool.id}`} />
              <Label htmlFor={`${ids}-${spool.id}`} className="flex min-w-0 flex-1 flex-wrap items-center gap-x-2 gap-y-0 font-normal">
                <span className="font-medium tabular-nums">{spoolCode(spool)}</span>{' '}
                {rgba && <RgbaSwatch rgba={rgba} size="sm" />}
                <span>{spool.effective?.colour_name ?? spool.colour_name ?? spool.names?.material ?? 'No colour yet'}</span>{' '}
                <span className="text-muted-foreground">{gramsText(spool.grams_left)},</span>{' '}
                <span className="text-muted-foreground">{spool.names?.location ?? 'No place'}</span>
              </Label>
            </div>
          )
        })}
      </RadioGroup>
    </div>
  )
}
