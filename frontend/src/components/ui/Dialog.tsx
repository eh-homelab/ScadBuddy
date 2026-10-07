import {
  useContext,
  useEffect,
  useId,
  useLayoutEffect,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
  type ReactNode,
} from 'react'
import { ModalCompanionContext, focusInto, focusables } from '../../lib/modal'

interface Props {
  open: boolean
  title: string
  description?: string
  onClose: () => void
  children: ReactNode
  footer?: ReactNode
}

/** Whether `panel` is the topmost open dialog: a dialog opened from inside another comes later. */
function topmost(panel: HTMLElement | null): boolean {
  const dialogs = document.querySelectorAll('[role="dialog"]')
  return dialogs[dialogs.length - 1] === panel
}

/** The narrowest a dialog's overlay gets beside a companion; narrower, the dialog covers it. */
const MIN_BESIDE = 360

/**
 * The room to keep for `companion` at the right edge, in CSS pixels: 0 when there is
 * none to spare (a phone), so the dialog covers it; null when it is not laid out
 * (jsdom), where it counts as beside.
 */
function roomFor(companion: HTMLElement): number | null {
  const { left, width } = companion.getBoundingClientRect()
  if (width === 0) return null
  return left >= MIN_BESIDE ? window.innerWidth - left : 0
}

/** Whether `companion` is usable beside the dialog, measured now. */
function besideNow(companion: HTMLElement | null): companion is HTMLElement {
  return companion !== null && roomFor(companion) !== 0
}

/**
 * A modal dialog. #351 — on opening, focus goes to the first focusable element (an
 * `autoFocus` one keeps it), falling back to the panel; Tab and Shift+Tab stay inside;
 * and on closing, focus goes back to whatever had it before the dialog opened.
 */
export function Dialog({ open, title, description, onClose, children, footer }: Props) {
  const panelRef = useRef<HTMLDivElement>(null)
  const descriptionId = useId()
  const companion = useContext(ModalCompanionContext)
  // #798 — room kept for the companion at the right edge, in CSS pixels; `null` until
  // measured. With no room to spare (a phone), the dialog covers it as before.
  const [reserve, setReserve] = useState<number | null>(null)
  useLayoutEffect(() => {
    if (!open || !companion) return
    const measure = () => setReserve(roomFor(companion))
    measure()
    const observer = typeof ResizeObserver === 'function' ? new ResizeObserver(measure) : null
    observer?.observe(companion)
    window.addEventListener('resize', measure)
    return () => {
      observer?.disconnect()
      window.removeEventListener('resize', measure)
      setReserve(null)
    }
  }, [open, companion])
  const beside = companion !== null && reserve !== 0

  // What had focus as the dialog opened, read while rendering it: by the time any effect
  // runs, a child's `autoFocus` has already moved focus into the dialog.
  const [opener, setOpener] = useState<Element | null>(() => (open ? document.activeElement : null))
  const [wasOpen, setWasOpen] = useState(open)
  if (open !== wasOpen) {
    setWasOpen(open)
    if (open) setOpener(document.activeElement)
  }

  useEffect(() => {
    if (!open) return
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return
      // The companion's own Escape (closing the assistant) is not this dialog's, while it
      // is usable beside it; covered by the dialog, it is no one else's.
      if (besideNow(companion) && companion.contains(event.target as Node)) return
      // Only the topmost modal closes: a dialog opened from inside another (Duplicate
      // from a built-in's Media, #279) comes later in the document, and the one under
      // it stays open. The outer listener runs first, so `defaultPrevented` cannot tell.
      if (!topmost(panelRef.current)) return
      // Taken: the full-screen view (useFullscreen) must not leave on the same key.
      event.preventDefault()
      onClose()
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [open, onClose, companion])

  // On opening only. Callers pass a fresh `onClose` every render, and refocusing with it
  // would pull focus out of a text field after its first keystroke.
  useEffect(() => {
    const panel = panelRef.current
    if (!open || !panel) return
    // Left where it is in a companion beside it: the agent may open a dialog while the
    // user types. Measured here, not read from `reserve`: this runs before that is set.
    const at = document.activeElement
    if (!panel.contains(at) && !(besideNow(companion) && companion.contains(at))) focusInto(panel)
    return () => {
      // Back to the opener, unless the dialog's own action already put focus somewhere
      // (another dialog, a page it went to): only focus that fell to <body> with the
      // dialog's removal is brought back.
      const now = document.activeElement
      const dropped = !now || now === document.body || panel.contains(now)
      if (dropped && opener instanceof HTMLElement && opener.isConnected) opener.focus()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- on opening only, as above
  }, [open, opener])

  const trapTab = (event: ReactKeyboardEvent) => {
    const panel = panelRef.current
    if (event.key !== 'Tab' || !panel || !topmost(panel)) return
    const all = focusables(panel)
    const first = all[0]
    const last = all.at(-1)
    if (!first || !last) {
      event.preventDefault()
      return
    }
    const at = document.activeElement
    if (event.shiftKey && (at === first || at === panel)) {
      event.preventDefault()
      last.focus()
    } else if (!event.shiftKey && at === last) {
      event.preventDefault()
      first.focus()
    }
  }

  if (!open) return null

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/55 p-4"
      style={companion && reserve ? { right: reserve } : undefined}
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose()
      }}
    >
      {/* Bounded, with the body scrolling, so the title and the buttons stay reachable
          however tall the content grows. The print picker's filament step (#87) is the
          first content to exceed a short viewport, and without this the Run button sits
          off screen with nothing to scroll it into view. */}
      <div
        ref={panelRef}
        role="dialog"
        // Not modal to assistive tech while the companion beside it is usable too (#798).
        aria-modal={beside ? undefined : 'true'}
        // Still modal to the agent bridge (agent/dom activeDialog) either way.
        data-modal=""
        aria-label={title}
        aria-describedby={description ? descriptionId : undefined}
        tabIndex={-1}
        onKeyDown={trapTab}
        className="flex max-h-[calc(100vh-2rem)] w-full max-w-lg flex-col rounded-lg border border-line bg-surface shadow-2xl outline-none"
      >
        <header className="shrink-0 border-b border-line px-5 py-3.5">
          <h2 className="text-[15px] font-semibold">{title}</h2>
          {description && (
            <p id={descriptionId} className="mt-1 text-[13px] text-muted">
              {description}
            </p>
          )}
        </header>
        <div className="overflow-y-auto px-5 py-4">{children}</div>
        {footer && (
          <footer className="flex shrink-0 items-center justify-end gap-2 border-t border-line px-5 py-3">
            {footer}
          </footer>
        )}
      </div>
    </div>
  )
}
