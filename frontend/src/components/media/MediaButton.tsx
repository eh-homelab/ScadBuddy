import { useState } from 'react'
import { ImagesIcon } from 'lucide-react'
import type { ModelSummary } from '../../api/types'
import { MediaManager } from './MediaManager'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog'

interface Props {
  model: ModelSummary
  onChanged?: (model: ModelSummary) => void
}

/** #279 — opens a template's media to manage; on a built-in, what was added to it (#722). */
export function MediaButton({ model, onChanged }: Props) {
  const [open, setOpen] = useState(false)

  return (
    <>
      <Button size="sm" variant="ghost" className="text-muted-foreground" onClick={() => setOpen(true)}>
        <ImagesIcon /> Media
      </Button>
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent className="max-h-[calc(100dvh-2rem)] overflow-y-auto sm:max-w-2xl">
          <DialogHeader>
            <DialogTitle>Media</DialogTitle>
            <DialogDescription>
              {model.origin === 'builtin'
                ? 'What the template ships, then the images and videos added to it. Each change is saved as it is made.'
                : 'Images and videos, in order: the first is the cover. Each change is saved as it is made.'}
            </DialogDescription>
          </DialogHeader>
          <MediaManager key={model.slug} model={model} onChanged={onChanged} />
        </DialogContent>
      </Dialog>
    </>
  )
}
