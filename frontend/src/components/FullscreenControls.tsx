import type { Ref } from 'react'
import { Maximize2Icon, Minimize2Icon, SlidersVerticalIcon, XIcon } from 'lucide-react'
import { Button } from '@/components/ui/button'

/*
 * The full-screen view's controls, whose state the page owns. `Preview` lays the two
 * buttons over the scene; they live apart from it because it loads lazily, with
 * three.js.
 */

/** Over the scene: readable on whatever the model looks like behind it. */
const OVERLAY = 'pointer-events-auto bg-background/90 backdrop-blur-sm'

// #3653 — a `title`, not a Radix tooltip: an open tooltip takes the first Escape for
// itself, and here Escape closes the flyout and leaves full screen (useFullscreen).

export function FullscreenButton({ active, onClick }: { active: boolean; onClick: () => void }) {
  const label = active ? 'Exit full screen' : 'Full screen'
  return (
    <Button
      variant="outline"
      size="icon-sm"
      onClick={onClick}
      aria-label={label}
      title={active ? `${label} (Esc)` : label}
      className={OVERLAY}
    >
      {/* Corners pointing out to enter, in to leave. */}
      {active ? <Minimize2Icon aria-hidden="true" /> : <Maximize2Icon aria-hidden="true" />}
    </Button>
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
    <Button
      ref={ref}
      variant="outline"
      size="sm"
      onClick={onClick}
      aria-expanded={open}
      aria-controls={flyout}
      className={OVERLAY}
    >
      <SlidersVerticalIcon aria-hidden="true" />
      Parameters
    </Button>
  )
}

/** The top of the parameters flyout: what it is, and the way out of it. */
export function FlyoutHeader({ onClose, ref }: { onClose: () => void; ref?: Ref<HTMLButtonElement> }) {
  return (
    <div className="flex shrink-0 items-center justify-between border-b py-1 pr-1.5 pl-3">
      <h2 className="text-[13px] font-medium">Parameters</h2>
      <Button
        ref={ref}
        variant="ghost"
        size="icon-sm"
        onClick={onClose}
        aria-label="Close parameters"
        title="Close parameters"
      >
        <XIcon aria-hidden="true" />
      </Button>
    </div>
  )
}
