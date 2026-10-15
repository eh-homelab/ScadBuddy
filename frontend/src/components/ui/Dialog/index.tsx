import type { ReactNode } from 'react'
import { Button } from '@/components/ui/button'
import {
  Dialog as DialogRoot,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { cn } from '@/lib/utils'

interface Props {
  open: boolean
  title: string
  description?: string
  onClose: () => void
  children: ReactNode
  footer?: ReactNode
  /** `wide`: up to the window's width, for an image (#1891). `split`: choices beside a docked pane (#2169). */
  size?: 'md' | 'split' | 'wide'
  /** `right`: a drawer along the right edge, full height (the activity detail). */
  placement?: 'center' | 'right'
}

const WIDTH = { md: 'sm:max-w-lg', split: 'sm:max-w-[1120px]', wide: 'sm:max-w-[1600px]' } as const

/**
 * #3653 — the legacy dialog's props over shadcn's dialog (`@/components/ui/dialog`),
 * kept until every area has moved to that directly. The modal behaviour (#351, #798:
 * focus, Escape, the assistant beside it) is DialogContent's.
 */
export function Dialog({ open, title, description, onClose, children, footer, size = 'md', placement = 'center' }: Props) {
  return (
    <DialogRoot
      open={open}
      onOpenChange={(next) => {
        if (!next) onClose()
      }}
    >
      {/* Bounded, with the body scrolling, so the title and the buttons stay reachable
          however tall the content grows (#87). By the *visible* height (`dvh`): on a
          phone `100vh` ran under the browser's toolbars (#1723). */}
      <DialogContent
        placement={placement}
        showCloseButton={false}
        className={cn(
          'flex flex-col gap-0 p-0',
          placement === 'right' ? '' : cn('max-h-[calc(100dvh-2rem)]', WIDTH[size]),
        )}
      >
        <DialogHeader className="shrink-0 border-b px-5 py-4">
          <DialogTitle>{title}</DialogTitle>
          {description && <DialogDescription>{description}</DialogDescription>}
        </DialogHeader>
        <div className="min-h-0 flex-1 overflow-y-auto px-5 py-4">{children}</div>
        {/* #2386 — a dialog with no actions of its own still has a way out that is not
            Esc or the backdrop: touch has no Esc, and on a phone there is little backdrop. */}
        <DialogFooter
          className={cn('m-0 shrink-0 flex-row flex-wrap items-center justify-end px-5 py-3', placement === 'right' && 'rounded-none')}
        >
          {footer ?? (
            <Button variant="ghost" onClick={onClose}>
              Close
            </Button>
          )}
        </DialogFooter>
      </DialogContent>
    </DialogRoot>
  )
}
