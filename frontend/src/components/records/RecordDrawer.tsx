import { useState, type ReactNode } from 'react'
import { useCompanionRoom } from '../../lib/companionRoom'
import { Button } from '@/components/ui/button'
import { ScrollArea } from '@/components/ui/scroll-area'
import { Sheet, SheetClose, SheetContent, SheetFooter, SheetHeader, SheetTitle } from '@/components/ui/sheet'

/**
 * #4249 — a record's detail in a drawer (spec 2026-10-18 §9): the `Sheet` shell of the
 * Activity drawer, extracted so the orders pages open their records the same way.
 * `ActivityDrawer` is one over it. It keeps the shell's rules: modal to the shell unless
 * the assistant panel beside it is usable (#798), closed from the footer (#1897, #2386),
 * and the focus returned to what opened it.
 */

export interface RecordDrawerProps {
  /** Open while mounted by default: the page keys the drawer by record and unmounts it to close. */
  open?: boolean
  onClose: () => void
  title: ReactNode
  /** A row of controls under the title (a Back button, a switch). */
  actions?: ReactNode
  children: ReactNode
  /** Buttons beside Close in the footer. */
  footer?: ReactNode
}

export function RecordDrawer({ open = true, onClose, title, actions, children, footer }: RecordDrawerProps) {
  const { companion, room } = useCompanionRoom()
  // What had focus as the drawer opened; Radix would return it to a trigger, and the
  // drawer has none (the page opens it from a row).
  const [opener] = useState(() => document.activeElement)
  // Beside the assistant panel the drawer is not modal (#798): the panel stays usable.
  const beside = companion !== null && room !== 0
  return (
    <Sheet
      open={open}
      modal={!beside}
      onOpenChange={(next) => {
        if (!next) onClose()
      }}
    >
      <SheetContent
        side="right"
        // Closed from the footer: the corner is where the shell raises its Assistant
        // button above an open dialog (#1897).
        showCloseButton={false}
        aria-describedby={undefined}
        // Modal to the shell (AppShell's assistant toggle reads aria-modal) unless the
        // companion beside it is usable too (#798); Radix itself sets no aria-modal.
        {...(beside ? {} : { 'aria-modal': 'true' as const })}
        // Still modal to the agent bridge (agent/dom activeDialog) either way.
        data-modal=""
        style={beside && room ? { right: room } : undefined}
        onInteractOutside={(event) => {
          // The assistant beside it; the shell's Assistant button is SheetContent's (#1897).
          if (companion?.contains(event.target as Node)) event.preventDefault()
        }}
        onOpenAutoFocus={(event) => {
          // Left in the assistant beside it: the drawer may open while the user types there.
          if (beside && companion.contains(document.activeElement)) event.preventDefault()
        }}
        onCloseAutoFocus={(event) => {
          event.preventDefault()
          // Only focus that fell to <body> comes back: a link in the drawer may have moved it.
          const now = document.activeElement
          if ((!now || now === document.body) && opener instanceof HTMLElement && opener.isConnected) opener.focus()
        }}
        onEscapeKeyDown={(event) => {
          // The companion's own Escape (closing the assistant) is not the drawer's.
          if (beside && companion.contains(event.target as Node)) event.preventDefault()
        }}
        className="gap-0 data-[side=right]:w-full data-[side=right]:sm:max-w-[480px]"
      >
        <SheetHeader className="border-b sm:pr-32">
          <SheetTitle className="break-words">{title}</SheetTitle>
        </SheetHeader>
        {actions && <div className="flex items-center gap-2 border-b px-4 py-2.5">{actions}</div>}
        <ScrollArea className="min-h-0 flex-1">
          <div className="flex flex-col gap-4 p-4">{children}</div>
        </ScrollArea>
        {/* #2386 — a way out that is not Esc or the backdrop: touch has no Esc. */}
        <SheetFooter className="flex-row justify-end border-t py-3">
          {footer}
          <SheetClose asChild>
            <Button variant="outline">Close</Button>
          </SheetClose>
        </SheetFooter>
      </SheetContent>
    </Sheet>
  )
}
