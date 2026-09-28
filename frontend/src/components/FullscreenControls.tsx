import type { Ref } from 'react'

/*
 * The full-screen view's controls, whose state the page owns. `Preview` lays the two
 * buttons over the scene; they live apart from it because it loads lazily, with
 * three.js.
 */

const OVERLAY_BUTTON =
  'pointer-events-auto flex h-7 items-center justify-center rounded-[6px] border border-line bg-surface/90 text-muted backdrop-blur-sm transition-colors hover:border-line-strong hover:text-ink'

const ICON = {
  viewBox: '0 0 16 16',
  'aria-hidden': true,
  className: 'size-3.5 shrink-0',
  fill: 'none',
  stroke: 'currentColor',
  strokeWidth: 1.5,
  strokeLinecap: 'round',
  strokeLinejoin: 'round',
} as const

export function FullscreenButton({ active, onClick }: { active: boolean; onClick: () => void }) {
  const label = active ? 'Exit full screen' : 'Full screen'
  return (
    <button
      type="button"
      onClick={onClick}
      aria-label={label}
      title={active ? `${label} (Esc)` : label}
      className={`${OVERLAY_BUTTON} w-7`}
    >
      <svg {...ICON}>
        {/* Corners pointing out to enter, in to leave. */}
        <path
          d={
            active
              ? 'M6 2v4H2M10 2v4h4M6 14v-4H2M10 14v-4h4'
              : 'M2 6V2h4M14 6V2h-4M2 10v4h4M14 10v4h-4'
          }
        />
      </svg>
    </button>
  )
}

/** Opens the parameters flyout, which covers it while open. */
export function ParametersButton({
  open,
  flyout,
  onClick,
  ref,
}: {
  open: boolean
  /** The flyout's id. */
  flyout: string
  onClick: () => void
  ref?: Ref<HTMLButtonElement>
}) {
  return (
    <button
      ref={ref}
      type="button"
      onClick={onClick}
      aria-expanded={open}
      aria-controls={flyout}
      className={`${OVERLAY_BUTTON} gap-1.5 px-2 text-[12px]`}
    >
      <svg {...ICON}>
        {/* Three sliders. */}
        <path d="M4 2v12M8 2v12M12 2v12M2.5 10h3M6.5 5h3M10.5 8h3" />
      </svg>
      Parameters
    </button>
  )
}

/** The top of the parameters flyout: what it is, and the way out of it. */
export function FlyoutHeader({ onClose, ref }: { onClose: () => void; ref?: Ref<HTMLButtonElement> }) {
  return (
    <div className="flex shrink-0 items-center justify-between border-b border-line py-1 pr-1.5 pl-3">
      <h2 className="text-[13px] font-medium">Parameters</h2>
      <button
        ref={ref}
        type="button"
        onClick={onClose}
        aria-label="Close parameters"
        title="Close parameters"
        className="flex size-7 items-center justify-center rounded-[6px] text-muted transition-colors hover:bg-surface-2 hover:text-ink"
      >
        <svg {...ICON}>
          <path d="M4 4l8 8M12 4l-8 8" />
        </svg>
      </button>
    </div>
  )
}
