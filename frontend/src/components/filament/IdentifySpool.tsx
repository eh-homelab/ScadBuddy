import { ListIcon, NfcIcon, QrCodeIcon } from 'lucide-react'
import { useEffect, useId, useMemo, useRef, useState, type ReactNode } from 'react'
import { ApiError } from '../../api/client'
import { getSpool, listSpools, postTagRead, resolveCode, type NdefRecord, type Spool, type TagReadResult } from '../../api/filament'
import { spoolCode } from '../../lib/homeReturns'
import { codeFromScan } from '../../lib/unknownSpools'
import { matchesSearch, parseSearch, spoolSearchDoc, type PropertyText } from '../../lib/filamentSearch'
import { scanSearchQuery, searchesSpools } from '../../lib/scanSearch'
import { takeMifare } from '../../lib/nativeMifare'
import { nfcSupport, type Reading } from '../../lib/webNfc'
import { loadKindPropertyTexts } from '../entities/api'
import { DefinedFields } from '../entities/DefinedFields'
import { SPOOL_FORMATS } from './identify/camera'
import { CodeWay, Said, type CodeCopy, type CodeSearch } from './identify/CodeWay'
import { reasonOf } from './identify/reason'
import { useTagReads } from './identify/useTagReads'
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
 * `nfc` is the page's `nfcSupport()`: in a frame (`embedded`) there is no tag way and no camera.
 * A code that names no thing (a maker's barcode) becomes a "Pick by hand" search (#8372).
 */

type Way = 'tag' | 'code' | 'hand'
type OnIdentified = (spool: Spool | null) => void

/** The spool, or why it cannot be the one: an archived spool is in no box. */
const usable = (spool: Spool): Spool | string => (spool.archived_at ? `${spoolCode(spool)} is archived.` : spool)

/** A label's QR text: `scadbuddy:spool:<id>` (`labelPayload`). */
const SPOOL_TEXT = /^scadbuddy:spool:([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$/i

/** The code a typed or scanned text names: `S-142` from `s142` or a label's URL; any other code (`L-4`) as written. */
function codeOf(text: string): string {
  const last = text.trim().split(/[?#]/)[0]!.split('/').filter(Boolean).pop() ?? ''
  return codeFromScan(text) ?? last.trim().toUpperCase()
}

/**
 * The spool a label's text names, or the sentence that says why it names none. With `search`
 * (#8372), a text that no code resolves and that is not a ScadBuddy code (a barcode) gives its
 * spool search instead.
 */
function spoolFromText(text: string): Promise<Spool | string>
function spoolFromText(text: string, search: true): Promise<Spool | string | CodeSearch>
async function spoolFromText(text: string, search = false): Promise<Spool | string | CodeSearch> {
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
    const unknown = cause instanceof ApiError && cause.status === 404
    if (unknown && search && searchesSpools(text)) return { search: scanSearchQuery(text) }
    return unknown ? `No spool has the code ${code}.` : reasonOf(cause, 'ScadBuddy could not look up the code. Try again.')
  }
}

const spoolOrSearch = (text: string) => spoolFromText(text, true)

const SPOOL_COPY: CodeCopy = { label: 'Spool code', placeholder: 'S-142', noLabel: 'No spool label was found in the photo. Take it again, or type the code.' }

export function IdentifySpool({ onIdentified, nfc }: { onIdentified: OnIdentified; nfc?: ReturnType<typeof nfcSupport> }) {
  // Asked once for the component's life, not on each render (#7168 review 3).
  const [support] = useState(() => nfc ?? nfcSupport())
  const [way, setWay] = useState<Way | ''>('')
  /** #8372: the search a scanned code gave, which "Pick by hand" opens with. */
  const [scanned, setScanned] = useState<string | null>(null)
  const change = (next: Way | '') => {
    setWay(next)
    setScanned(null)
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
      {way === 'code' && (
        <CodeWay
          kind="spool"
          copy={SPOOL_COPY}
          fromText={spoolOrSearch}
          fromCode={spoolFromText}
          onIdentified={onIdentified}
          onSearch={(search) => {
            setWay('hand')
            setScanned(search)
          }}
          camera={support !== 'embedded'}
          formats={SPOOL_FORMATS}
        />
      )}
      {way === 'hand' && <HandWay onIdentified={onIdentified} initialSearch={scanned ?? ''} fromScan={scanned !== null} />}
    </div>
  )
}

function Note({ children }: { children: ReactNode }) {
  return <p className="text-[13px] text-muted-foreground">{children}</p>
}

/** A 4-byte UID with no NDEF records, as a Bambu tag's side gives: a browser reads no more of it (Web NFC is NDEF only). */
const bambuSide = (uid: string, records: NdefRecord[]) => records.length === 0 && uid.replace(/[^0-9a-f]/gi, '').length === 8

/**
 * One tap's read. In the ScadBuddy app a Bambu tag's MIFARE blocks come with the tap, and core
 * finds the spool by its tray UUID, so either side names it (#8148). A server that does not
 * decode them (503 `mifare_needs_core`, 422 `mifare_unreadable` or `mifare_invalid`, refused
 * before anything is recorded) gets the plain read, as the scan page does it. `blocks` says
 * whether the answer came from the blocks.
 */
async function postTap(uid: string, records: NdefRecord[]): Promise<{ result: TagReadResult; blocks: boolean }> {
  const plain = { uid: uid || null, tag_type: null, records }
  const mifare = takeMifare(uid)
  if (!mifare) return { result: await postTagRead(plain), blocks: false }
  try {
    return { result: await postTagRead({ ...mifare, records: [] }), blocks: true }
  } catch (cause) {
    const undecoded = cause instanceof ApiError && ((cause.status === 422 && (cause.problem.code === 'mifare_unreadable' || cause.problem.code === 'mifare_invalid')) || (cause.status === 503 && cause.problem.code === 'mifare_needs_core'))
    if (!undecoded) throw cause
    return { result: await postTagRead(plain), blocks: false }
  }
}

/** One tap's answer: the spool or the sentence why none (`found`, null when the read names nothing), and the read. */
interface TapAnswer {
  reading: Reading
  result: TagReadResult
  blocks: boolean
  found: Spool | string | null
}

async function readTap(reading: Reading): Promise<TapAnswer> {
  const { result, blocks } = await postTap(reading.uid, reading.records)
  const found =
    result.next === 'open_spool' && result.spool
      ? usable(result.spool)
      : result.next === 'open_code' && result.resolved
        ? await spoolFromText(result.resolved.code)
        : null
  return { reading, result, blocks, found }
}

/** Web NFC: each tap posts the read to `POST /filament/tags/reads`; the newest tap's answer names the spool. */
function TagWay({ onIdentified, onPickByHand }: { onIdentified: OnIdentified; onPickByHand: () => void }) {
  const [unknown, setUnknown] = useState<{ key: number; uid: string; records: NdefRecord[]; result: TagReadResult } | null>(null)
  const [unseenSide, setUnseenSide] = useState(false)
  const unknowns = useRef(0)
  const { error, setError } = useTagReads({
    read: readTap,
    onTap: () => {
      setUnknown(null)
      setUnseenSide(false)
      // A new read: until it names a spool, none is named.
      onIdentified(null)
    },
    onAnswer: ({ reading, result, blocks, found }) => {
      if (found !== null) return typeof found === 'string' ? setError(found) : onIdentified(found)
      // A 4-byte UID with no records, read without the app: no spool holds it, and making one could duplicate a spool.
      if (result.next === 'bind_or_create' && !blocks && bambuSide(reading.uid, reading.records)) return setUnseenSide(true)
      if (result.next === 'bind_or_create') return setUnknown({ key: (unknowns.current += 1), uid: reading.uid, records: reading.records, result })
      setError('This tag has no serial number. Pick the spool by hand.')
    },
  })

  return (
    <div className="flex flex-col gap-2 rounded-lg border border-dashed px-3 py-2.5">
      <p className="flex items-center gap-2 text-[13px]">
        <NfcIcon aria-hidden="true" className="size-4 text-primary motion-safe:animate-pulse" />
        Hold the spool's tag to the phone.
      </p>
      {unseenSide && (
        <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
          <Said>This side of the Bambu tag is on no spool yet. Tap the other side, use the QR code, or pick the spool by hand.</Said>
          <Button type="button" size="sm" variant="link" className="h-auto px-0" onClick={onPickByHand}>
            Pick it by hand
          </Button>
        </div>
      )}
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

const gramsText = (grams: number | null | undefined) => (grams == null ? 'grams not known' : `${Number(grams.toFixed(1))} g left`)

function searchText(spool: Spool): string {
  return [spoolCode(spool), `#${spool.number}`, spool.names?.material, spool.names?.subtype, spool.names?.brand, spool.effective?.colour_name ?? spool.colour_name, spool.names?.location]
    .filter(Boolean)
    .join(' ')
    .toLowerCase()
}

/**
 * Every unarchived spool. Each word of the box matches its code, colour, material, brand or place,
 * or the filament search over its and its type's property values (#8372); none picked until a click. `fromScan`
 * (a scanned code's search in `initialSearch`): once the spools and both property reads have
 * settled, one shown spool is named. That decision is made once, and never after an edit of the box.
 */
function HandWay({ onIdentified, initialSearch = '', fromScan = false }: { onIdentified: OnIdentified; initialSearch?: string; fromScan?: boolean }) {
  const [spools, setSpools] = useState<Spool[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [search, setSearch] = useState(initialSearch)
  const [picked, setPicked] = useState<string | null>(null)
  const [properties, setProperties] = useState<ReadonlyMap<string, PropertyText[]> | null>(null)
  const [typeProperties, setTypeProperties] = useState<ReadonlyMap<string, PropertyText[]> | null>(null)
  /** Whether the scan's one decision is made (or there is none to make). A ref, so StrictMode's second effect run does not make it again. */
  const decided = useRef(!fromScan)
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
    // A failed read gives an empty map, so it settles too.
    void loadKindPropertyTexts('spool').then((found) => {
      if (alive) setProperties(found)
    })
    void loadKindPropertyTexts('spool_type').then((found) => {
      if (alive) setTypeProperties(found)
    })
    return () => {
      alive = false
    }
  }, [])

  const docs = useMemo(
    () =>
      new Map(
        (spools ?? []).map((spool) => [
          spool.id,
          spoolSearchDoc(spool, { properties: properties?.get(spool.id), typeProperties: spool.type_id ? typeProperties?.get(spool.type_id) : undefined }),
        ]),
      ),
    [spools, properties, typeProperties],
  )

  const found = useMemo(() => {
    const words = search
      .trim()
      .toLowerCase()
      .split(/\s+/)
      .filter(Boolean)
      .map((word) => ({ word, tokens: parseSearch(word) }))
    return (spools ?? []).filter((spool) => {
      const text = searchText(spool)
      const doc = docs.get(spool.id)!
      // Each word matches by one rule or the other, so `S-2 gtin:…` finds S-2.
      return words.every(({ word, tokens }) => text.includes(word) || matchesSearch(doc, tokens))
    })
  }, [spools, search, docs])

  const settled = (spools !== null || error !== null) && properties !== null && typeProperties !== null
  const looking = fromScan && !settled
  useEffect(() => {
    if (decided.current || !settled) return
    decided.current = true
    const only = found.length === 1 ? found[0]! : null
    if (!only) return
    setPicked(only.id)
    onIdentified(only)
  }, [settled, found, onIdentified])

  return (
    <div className="flex flex-col gap-2">
      <Input
        type="search"
        aria-label="Find a spool"
        placeholder="Code, colour, material or place"
        value={search}
        onChange={(event) => {
          // After an edit of the box, the scan names no spool by itself.
          decided.current = true
          setSearch(event.target.value)
        }}
      />
      {error && (
        <p role="alert" className="text-[13px] text-warn">
          {error}
        </p>
      )}
      {looking && <Note>Looking for “{search}”…</Note>}
      {spools === null && !error && !looking && <Note>Loading the spools…</Note>}
      {spools !== null && !looking && found.length === 0 && <Note>No spool matches. Check the code on its label.</Note>}
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
        {!looking && found.map((spool) => {
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
