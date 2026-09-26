import { useEffect, useRef, type ReactNode } from 'react'

interface Props {
  open: boolean
  title: string
  description?: string
  onClose: () => void
  children: ReactNode
  footer?: ReactNode
}

export function Dialog({ open, title, description, onClose, children, footer }: Props) {
  const panelRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!open) return
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [open, onClose])

  // On opening only. Callers pass a fresh `onClose` every render, and refocusing the
  // panel with it would pull focus out of a text field after its first keystroke.
  useEffect(() => {
    if (open) panelRef.current?.focus()
  }, [open])

  if (!open) return null

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/55 p-4"
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
        aria-modal="true"
        aria-label={title}
        tabIndex={-1}
        className="flex max-h-[calc(100vh-2rem)] w-full max-w-lg flex-col rounded-lg border border-line bg-surface shadow-2xl outline-none"
      >
        <header className="shrink-0 border-b border-line px-5 py-3.5">
          <h2 className="text-[15px] font-semibold">{title}</h2>
          {description && <p className="mt-1 text-[13px] text-muted">{description}</p>}
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
