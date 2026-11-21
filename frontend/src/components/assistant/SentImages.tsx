import { FileTextIcon } from 'lucide-react'
import { useRef, useState } from 'react'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { blobUrl, type SentImage } from '../../agent/chat/protocol'

/** Where the opened image's full size stands: loading, shown, or failed (its preview shown instead). */
type Load = 'loading' | 'loaded' | 'failed'

/**
 * #1891 — the images a user turn carried, as previews that open large. The agent stores
 * each image and its preview with the session and the turn names both (#2479), so the
 * thumbnails and the lightbox load them from the agent's blob route (same-origin, so
 * inside Bambuddy's sandboxed frame too), the lightbox showing the preview until the
 * full image has. A turn whose full image was never stored, or a load that fails (a
 * deleted session), shows the preview enlarged, saying so.
 */
export function SentImages({ images, sessionId = '' }: { images: SentImage[]; sessionId?: string }) {
  const [shown, setShown] = useState<number | null>(null)
  const [load, setLoad] = useState<Load>('loading')
  /** The thumbnail that opened the lightbox: focus goes back to it when it closes. */
  const opener = useRef<HTMLElement | null>(null)
  // A PDF chip is not an image: the labels count and number the images only.
  const isImage = (sent: SentImage) => sent.mediaType !== 'application/pdf'
  const count = images.filter(isImage).length
  const place = (index: number) => images.slice(0, index + 1).filter(isImage).length
  const name = (index: number) => `Image ${place(index)} of ${count}`
  const image = shown === null ? undefined : images[shown]
  const fullUrl = image?.name && sessionId ? blobUrl(sessionId, image.name) : undefined
  // A server-built image (#6267) has no preview: its full image stands in for it.
  const previewUrl = (sent: SentImage) => blobUrl(sessionId, 'preview' in sent ? sent.preview.name : (sent.name ?? ''))
  const full = fullUrl !== undefined && load !== 'failed'
  const open = (index: number) => {
    setLoad('loading')
    setShown(index)
  }
  const close = () => setShown(null)

  return (
    <>
      <ul aria-label="Images sent" className="mb-1 flex flex-wrap gap-1.5">
        {images.map((sent, index) => (
          <li key={index}>
            {sent.mediaType === 'application/pdf' ? (
              // #6267: an invoice PDF. The blob route serves it as an attachment, so it
              // downloads (in a new tab, which also escapes Bambuddy's sandboxed frame).
              <a
                href={sent.name ? blobUrl(sessionId, sent.name) : undefined}
                target="_blank"
                rel="noopener noreferrer"
                className="inline-flex min-h-6 max-w-[12rem] items-center gap-1.5 rounded-md border border-border bg-muted/40 px-2 py-1 text-xs text-foreground hover:bg-muted"
              >
                <FileTextIcon aria-hidden className="size-4 shrink-0 text-muted-foreground" />
                <span className="truncate" title={'fileName' in sent && sent.fileName ? sent.fileName : sent.name}>
                  {'fileName' in sent && sent.fileName ? sent.fileName : 'Invoice PDF'}
                </span>
                <span className="shrink-0 rounded bg-muted px-1 font-medium text-muted-foreground">PDF</span>
              </a>
            ) : (
              <Button
                variant="ghost"
                aria-label={`View image ${place(index)} of ${count} larger`}
                aria-haspopup="dialog"
                onClick={(event) => {
                  opener.current = event.currentTarget
                  open(index)
                }}
                className="h-auto min-h-6 min-w-6 cursor-zoom-in p-0 hover:bg-transparent"
              >
                <img
                  src={previewUrl(sent)}
                  alt={name(index)}
                  className="max-h-24 max-w-[8rem] rounded-md border border-border object-contain"
                />
              </Button>
            )}
          </li>
        ))}
      </ul>
      {/* A lightbox covers the assistant panel too, rather than sitting beside it as a
          page's dialog does (#798): it is the panel's own, and modal. Its Escape is its
          own (Radix marks it handled), so the panel's Escape does not close the panel. */}
      <Dialog modal open={image !== undefined && shown !== null} onOpenChange={(next) => !next && close()}>
        {image && shown !== null && (
          <DialogContent
            data-modal=""
            className="sm:max-w-4xl"
            showCloseButton={false}
            onCloseAutoFocus={(event) => {
              // No DialogTrigger (one lightbox for every thumbnail): back to the one opened.
              event.preventDefault()
              opener.current?.focus()
            }}
            {...(full ? { 'aria-describedby': undefined } : {})}
          >
            <DialogHeader>
              <DialogTitle>{name(shown)}</DialogTitle>
              {!full && (
                <DialogDescription>
                  {fullUrl && !('preview' in image)
                    ? 'The image could not be loaded. The assistant was given it.'
                    : fullUrl
                    ? 'The full image could not be loaded, so this is its preview. The assistant was given the full image.'
                    : 'Only a preview of this image is kept. The assistant was given the full image.'}
                </DialogDescription>
              )}
            </DialogHeader>
            {load !== 'loaded' && ('preview' in image || load !== 'failed') && (
              <img
                src={previewUrl(image)}
                alt={'preview' in image ? `${name(shown)}, preview` : name(shown)}
                className="mx-auto max-h-[calc(100dvh-12rem)] w-full max-w-3xl object-contain"
              />
            )}
            {full && (
              // A same-origin URL, so it loads inside Bambuddy's sandboxed frame too.
              // Hidden until it has, while the preview stands in for it.
              <img
                key={fullUrl}
                src={fullUrl}
                alt={`${name(shown)}, as sent`}
                hidden={load !== 'loaded'}
                onLoad={() => setLoad('loaded')}
                onError={() => setLoad('failed')}
                className="mx-auto max-h-[calc(100dvh-12rem)] max-w-full object-contain"
              />
            )}
            <DialogFooter>
              <DialogClose asChild>
                <Button variant="outline" size="lg">
                  Close
                </Button>
              </DialogClose>
            </DialogFooter>
          </DialogContent>
        )}
      </Dialog>
    </>
  )
}
