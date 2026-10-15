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
  const count = images.length
  const name = (index: number) => `Image ${index + 1} of ${count}`
  const image = shown === null ? undefined : images[shown]
  const fullUrl = image?.name && sessionId ? blobUrl(sessionId, image.name) : undefined
  const previewUrl = (sent: SentImage) => blobUrl(sessionId, sent.preview.name)
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
            <Button
              variant="ghost"
              aria-label={`View image ${index + 1} of ${count} larger`}
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
          </li>
        ))}
      </ul>
      {/* A lightbox covers the assistant panel too, rather than sitting beside it as a
          page's dialog does (#798): it is the panel's own, and modal. Its Escape is its
          own (Radix marks it handled), so the panel's Escape does not close the panel. */}
      <Dialog open={image !== undefined && shown !== null} onOpenChange={(next) => !next && close()}>
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
                  {fullUrl
                    ? 'The full image could not be loaded, so this is its preview. The assistant was given the full image.'
                    : 'Only a preview of this image is kept. The assistant was given the full image.'}
                </DialogDescription>
              )}
            </DialogHeader>
            {load !== 'loaded' && (
              <img
                src={previewUrl(image)}
                alt={`${name(shown)}, preview`}
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
