import { LinkIcon, PlusIcon, SearchIcon } from 'lucide-react'
import { useEffect, useId, useMemo, useState, type ReactNode } from 'react'
import { Link } from 'react-router'
import { ApiError } from '../../../api/client'
import { listSpools, postTagRead, type DecodedSpool, type NdefRecord, type Spool, type TagReadResult } from '../../../api/filament'
import { filamentLinks } from '../../../lib/filamentLinks'
import { DefinedFields } from '../../entities/DefinedFields'
import { RgbaSwatch } from '../RgbaSwatch'
import { PhotoButtons, type HeldPhotos } from './PhotoButtons'
import { useSpoolFromTag } from './useSpoolFromTag'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'

const FORMAT_NAMES: Record<DecodedSpool['format'], string> = { openspool: 'OpenSpool', opentag3d: 'OpenTag3D' }

function range(min: number | null | undefined, max: number | null | undefined): string | null {
  if (min != null && max != null) return min === max ? `${min} °C` : `${min}–${max} °C`
  if (min != null) return `from ${min} °C`
  if (max != null) return `up to ${max} °C`
  return null
}

/** A warning for a field read and then cut, as the backend's `_capped` words it. */
const CUT = / cut to 128 characters$/

/** What the tag's records decode to: only the fields that are set. */
export function DecodedFields({ decoded }: { decoded: DecodedSpool }) {
  const warnings = decoded.warnings ?? []
  const cut = warnings.filter((one) => CUT.test(one)).map((one) => one.replace(CUT, ''))
  const unread = warnings.filter((one) => !CUT.test(one))
  const rows: [string, ReactNode][] = [['Format', `${FORMAT_NAMES[decoded.format]} ${decoded.version}`.trim()]]
  const material = [decoded.material, decoded.subtype].filter(Boolean).join(' ')
  if (material) rows.push(['Material', material])
  if (decoded.brand) rows.push(['Brand', decoded.brand])
  if (decoded.rgba || decoded.colour_name) {
    rows.push([
      'Colour',
      <span key="colour" className="inline-flex items-center gap-2">
        {decoded.rgba && <RgbaSwatch rgba={decoded.rgba} size="sm" />}
        {decoded.colour_name ?? decoded.rgba}
      </span>,
    ])
  }
  const nozzle = range(decoded.nozzle_temp_min, decoded.nozzle_temp_max)
  if (nozzle) rows.push(['Nozzle', nozzle])
  const bed = range(decoded.bed_temp_min, decoded.bed_temp_max)
  if (bed) rows.push(['Bed', bed])
  if (decoded.label_weight_g != null) rows.push(['Weight', `${decoded.label_weight_g} g`])
  return (
    <section aria-label="On the tag" className="rounded-xl border border-border bg-card p-4">
      <h2 className="mb-2 font-medium">On the tag</h2>
      <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1">
        {rows.map(([term, value]) => (
          <div key={term} className="contents">
            <dt className="text-muted-foreground">{term}</dt>
            <dd className="min-w-0 break-words">{value}</dd>
          </div>
        ))}
      </dl>
      {cut.length > 0 && <p className="mt-2 text-warn">Cut to 128 characters: {cut.join(', ')}.</p>}
      {unread.length > 0 && <p className="mt-2 text-warn">Some fields could not be read: {unread.join(', ')}.</p>}
    </section>
  )
}

/** A spool's material and subtype. */
const materialOf = (spool: Spool) => [spool.names?.material, spool.names?.subtype].filter(Boolean).join(' ')

const reasonOf = (cause: unknown, fallback: string) => (cause instanceof ApiError ? cause.detail : fallback)

/** Every word of the search is in the spool's number, material, brand or colour name. */
function matches(spool: Spool, words: string[]): boolean {
  const text = [`#${spool.number}`, spool.names?.material, spool.names?.subtype, spool.names?.brand, spool.effective?.colour_name ?? spool.colour_name]
    .filter(Boolean)
    .join(' ')
    .toLowerCase()
  return words.every((word) => text.includes(word))
}

export interface NewTagProps {
  uid: string
  records: NdefRecord[]
  result: TagReadResult
  /** The read again after the bind: it holds the spool now. */
  onBound: (result: TagReadResult) => void
  held?: HeldPhotos
  onHeld?: (held: HeldPhotos) => void
}

/**
 * #7273 screen 3 — a tag no spool holds: what it says, the label photos (held until a spool
 * exists), "Create a spool from this tag" from the read's draft, and a search to bind it to a spool.
 */
export function NewTag({ uid, records, result, onBound, held, onHeld }: NewTagProps) {
  const searchId = useId()
  const [binding, setBinding] = useState(false)
  const [bindError, setBindError] = useState<string | null>(null)
  const [spools, setSpools] = useState<Spool[] | null>(null)
  const [search, setSearch] = useState('')
  // #7703 and the kept spool of a failed bind (`made`): the next tap binds it, and makes none.
  const { schema, defined, setDefined, fieldErrors, made, busy: making, error: makeError, create: makeAndBind } = useSpoolFromTag(uid, records, result.draft)
  const busy = binding || making
  const error = bindError ?? makeError

  useEffect(() => {
    let live = true
    listSpools().then(
      (rows) => live && setSpools(rows),
      () => live && setSpools([]),
    )
    return () => {
      live = false
    }
  }, [])

  const found = useMemo(() => {
    const words = search.trim().toLowerCase().split(/\s+/).filter(Boolean)
    if (!spools || words.length === 0) return []
    return spools.filter((spool) => matches(spool, words)).slice(0, 8)
  }, [spools, search])

  async function create() {
    setBindError(null)
    const next = await makeAndBind()
    if (next) onBound(next)
  }

  async function choose(spool: Spool) {
    setBinding(true)
    setBindError(null)
    try {
      onBound(await postTagRead({ uid, tag_type: null, records, bind_spool_id: spool.id }))
    } catch (cause) {
      setBindError(reasonOf(cause, `ScadBuddy could not bind the tag to spool #${spool.number}. Try again.`))
    } finally {
      setBinding(false)
    }
  }

  return (
    <div className="flex flex-col gap-5">
      {result.decoded ? <DecodedFields decoded={result.decoded} /> : <p className="text-muted-foreground">The tag holds no spool data ScadBuddy can read.</p>}
      <PhotoButtons spool={null} held={held} onHeld={onHeld} />
      {!made && result.draft && <DefinedFields schema={schema} values={defined} onChange={setDefined} errors={fieldErrors} />}
      {made ? (
        <Button type="button" className="h-12 text-base" disabled={busy} onClick={() => void create()}>
          <LinkIcon /> Bind to spool #{made.number}
        </Button>
      ) : result.draft ? (
        <Button type="button" className="h-12 text-base" disabled={busy} onClick={() => void create()}>
          <PlusIcon /> Create a spool from this tag
        </Button>
      ) : (
        <Link to={filamentLinks.newSpool} className="underline">
          Make the spool by hand
        </Link>
      )}
      {error && (
        <p role="alert" className="text-warn">
          {error}
        </p>
      )}
      <section aria-labelledby={`${searchId}-label`} className="flex flex-col gap-2">
        <label id={`${searchId}-label`} htmlFor={searchId} className="font-medium">
          Bind to a spool
        </label>
        <div className="relative">
          <SearchIcon className="pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2 text-muted-foreground" aria-hidden="true" />
          <Input
            id={searchId}
            type="search"
            placeholder="Number, material, brand or colour"
            autoComplete="off"
            value={search}
            onChange={(event) => setSearch(event.target.value)}
            className="h-11 pl-9 text-base"
          />
        </div>
        {search.trim() && spools && found.length === 0 && <p className="text-muted-foreground">No spool matches.</p>}
        {found.length > 0 && (
          <ul aria-label="Spools found" className="flex flex-col divide-y divide-border rounded-lg border border-border">
            {found.map((spool) => {
              const rgba = spool.effective?.rgba ?? spool.rgba
              const colour = spool.effective?.colour_name ?? spool.colour_name
              return (
                <li key={spool.id}>
                  <button
                    type="button"
                    disabled={busy}
                    onClick={() => void choose(spool)}
                    className="flex min-h-12 w-full items-center gap-3 px-3 py-2 text-left hover:bg-muted focus-visible:outline-2 focus-visible:outline-ring disabled:opacity-50"
                  >
                    {rgba ? <RgbaSwatch rgba={rgba} /> : <span className="size-5" aria-hidden="true" />}
                    <span className="tabular-nums font-medium">#{spool.number}</span>
                    <span className="min-w-0 truncate text-muted-foreground">{[materialOf(spool), spool.names?.brand, colour].filter(Boolean).join(', ')}</span>
                  </button>
                </li>
              )
            })}
          </ul>
        )}
      </section>
    </div>
  )
}
