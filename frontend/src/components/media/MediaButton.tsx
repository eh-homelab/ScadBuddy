import { useCallback, useState } from 'react'
import type { ModelSummary } from '../../api/types'
import { Dialog } from '../ui/Dialog'
import { MediaManager } from './MediaManager'

interface Props {
  model: ModelSummary
  onChanged?: (model: ModelSummary) => void
}

/** #279 — opens a template's media to manage; on a built-in, what was added to it (#722). */
export function MediaButton({ model, onChanged }: Props) {
  const [open, setOpen] = useState(false)
  // Stable, because `Dialog` refocuses its panel whenever `onClose` changes.
  const close = useCallback(() => setOpen(false), [])

  return (
    <>
      <button
        type="button"
        onClick={() => setOpen(true)}
        className="rounded-[6px] px-2 py-1 text-[12px] text-muted hover:bg-surface-2 hover:text-ink"
      >
        Media
      </button>
      <Dialog
        open={open}
        title="Media"
        description={
          model.origin === 'builtin'
            ? 'What the template ships, then the images and videos added to it. Each change is saved as it is made.'
            : 'Images and videos, in order: the first is the cover. Each change is saved as it is made.'
        }
        onClose={close}
      >
        <MediaManager key={model.slug} model={model} onChanged={onChanged} />
      </Dialog>
    </>
  )
}
