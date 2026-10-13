import { useSyncExternalStore } from 'react'
import { Link } from 'react-router'
import { dismissToast, getToasts, holdToast, resumeToast, subscribeToasts } from '../../lib/toast'

export function Toasts() {
  const shown = useSyncExternalStore(subscribeToasts, getToasts)
  return (
    // No aria-live here: each toast is its own role=status, which is already a polite
    // live region, so both would announce it twice.
    <div
      className="pointer-events-none fixed bottom-4 right-4 z-50 flex w-72 max-w-[calc(100vw-2rem)] flex-col gap-2"
    >
      {shown.map((one) => (
        <div
          key={one.id}
          role="status"
          onMouseEnter={() => holdToast(one.id)}
          onMouseLeave={() => resumeToast(one.id)}
          onFocus={() => holdToast(one.id)}
          onBlur={() => resumeToast(one.id)}
          className="pointer-events-auto flex items-start gap-2 rounded-[6px] border border-line bg-surface px-3 py-2 text-[13px] shadow-lg"
        >
          {one.href ? (
            <Link to={one.href} className="min-w-0 flex-1 truncate hover:underline" onClick={() => dismissToast(one.id)}>
              {one.title}
            </Link>
          ) : (
            <span className="min-w-0 flex-1 truncate">{one.title}</span>
          )}
          <button
            type="button"
            aria-label="Dismiss"
            onClick={() => dismissToast(one.id)}
            className="shrink-0 text-muted hover:text-ink"
          >
            ×
          </button>
        </div>
      ))}
    </div>
  )
}
