import { useState } from 'react'
import { createPortal } from 'react-dom'
import { dataUrl, fullSizeOf } from '../../agent/chat/images'
import type { ImagePreview } from '../../agent/chat/protocol'
import { ModalCompanionContext } from '../../lib/modal'
import { Button } from '../ui/Button'
import { Dialog } from '../ui/Dialog'

/**
 * #1891 — the images a user turn carried, as previews that open large. The agent keeps
 * previews only (#1866); the tab that sent an image still has it (`fullSizeOf`) and
 * shows that, and anywhere else the preview is shown enlarged, saying so.
 */
export function SentImages({ images }: { images: ImagePreview[] }) {
  const [shown, setShown] = useState<number | null>(null)
  const count = images.length
  const name = (index: number) => `Image ${index + 1} of ${count}`
  const image = shown === null ? undefined : images[shown]
  const full = image && fullSizeOf(image)
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
              onClick={() => setShown(index)}
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
                    : 'Only a preview of this image is kept. The assistant was given the full image; this tab does not have it.'
                }
                onClose={close}
                footer={<Button onClick={close}>Close</Button>}
              >
                <img
                  src={full ?? dataUrl(image)}
                  alt={`${name(shown)}, ${full ? 'as sent' : 'preview'}`}
                  className={`mx-auto max-h-[calc(100vh-12rem)] object-contain ${full ? 'max-w-full' : 'w-full max-w-3xl'}`}
                />
              </Dialog>
            </div>
          </ModalCompanionContext.Provider>,
          document.body,
        )}
    </>
  )
}
