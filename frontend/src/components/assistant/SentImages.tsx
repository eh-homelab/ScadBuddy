import { useState } from 'react'
import { createPortal } from 'react-dom'
import { dataUrl } from '../../agent/chat/images'
import { blobUrl, type SentImage } from '../../agent/chat/protocol'
import { ModalCompanionContext } from '../../lib/modal'
import { Button } from '../ui/Button'
import { Dialog } from '../ui/Dialog'

/** Where the opened image's full size stands: loading, shown, or failed (its preview shown instead). */
type Load = 'loading' | 'loaded' | 'failed'

/**
 * #1891 — the images a user turn carried, as previews that open large. The agent stores
 * each full image with the session and the turn names it (`SentImage.name`), so the
 * lightbox loads it from the agent's blob route, showing the preview until it has. A
 * turn logged before that, or a load that fails (a deleted session), shows the preview
 * enlarged, saying so.
 */
export function SentImages({ images, sessionId = '' }: { images: SentImage[]; sessionId?: string }) {
  const [shown, setShown] = useState<number | null>(null)
  const [load, setLoad] = useState<Load>('loading')
  const count = images.length
  const name = (index: number) => `Image ${index + 1} of ${count}`
  const image = shown === null ? undefined : images[shown]
  const fullUrl = image?.name && sessionId ? blobUrl(sessionId, image.name) : undefined
  const full = fullUrl !== undefined && load !== 'failed'
  const open = (index: number) => {
    setLoad('loading')
    setShown(index)
  }
  const close = () => setShown(null)

  return (
    <>
      <ul aria-label="Images sent" className="mb-1 flex flex-wrap gap-1.5">
        {images.map((preview, index) => (
          <li key={index}>
            <button
              type="button"
              aria-label={`View image ${index + 1} of ${count} larger`}
              aria-haspopup="dialog"
              onClick={() => open(index)}
              className="block min-h-6 min-w-6 cursor-zoom-in rounded-[4px] outline-none focus-visible:ring-2 focus-visible:ring-accent"
            >
              <img
                src={dataUrl(preview)}
                alt={name(index)}
                className="max-h-24 max-w-[8rem] rounded-[4px] border border-line object-contain"
              />
            </button>
          </li>
        ))}
      </ul>
      {image &&
        shown !== null &&
        createPortal(
          // A lightbox covers the assistant panel too, rather than sitting beside it as a
          // page's dialog does (#798): it is the panel's own, and modal. Its Escape is
          // kept from the panel, whose own Escape would close the panel with it.
          <ModalCompanionContext.Provider value={null}>
            <div
              onKeyDown={(event) => {
                if (event.key !== 'Escape') return
                event.preventDefault()
                event.stopPropagation()
                close()
              }}
            >
              <Dialog
                open
                size="wide"
                title={name(shown)}
                description={
                  full
                    ? undefined
                    : fullUrl
                      ? 'The full image could not be loaded, so this is its preview. The assistant was given the full image.'
                      : 'Only a preview of this image is kept. The assistant was given the full image.'
                }
                onClose={close}
                footer={<Button onClick={close}>Close</Button>}
              >
                {load !== 'loaded' && (
                  <img
                    src={dataUrl(image)}
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
              </Dialog>
            </div>
          </ModalCompanionContext.Provider>,
          document.body,
        )}
    </>
  )
}
