import { useRef, useState } from 'react'
import { ArrowDownIcon, ArrowUpIcon, PlusIcon, UploadIcon, XIcon } from 'lucide-react'
import { api } from '../../api/client'
import type { MediaView, ModelSummary } from '../../api/types'
import { failure, mediaKindOf, mediaProblem, useUploadLimit } from '../../lib/mediaFiles'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { sellMedia } from './listingDraft'

/**
 * What a listing picture that is no image the store can show is, said beside it; null for
 * an image. An id the media has not been read for yet is never called gone.
 */
function pictureNote(id: string, item: MediaView | undefined, images: readonly string[], state: MediaState): string | null {
  if (!item) {
    if (state === 'loading') return 'Reading the design’s media…'
    if (state === 'failed') return 'The design’s media could not be read.'
    return 'Not in the design’s media: publish refuses it. Remove it.'
  }
  if (item.missing) return 'Its file is missing: publish refuses it. Remove it.'
  if (item.kind === 'video') return 'A video: the store shows it as a video. Pictures you add here are images.'
  if (!images.includes(id)) return 'Not an image file the store can show. Remove it.'
  return null
}

/** The image types a listing's upload takes. The store also shows a video a listing names, but an upload here adds an image (#6290). */
const PICTURE_ACCEPT = 'image/png,image/jpeg,image/webp'

/**
 * #6213 — what the public site shows: the media ids, in order; null while the listing is
 * not published; `unknown` for a listing published before publish kept each picture's
 * media id (publish it again to tell).
 */
export type LivePictures = readonly string[] | null | 'unknown'

/** Whether the template's media has been read: until it has, an id is not known to be gone. */
export type MediaState = 'loading' | 'failed' | 'ready'

export interface ListingPicturesProps {
  slug: string
  /** The template's media; only its images can be added. */
  media: readonly MediaView[]
  mediaState?: MediaState
  /** The listing's pictures (`media_ids`), in the order the customer sees them. */
  chosen: readonly string[]
  live: LivePictures
  onChange: (ids: string[]) => void
  /**
   * An upload finished: the template as it left it, and the new image's id. The owner adds
   * the id to the listing as it is when the upload ends (other edits may have landed
   * meanwhile), never to the pictures this component held when it started.
   */
  onUploaded: (model: ModelSummary, id: string) => void
}

/**
 * #6213 — a listing's pictures in the editor: every id of the draft's `media_ids`, in
 * order (Move up, Move down, Remove), so the list is exactly what Save draft and Publish
 * send. Each is marked Live when the public site shows it now, and one that is no image the
 * store can show (a video, a missing file, an id the template's media lacks) says so. Then
 * the template's other images to add, and an upload, which adds the file to the template's
 * media and to the listing at once. A change is the draft's until the listing is saved or
 * published.
 */
export function ListingPictures({ slug, media, mediaState = 'ready', chosen, live, onChange, onUploaded }: ListingPicturesProps) {
  const images = sellMedia(media)
  const byId = new Map(media.map((item) => [item.id, item]))
  const shown = [...chosen]
  const others = images.filter((id) => !chosen.includes(id))
  const [uploading, setUploading] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const input = useRef<HTMLInputElement>(null)
  const uploadLimit = useUploadLimit()

  function move(from: number, to: number) {
    const next = [...shown]
    const [id] = next.splice(from, 1)
    if (id === undefined) return
    next.splice(to, 0, id)
    onChange(next)
  }

  async function upload(file: File) {
    setError(null)
    if (mediaKindOf(file) !== 'image') {
      setError(`${file.name} is not a PNG, JPEG or WebP image.`)
      return
    }
    const problem = mediaProblem(file, await uploadLimit())
    if (problem) {
      setError(problem)
      return
    }
    setUploading(file.name)
    try {
      const before = new Set(media.map((item) => item.id))
      const model = await api.uploadMedia(slug, file)
      // The server adds an upload as the template's last item.
      const added = model.media?.at(-1)
      if (added && !before.has(added.id)) onUploaded(model, added.id)
      else setError(`${file.name} added no new picture to the design; add one of its pictures above instead.`)
    } catch (caught) {
      setError(`${file.name} was not added: ${failure(caught)}`)
    } finally {
      setUploading(null)
    }
  }

  return (
    <div className="flex flex-col gap-2" role="group" aria-label="Pictures">
      <span className="text-sm font-medium">Pictures</span>
      {live === 'unknown' && <p className="text-xs text-muted-foreground">Publish again to see which pictures are live.</p>}
      {Array.isArray(live) && (
        <p className="text-xs text-muted-foreground">Live is what the store shows now. Save draft takes the listing off the store; Publish puts these pictures live.</p>
      )}
      {shown.length === 0 ? (
        <p className="text-xs text-muted-foreground">No pictures: the customer sees none.</p>
      ) : (
        <ol aria-label="Pictures shown" className="flex flex-col gap-1.5">
          {shown.map((id, index) => {
            const item = byId.get(id)
            const name = item?.caption || item?.file || id
            const isLive = Array.isArray(live) && live.includes(id)
            const note = pictureNote(id, item, images, mediaState)
            return (
              <li key={`${index}-${id}`} data-testid="listing-picture" className="flex flex-wrap items-center gap-2 rounded-md border p-1.5 text-sm">
                {item && images.includes(id) ? (
                  <img src={api.mediaUrl(slug, { id })} alt={name} className="h-10 w-14 shrink-0 rounded object-cover" />
                ) : (
                  <span aria-hidden="true" className="flex h-10 w-14 shrink-0 items-center justify-center rounded bg-muted text-[10px] text-muted-foreground">
                    {item?.kind === 'video' ? 'Video' : 'No image'}
                  </span>
                )}
                <span className="flex min-w-0 flex-1 flex-col">
                  <span className="truncate">{name}</span>
                  {note && <span className="text-xs text-warn">{note}</span>}
                </span>
                {Array.isArray(live) && (isLive ? <Badge>Live</Badge> : <Badge variant="outline">Not live yet</Badge>)}
                <Button size="sm" variant="ghost" aria-label={`Move ${name} up`} disabled={index === 0} onClick={() => move(index, index - 1)}>
                  <ArrowUpIcon />
                </Button>
                <Button size="sm" variant="ghost" aria-label={`Move ${name} down`} disabled={index === shown.length - 1} onClick={() => move(index, index + 1)}>
                  <ArrowDownIcon />
                </Button>
                <Button size="sm" variant="ghost" aria-label={`Remove ${name}`} onClick={() => onChange(shown.filter((_other, at) => at !== index))}>
                  <XIcon />
                </Button>
              </li>
            )
          })}
        </ol>
      )}
      {others.length > 0 && (
        <div className="flex flex-col gap-1">
          <span className="text-xs text-muted-foreground">The design’s other pictures</span>
          <ul aria-label="Pictures to add" className="flex flex-wrap gap-2">
            {others.map((id) => {
              const item = byId.get(id)
              const name = item?.caption || item?.file || id
              return (
                <li key={id}>
                  <Button size="sm" variant="outline" className="h-auto gap-2 py-1" aria-label={`Add ${name}`} onClick={() => onChange([...shown, id])}>
                    <img src={api.mediaUrl(slug, { id })} alt="" className="h-8 w-10 rounded object-cover" />
                    <PlusIcon />
                  </Button>
                </li>
              )
            })}
          </ul>
        </div>
      )}
      <div className="flex flex-wrap items-center gap-2">
        <Button size="sm" variant="outline" disabled={!slug || uploading !== null} onClick={() => input.current?.click()}>
          <UploadIcon /> {uploading ? `Uploading ${uploading}…` : 'Upload a picture'}
        </Button>
        <span className="text-xs text-muted-foreground">It is also added to the design’s media.</span>
        <input
          ref={input}
          type="file"
          accept={PICTURE_ACCEPT}
          className="sr-only"
          aria-label="Upload a picture"
          onChange={(event) => {
            const file = event.target.files?.[0]
            event.target.value = ''
            if (file) void upload(file)
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
