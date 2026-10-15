"use client"

import * as React from "react"
import { createPortal } from "react-dom"
import { cn } from "@/lib/utils"
import { Dialog as DialogPrimitive } from "radix-ui"
import { ModalCompanionContext, besideNow, leaveToCompanion, roomFor } from "@/lib/modal"

import { Button } from "@/components/ui/button"
import { XIcon } from "lucide-react"

/**
 * #3653 — always Radix's non-modal dialog, made modal here (DialogContent): the
 * assistant panel stays usable beside an open dialog (#798), so the dialog cannot hide
 * the rest of the page from assistive tech or the pointer the way Radix's modal does,
 * and switching Radix's `modal` while open would remount everything in it.
 */
function Dialog({
  open: openProp,
  defaultOpen = false,
  onOpenChange,
  ...props
}: Omit<React.ComponentProps<typeof DialogPrimitive.Root>, "modal">) {
  const [uncontrolled, setUncontrolled] = React.useState(defaultOpen)
  const open = openProp ?? uncontrolled
  return (
    <DialogOpenContext.Provider value={open}>
      <DialogPrimitive.Root
        data-slot="dialog"
        {...props}
        open={open}
        onOpenChange={(next) => {
          if (openProp === undefined) setUncontrolled(next)
          onOpenChange?.(next)
        }}
        modal={false}
      />
    </DialogOpenContext.Provider>
  )
}

/**
 * Whether the dialog is open, for DialogContent: it portals itself in the same commit
 * that opens it, as Radix's portal does not (that mounts a render later), so a caller's
 * effect on opening finds what is in it (#350: a field to select).
 */
const DialogOpenContext = React.createContext(false)

function DialogTrigger({
  ...props
}: React.ComponentProps<typeof DialogPrimitive.Trigger>) {
  return <DialogPrimitive.Trigger data-slot="dialog-trigger" {...props} />
}

function DialogPortal({
  ...props
}: React.ComponentProps<typeof DialogPrimitive.Portal>) {
  return <DialogPrimitive.Portal data-slot="dialog-portal" {...props} />
}

function DialogClose({
  ...props
}: React.ComponentProps<typeof DialogPrimitive.Close>) {
  return <DialogPrimitive.Close data-slot="dialog-close" {...props} />
}

function DialogOverlay({
  className,
  ...props
}: React.ComponentProps<typeof DialogPrimitive.Overlay>) {
  return (
    <DialogPrimitive.Overlay
      data-slot="dialog-overlay"
      className={cn(
        "fixed inset-0 isolate z-50 bg-black/10 duration-100 supports-backdrop-filter:backdrop-blur-xs data-open:animate-in data-open:fade-in-0 data-closed:animate-out data-closed:fade-out-0",
        className
      )}
      {...props}
    />
  )
}

/**
 * The room kept for the companion (the assistant panel, `ModalCompanionContext`) at the
 * right edge, in CSS pixels, while the dialog is open: null until measured, 0 when there
 * is none to spare (a phone) and the dialog covers it.
 */
function useCompanionReserve(companion: HTMLElement | null): number | null {
  const [reserve, setReserve] = React.useState<number | null>(null)
  React.useLayoutEffect(() => {
    if (!companion) return
    const measure = () => setReserve(roomFor(companion))
    measure()
    const observer = typeof ResizeObserver === "function" ? new ResizeObserver(measure) : null
    observer?.observe(companion)
    window.addEventListener("resize", measure)
    return () => {
      observer?.disconnect()
      window.removeEventListener("resize", measure)
      setReserve(null)
    }
  }, [companion])
  return reserve
}

/**
 * A modal dialog over the page, or beside the assistant panel when it is open (#798):
 * then it stops short of the panel, is not `aria-modal`, and leaves the panel's focus
 * and Escape alone. `data-modal` keeps it modal to the agent bridge (agent/dom
 * `activeDialog`) either way. Escape closes only the topmost dialog; Tab stays inside;
 * on closing, focus goes back to what had it as the dialog opened, unless the dialog's
 * own action put it somewhere else (#351). `placement="right"` is a full-height drawer
 * along the right edge.
 */
function DialogLayer({
  className,
  children,
  showCloseButton = true,
  placement = "center",
  overlayClassName,
  onOpenAutoFocus,
  onCloseAutoFocus,
  onEscapeKeyDown,
  onInteractOutside,
  ...props
}: React.ComponentProps<typeof DialogPrimitive.Content> & {
  showCloseButton?: boolean
  placement?: "center" | "right"
  overlayClassName?: string
}) {
  const overlay = React.useRef<HTMLDivElement>(null)
  const companion = React.useContext(ModalCompanionContext)
  const reserve = useCompanionReserve(companion)
  const beside = companion !== null && reserve !== 0
  const content = React.useRef<HTMLDivElement>(null)
  // What had focus as the dialog opened, read while rendering it: by the time any effect
  // runs, a child's `autoFocus` has already moved focus into the dialog.
  const [opener] = React.useState(() => document.activeElement)
  React.useEffect(() => {
    const panel = content.current
    return () => {
      // Back to the opener, unless the dialog's own action already put focus somewhere
      // (another dialog, a page it went to): only focus that fell to <body> with the
      // dialog's removal is brought back. Here rather than in Radix's onCloseAutoFocus,
      // which comes a task later.
      const now = document.activeElement
      const dropped = !now || now === document.body || (panel?.contains(now) ?? false)
      if (dropped && opener instanceof HTMLElement && opener.isConnected) opener.focus()
    }
  }, [opener])
  return (
    <div
      ref={overlay}
      data-slot="dialog-overlay"
      className={cn(
        "fixed inset-0 isolate z-50 flex bg-black/50 duration-100 animate-in fade-in-0 supports-backdrop-filter:backdrop-blur-xs",
        placement === "right" ? "justify-end" : "items-center justify-center p-4",
        overlayClassName
      )}
      style={companion && reserve ? { right: reserve } : undefined}
    >
      <DialogPrimitive.Content
        ref={content}
        data-slot="dialog-content"
        data-placement={placement}
        // Not modal to assistive tech while the companion beside it is usable too (#798).
        aria-modal={beside ? undefined : true}
        // Still modal to the agent bridge (agent/dom activeDialog) either way.
        data-modal=""
        className={cn(
          "relative grid w-full gap-4 bg-popover p-4 text-sm text-popover-foreground ring-1 ring-foreground/10 duration-100 outline-none",
          placement === "right"
            ? "h-full max-w-[480px] rounded-none data-open:animate-in data-open:slide-in-from-right-10"
            : "max-h-full rounded-xl sm:max-w-sm data-open:animate-in data-open:fade-in-0 data-open:zoom-in-95",
          className
        )}
        onOpenAutoFocus={(event) => {
          onOpenAutoFocus?.(event)
          // Left where it is in a companion beside it: the agent may open a dialog while
          // the user types there. Measured now: `reserve` is not yet set.
          if (besideNow(companion) && companion.contains(document.activeElement)) event.preventDefault()
        }}
        onCloseAutoFocus={(event) => {
          onCloseAutoFocus?.(event)
          // Done as the dialog unmounted (above).
          event.preventDefault()
        }}
        onEscapeKeyDown={(event) => {
          onEscapeKeyDown?.(event)
          if (event.defaultPrevented) return
          // The companion's own Escape (closing the assistant) is not this dialog's while
          // it is usable beside it; covered by the dialog, it is no one else's.
          if (besideNow(companion) && companion.contains(event.target as Node)) leaveToCompanion(event)
        }}
        onInteractOutside={(event) => {
          onInteractOutside?.(event)
          if (event.defaultPrevented) return
          // Only a press on its own overlay closes it: not focus or a press in the
          // companion beside it, nor in anything else outside (a toast).
          if (event.type !== "dismissableLayer.pointerDownOutside" || event.target !== overlay.current) {
            event.preventDefault()
          }
        }}
        {...props}
      >
        {children}
        {showCloseButton && (
          <DialogPrimitive.Close data-slot="dialog-close" asChild>
            <Button
              variant="ghost"
              className="absolute top-2 right-2"
              size="icon-sm"
            >
              <XIcon
              />
              <span className="sr-only">Close</span>
            </Button>
          </DialogPrimitive.Close>
        )}
      </DialogPrimitive.Content>
    </div>
  )
}

/** See {@link DialogLayer}; mounted only while open, so it measures and remembers then. */
function DialogContent(props: React.ComponentProps<typeof DialogLayer>) {
  const open = React.useContext(DialogOpenContext)
  return open ? createPortal(<DialogLayer {...props} />, document.body) : null
}

function DialogHeader({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="dialog-header"
      className={cn("flex flex-col gap-2", className)}
      {...props}
    />
  )
}

function DialogFooter({
  className,
  showCloseButton = false,
  children,
  ...props
}: React.ComponentProps<"div"> & {
  showCloseButton?: boolean
}) {
  return (
    <div
      data-slot="dialog-footer"
      className={cn(
        "-mx-4 -mb-4 flex flex-col-reverse gap-2 rounded-b-xl border-t bg-ui-muted/50 p-4 sm:flex-row sm:justify-end",
        className
      )}
      {...props}
    >
      {children}
      {showCloseButton && (
        <DialogPrimitive.Close asChild>
          <Button variant="outline">Close</Button>
        </DialogPrimitive.Close>
      )}
    </div>
  )
}

function DialogTitle({
  className,
  ...props
}: React.ComponentProps<typeof DialogPrimitive.Title>) {
  return (
    <DialogPrimitive.Title
      data-slot="dialog-title"
      className={cn(
        "font-heading text-base leading-none font-medium",
        className
      )}
      {...props}
    />
  )
}

function DialogDescription({
  className,
  ...props
}: React.ComponentProps<typeof DialogPrimitive.Description>) {
  return (
    <DialogPrimitive.Description
      data-slot="dialog-description"
      className={cn(
        "text-sm text-muted-foreground *:[a]:underline *:[a]:underline-offset-3 *:[a]:hover:text-foreground",
        className
      )}
      {...props}
    />
  )
}

export {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogOverlay,
  DialogPortal,
  DialogTitle,
  DialogTrigger,
}
