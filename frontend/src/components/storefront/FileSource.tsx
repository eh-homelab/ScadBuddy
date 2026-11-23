import { useEffect, useRef, useState, type ReactNode } from 'react'
import { PlusIcon, UploadIcon } from 'lucide-react'
import { api } from '../../api/client'
import type { Filament } from '../../api/types'
import { LibraryFilePicker } from '../orders/LibraryFilePicker'
import { failure, mediaKindOf, mediaProblem } from '../../lib/mediaFiles'
import { Button } from '@/components/ui/button'
import { PictureList, type LivePictures, type PictureItem } from './ListingPictures'
import { supportFilament, type DraftSlot, type ListingDraft } from './listingDraft'
import { LISTED_TYPES, platePicture, type ListedFile } from './listedFile'

/**
 * #6710 §9 — the editor's parts for a listing that sells a library file: the file picked
 * (`FileChoice`), what the library says of the published version (`FileStateNotes`), the
 * file's parts (`FileParts`) and its pictures (`FilePictures`).
 */

/** The largest picture `POST …/pictures` takes (backend `upload_picture`, 413 over it). */
const PICTURE_MAX_BYTES = 10 * 1024 * 1024

const PICTURE_ACCEPT = 'image/png,image/jpeg,image/webp'

const PLATE = /^plate:([1-9][0-9]{0,3})$/

function Swatch({ colour, label }: { colour: string; label?: string }) {
  return <span role={label ? 'img' : undefined} aria-label={label} aria-hidden={label ? undefined : true} className="inline-block size-3.5 shrink-0 rounded-full border border-foreground/15" style={{ background: colour }} />
}

/** The parts a customer colours, in slot order: used, and not support. */
function offeredSlots(slots: readonly DraftSlot[]): DraftSlot[] {
  return slots.filter((slot) => slot.used && !slot.support)
}

/**
 * §9.1, §9.2 — the file a listing sells: its picture, name, folder, type and parts, with
 * "Change" opening the search (3MF and STL only) under it. With none picked, the search.
 */
export function FileChoice({
  draft,
  file,
  library,
  listingId,
  onChange,
}: {
  draft: ListingDraft
  file: ListedFile | undefined
  library: 'bambuddy' | 'native' | undefined
  listingId: string | null | undefined
  onChange: (draft: ListingDraft) => void
}) {
  const picked = draft.library_file ?? null
  const [searching, setSearching] = useState(false)
  const search = (
    <LibraryFilePicker
      open
      picked={null}
      disabled={!library}
      types={LISTED_TYPES}
      onPick={(hit) => {
        setSearching(false)
        if (!hit || !library || (picked && picked.id === hit.id && picked.library === library)) return
        onChange({ ...draft, library_file: { id: hit.id, library, filename: hit.filename, repin: false, pinned: null } })
      }}
    />
  )
  if (!picked) {
    return (
      <div className="flex flex-col gap-1">
        {search}
        {!library && <p className="text-xs text-muted-foreground">Reading which library serves…</p>}
      </div>
    )
  }
  const entry = file?.entry ?? null
  const parts = file?.slots ? offeredSlots(file.slots) : null
  const plate = file?.plates?.find((p) => p.has_thumbnail)
  const picture = plate ? platePicture(draft, listingId, plate.index) : entry?.has_thumbnail ? api.libraryThumbnailUrl(picked.id) : null
  const type = (entry?.file_type ?? picked.filename.split('.').pop() ?? '').toUpperCase()
  return (
    <div className="flex flex-col gap-2">
      <div className="flex items-center gap-3 rounded-md border p-2" data-testid="listing-file">
        {picture ? (
          <img src={picture} alt="" className="size-16 shrink-0 rounded bg-muted/50 object-contain" />
        ) : (
          <span aria-hidden="true" className="flex size-16 shrink-0 items-center justify-center rounded bg-muted text-[11px] font-medium text-muted-foreground">
            {type || 'File'}
          </span>
        )}
        <div className="flex min-w-0 flex-1 flex-col gap-0.5">
          <span className="truncate text-sm font-medium">{picked.filename || entry?.filename}</span>
          <span className="truncate text-xs text-muted-foreground">
            {entry ? (entry.folder_path?.length ? entry.folder_path.join(' / ') : 'Top level') : null}
            {entry && type ? `, ${type}` : ''}
          </span>
          {parts && (
            <span className="flex items-center gap-1.5 text-xs text-muted-foreground">
              {parts.length === 1 ? '1 part' : `${parts.length} parts`}
              {parts.map((slot) => (
                <Swatch key={slot.slot} colour={slot.colour} />
              ))}
            </span>
          )}
        </div>
        <Button variant="ghost" size="sm" aria-expanded={searching} onClick={() => setSearching((now) => !now)}>
          {searching ? 'Cancel' : 'Change'}
        </Button>
      </div>
      {searching && search}
    </div>
  )
}

/**
 * §9.4 — what the library says of a published file listing's file. The listing keeps
 * selling its pinned version whatever this finds, so it only informs: a file that changed
 * (its stated `file_hash` is not the pin) offers "Use the library's version", a file that is
 * gone says so, and a request already made offers "Keep the published version".
 */
export function FileStateNotes({
  draft,
  file,
  onChange,
  onKeepPublished,
}: {
  draft: ListingDraft
  file: ListedFile | undefined
  onChange: (draft: ListingDraft) => void
  onKeepPublished?: () => void
}) {
  const picked = draft.library_file
  if (!picked?.pinned || !file) return null
  if (picked.repin) {
    return (
      <Note>
        <span>The next publish takes the library’s version.</span>
        <Button variant="outline" size="xs" onClick={() => onKeepPublished?.()}>
          Keep the published version
        </Button>
      </Note>
    )
  }
  if (file.state === 'gone') return <Note>This file is no longer in the library. Customers still get the version you published.</Note>
  // #7073: the library's entry states its sha256 (lowercase hex); none stated, nothing shows.
  const stated = file.entry?.file_hash
  if (file.state !== 'ready' || !stated || stated.toLowerCase() === picked.pinned) return null
  return (
    <Note>
      <span>This file changed in the library since you published. Customers still get the version you published.</span>
      <Button variant="outline" size="xs" onClick={() => onChange({ ...draft, library_file: { ...picked, repin: true } })}>
        Use the library’s version
      </Button>
    </Note>
  )
}

function Note({ children }: { children: ReactNode }) {
  return (
    <div role="note" className="flex flex-wrap items-center gap-x-3 gap-y-1.5 rounded-md border border-warn/50 bg-warn/5 p-2.5 text-xs">
      {children}
    </div>
  )
}

function filamentName(filament: Filament): string {
  return `${filament.material} ${filament.colour_name ?? filament.colour}`
}

/**
 * §9.3 — "Parts", in place of a design's settings: one row per part that prints, in slot
 * order. A part a customer colours is "Part n", as the customer's page names it; a support
 * part is the shop's, locked to the support filament publish picks from the colours on offer
 * (§5.1 row 8), which is said here and set by ticking support filaments below.
 */
export function FileParts({ draft, file, filaments }: { draft: ListingDraft; file: ListedFile | undefined; filaments: readonly Filament[] }) {
  const slots = file?.slots?.filter((slot) => slot.used) ?? null
  // A part a customer colours is numbered among those parts only, as the customer’s page numbers them.
  const numbers = new Map(offeredSlots(slots ?? []).map((slot, index) => [slot.slot, index + 1]))
  return (
    <section className="flex flex-col gap-2" aria-label="Parts">
      <h3 className="text-sm font-semibold">Parts</h3>
      {!draft.library_file ? (
        <p className="text-xs text-muted-foreground">Pick a library file to see its parts.</p>
      ) : slots === null && !file?.slotsFailed ? (
        <p className="text-xs text-muted-foreground">Reading the file’s parts…</p>
      ) : slots === null ? (
        <p className="text-xs text-warn">The file’s parts could not be read. Reload the page to try again.</p>
      ) : (
        <>
          <p className="text-xs text-muted-foreground">
            {file?.slotsFrom === 'file'
              ? 'Each colour the file names. Publish reads which of them a part prints in and leaves the others out.'
              : 'Each colour a part of the file prints in. The customer picks one of the colours on offer for each.'}
          </p>
          <ul className="flex flex-col gap-1.5">
            {slots.map((slot) => {
              if (slot.support) {
                const locked = supportFilament(slot, draft.colours, filaments)
                return (
                  <li key={slot.slot} data-testid="listing-part" className="flex flex-wrap items-center gap-2 rounded-md border px-3 py-2 text-sm">
                    <Swatch colour={slot.colour} label={`The file’s colour ${slot.colour}`} />
                    <span className="font-medium">Support part</span>
                    {locked ? (
                      <span className="text-xs text-muted-foreground">Support: locked to {filamentName(locked)}</span>
                    ) : (
                      <span className="text-xs text-warn">No support filament is on offer. Tick a support filament under “Colours on offer”, or publish refuses the listing.</span>
                    )}
                  </li>
                )
              }
              return (
                <li key={slot.slot} data-testid="listing-part" className="flex flex-wrap items-center gap-2 rounded-md border px-3 py-2 text-sm">
                  <Swatch colour={slot.colour} label={`The file’s colour ${slot.colour}`} />
                  <span className="font-medium">Part {numbers.get(slot.slot)}</span>
                  <span className="text-xs text-muted-foreground">The customer picks its colour</span>
                </li>
              )
            })}
          </ul>
        </>
      )}
    </section>
  )
}

/**
 * §9.3, decision 11 — a file listing's pictures: the file's plate pictures (`plate:<n>`) and
 * the shop's uploads (`upload:<sha256>`, §5.4), listed, ordered and marked Live as a
 * template's are. An upload goes through the page (`onUpload`), which saves a new draft first
 * and adds the id it answers; this tab shows the uploaded image until the page is left, since
 * no route reads an upload back before publish.
 */
export function FilePictures({
  draft,
  file,
  listingId,
  live,
  onChange,
  onUpload,
}: {
  draft: ListingDraft
  file: ListedFile | undefined
  listingId: string | null | undefined
  live: LivePictures
  onChange: (ids: string[]) => void
  onUpload?: (file: File) => Promise<string | null>
}) {
  const [uploading, setUploading] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [previews, setPreviews] = useState<Record<string, string>>({})
  const input = useRef<HTMLInputElement>(null)
  const made = useRef<string[]>([])
  useEffect(() => () => made.current.forEach((url) => URL.revokeObjectURL(url)), [])

  const plates = (file?.plates ?? []).filter((plate) => plate.has_thumbnail).map((plate) => plate.index)
  const items: PictureItem[] = draft.media_ids.map((id) => {
    const plate = PLATE.exec(id)?.[1]
    if (plate !== undefined) {
      const n = Number(plate)
      const known = file?.plates == null || plates.includes(n) || Boolean(draft.library_file?.pinned)
      return {
        id,
        name: `Plate ${n}`,
        src: platePicture(draft, listingId, n),
        placeholder: 'No image',
        note: known ? null : 'This file has no picture of that plate: publish refuses it. Remove it.',
      }
    }
    if (id.startsWith('upload:')) return { id, name: 'Uploaded picture', src: previews[id] ?? null, placeholder: 'Uploaded', note: null }
    return { id, name: id, src: null, placeholder: 'No image', note: 'Not a picture of this file: publish refuses it. Remove it.' }
  })
  const others = plates.filter((n) => !draft.media_ids.includes(`plate:${n}`))

  async function upload(picture: File) {
    setError(null)
    if (mediaKindOf(picture) !== 'image') {
      setError(`${picture.name} is not a PNG, JPEG or WebP image.`)
      return
    }
    const problem = mediaProblem(picture, PICTURE_MAX_BYTES)
    if (problem) {
      setError(problem)
      return
    }
    if (!onUpload) return
    setUploading(picture.name)
    try {
      const id = await onUpload(picture)
      if (id) {
        const url = URL.createObjectURL(picture)
        made.current.push(url)
        setPreviews((now) => ({ ...now, [id]: url }))
      }
    } catch (caught) {
      setError(`${picture.name} was not added: ${failure(caught)}`)
    } finally {
      setUploading(null)
    }
  }

  return (
    <div className="flex flex-col gap-2" role="group" aria-label="Pictures">
      <PictureList items={items} live={live} onChange={onChange} />
      {others.length > 0 && (
        <div className="flex flex-col gap-1">
          <span className="text-xs text-muted-foreground">The file’s other plate pictures</span>
          <ul aria-label="Pictures to add" className="flex flex-wrap gap-2">
            {others.map((n) => (
              <li key={n}>
                <Button size="sm" variant="outline" className="h-auto gap-2 py-1" aria-label={`Add Plate ${n}`} onClick={() => onChange([...draft.media_ids, `plate:${n}`])}>
                  <img src={platePicture(draft, listingId, n) ?? ''} alt="" className="h-8 w-10 rounded object-cover" />
                  <PlusIcon />
                </Button>
              </li>
            ))}
          </ul>
        </div>
      )}
      <div className="flex flex-wrap items-center gap-2">
        <Button size="sm" variant="outline" disabled={!draft.library_file || uploading !== null} onClick={() => input.current?.click()}>
          <UploadIcon /> {uploading ? `Uploading ${uploading}…` : 'Upload a picture'}
        </Button>
        <span className="text-xs text-muted-foreground">{listingId ? 'It is kept with this listing.' : 'This saves the listing as a draft first.'}</span>
        <input
          ref={input}
          type="file"
          accept={PICTURE_ACCEPT}
          className="sr-only"
          aria-label="Upload a picture"
          onChange={(event) => {
            const picture = event.target.files?.[0]
            event.target.value = ''
            if (picture) void upload(picture)
          }}
        />
      </div>
      {error && (
        <p role="alert" className="text-xs text-destructive">
          {error}
        </p>
      )}
    </div>
  )
}
