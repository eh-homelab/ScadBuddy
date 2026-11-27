import { ListIcon, NfcIcon, QrCodeIcon } from 'lucide-react'
import { useEffect, useId, useMemo, useRef, useState, type ReactNode } from 'react'
import { ApiError } from '../../api/client'
import { getLocation, listLocations, postTagRead, resolveCode, type LocationDetail, type LocationNode, type TagReadResult } from '../../api/filament'
import { CodeWay, Said, type CodeCopy } from './identify/CodeWay'
import { reasonOf } from './identify/reason'
import { useTagReads } from './identify/useTagReads'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { RadioGroup, RadioGroupItem } from '@/components/ui/radio-group'
import { ToggleGroup, ToggleGroupItem } from '@/components/ui/toggle-group'

/**
 * #7168 — the three ways a person names a place, as `IdentifySpool` names a spool: the place's
 * tag (only where Web NFC is), the place label's QR code or typed code, or a pick from the list.
 * No way and no place is chosen at first. `onIdentified` gets each place the person names, and
 * null as soon as none is named any more. An archived place and an AMS unit's place (a printer's
 * unit, not one of its slots: the move refuses it) are never named. With `nfc` `embedded` (in a
 * frame) there is no tag way and no camera.
 */

type Way = 'tag' | 'code' | 'hand'
type OnIdentified = (place: LocationDetail | null) => void
type Place = Pick<LocationDetail, 'code' | 'archived_at' | 'ams_id' | 'tray_id'>

/** An AMS unit's own place: a unit, no tray. */
const amsUnit = (place: Pick<LocationNode, 'ams_id' | 'tray_id'>) => place.ams_id != null && place.tray_id == null

/** The place, or why it cannot be the one. */
function usable<P extends Place>(place: P): P | string {
  if (place.archived_at) return `${place.code} is archived.`
  if (amsUnit(place)) return `${place.code} is an AMS unit. Choose one of its slots, or another place.`
  return place
}

/** A spool label's QR text (`labelPayload`). */
const SPOOL_TEXT = /^scadbuddy:spool:[0-9a-f-]{36}$/i

/** The code a typed or scanned text names: `L-4` from `l4` or a place label's URL `<base>/x/L-4`; any other code as written. */
function codeOf(text: string): string {
  const last = (text.trim().split(/[?#]/)[0]!.split('/').filter(Boolean).pop() ?? '').trim()
  const short = /^([LS])-?(\d+)$/i.exec(last)
  return short ? `${short[1]!.toUpperCase()}-${short[2]}` : last.toUpperCase()
}

/** The place a resolved code names, or the sentence that says why it names none. */
async function placeOfCode(code: string): Promise<LocationDetail | string> {
  try {
    const resolved = await resolveCode(code)
    if (resolved.kind !== 'location') return `${resolved.code} is a spool, not a place.`
    if (resolved.archived) return `${resolved.code} is archived.`
    return usable(await getLocation(resolved.id))
  } catch (cause) {
    return cause instanceof ApiError && cause.status === 404 ? `No place has the code ${code}.` : reasonOf(cause, 'ScadBuddy could not look up the code. Try again.')
  }
}

/** The place a label's text names, or the sentence that says why it names none. */
async function placeFromText(text: string): Promise<LocationDetail | string> {
  if (SPOOL_TEXT.test(text.trim())) return "This is a spool's label, not a place's."
  const code = codeOf(text)
  if (!code) return 'Type a place code, such as L-4.'
  // A spool code is never a place's: refused before any lookup.
  return /^S-\d+$/.test(code) ? `${code} is a spool, not a place.` : placeOfCode(code)
}

const PLACE_COPY: CodeCopy = { label: 'Place code', placeholder: 'L-4', noLabel: 'No place label was found in the photo. Take it again, or type the code.' }

export function IdentifyPlace({ onIdentified, nfc }: { onIdentified: OnIdentified; nfc: 'ok' | 'no_api' | 'embedded' }) {
  const [way, setWay] = useState<Way | ''>('')
  const change = (next: Way | '') => {
    setWay(next)
    // Another way: the place named the old way is no longer the one.
    onIdentified(null)
  }
  return (
    <div className="flex flex-col gap-3">
      <ToggleGroup type="single" variant="outline" aria-label="How do you name the place?" value={way} onValueChange={(next) => change(next as Way | '')} className="flex-wrap">
        {nfc === 'ok' && (
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
      {way === 'tag' && <TagWay onIdentified={onIdentified} />}
      {way === 'code' && <CodeWay kind="location" copy={PLACE_COPY} fromText={placeFromText} fromCode={placeOfCode} onIdentified={onIdentified} camera={nfc !== 'embedded'} />}
      {way === 'hand' && <HandWay onIdentified={onIdentified} />}
    </div>
  )
}

function Note({ children }: { children: ReactNode }) {
  return <p className="text-[13px] text-muted-foreground">{children}</p>
}

/** What a tag read names: the place, or the sentence why none. */
async function placeOfRead(result: TagReadResult): Promise<LocationDetail | string> {
  if (result.next === 'open_spool' || (result.next === 'open_code' && result.resolved?.kind === 'spool')) return "This tag is a spool's tag."
  if (result.next === 'open_code' && result.resolved) return placeOfCode(result.resolved.code)
  return "This tag names no place. Use the QR code on the place's label, or pick the place by hand."
}

/** Web NFC: each tap posts the read to `POST /filament/tags/reads`; the newest tap's answer names the place. */
function TagWay({ onIdentified }: { onIdentified: OnIdentified }) {
  const { error, setError } = useTagReads({
    read: async (reading) => placeOfRead(await postTagRead({ uid: reading.uid || null, tag_type: null, records: reading.records })),
    // A new read: until it names a place, none is named.
    onTap: () => onIdentified(null),
    onAnswer: (found) => (typeof found === 'string' ? setError(found) : onIdentified(found)),
  })

  return (
    <div className="flex flex-col gap-2 rounded-lg border border-dashed px-3 py-2.5">
      <p className="flex items-center gap-2 text-[13px]">
        <NfcIcon aria-hidden="true" className="size-4 text-primary motion-safe:animate-pulse" />
        Hold the place's tag to the phone.
      </p>
      {error && <Said>{error}</Said>}
    </div>
  )
}

/** A place in the list: its code, its name and the places it is in. */
type Row = { node: LocationNode; code: string; path: string }

/** Every place the list holds, its path beside it; an AMS unit's place is left out (its slots stay). */
function rowsOf(nodes: LocationNode[], path: string[] = []): Row[] {
  return nodes.flatMap((node) => [...(amsUnit(node) ? [] : [{ node, code: `L-${node.number}`, path: path.join(' / ') }]), ...rowsOf(node.children ?? [], [...path, node.name])])
}

const spoolsText = (node: LocationNode) => {
  const count = `${node.spool_count} ${node.spool_count === 1 ? 'spool' : 'spools'}`
  return node.capacity == null ? count : `${count} of ${node.capacity}`
}

/** The unarchived places, found by code, name or the places they are in; none picked until a click. */
function HandWay({ onIdentified }: { onIdentified: OnIdentified }) {
  const [rows, setRows] = useState<Row[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [search, setSearch] = useState('')
  const [picked, setPicked] = useState<string | null>(null)
  const pick = useRef(0)
  const ids = useId()

  useEffect(() => {
    let alive = true
    listLocations().then(
      (nodes) => {
        if (alive) setRows(rowsOf(nodes))
      },
      (cause: unknown) => {
        if (alive) setError(`The places could not be loaded: ${reasonOf(cause, 'try again')}.`)
      },
    )
    return () => {
      alive = false
    }
  }, [])

  const found = useMemo(() => {
    const words = search.trim().toLowerCase().split(/\s+/).filter(Boolean)
    return (rows ?? []).filter((row) => words.every((word) => `${row.code} ${row.node.name} ${row.path}`.toLowerCase().includes(word)))
  }, [rows, search])

  async function choose(id: string) {
    const mine = (pick.current += 1)
    setPicked(id)
    setError(null)
    onIdentified(null)
    try {
      const place = usable(await getLocation(id))
      if (mine !== pick.current) return
      if (typeof place === 'string') setError(place)
      else onIdentified(place)
    } catch (cause) {
      if (mine === pick.current) setError(`The place could not be read: ${reasonOf(cause, 'try again')}.`)
    }
  }

  return (
    <div className="flex flex-col gap-2">
      <Input type="search" aria-label="Find a place" placeholder="Code or name" value={search} onChange={(event) => setSearch(event.target.value)} />
      {error && <Said>{error}</Said>}
      {rows === null && !error && <Note>Loading the places…</Note>}
      {rows !== null && found.length === 0 && <Note>No place matches. Check the code on its label.</Note>}
      <RadioGroup aria-label="Places" value={picked ?? ''} onValueChange={(next) => void choose(next)} className="max-h-56 gap-0.5 overflow-y-auto rounded-lg border p-1">
        {found.map(({ node, code, path }) => (
          <div key={node.id} className="flex items-center gap-2 rounded-md px-2 py-1 has-[[data-state=checked]]:bg-muted">
            <RadioGroupItem value={node.id} id={`${ids}-${node.id}`} />
            <Label htmlFor={`${ids}-${node.id}`} className="flex min-w-0 flex-1 flex-wrap items-baseline gap-x-2 gap-y-0 font-normal">
              <span className="font-medium tabular-nums">{code}</span> <span>{node.name}</span>{' '}
              {path && <span className="text-muted-foreground">in {path},</span>} <span className="text-muted-foreground">{spoolsText(node)}</span>
            </Label>
          </div>
        ))}
      </RadioGroup>
    </div>
  )
}
