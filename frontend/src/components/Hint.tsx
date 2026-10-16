import type { ReactElement, ReactNode } from 'react'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'

/**
 * A tooltip on `children` (#3672), in place of a `title`: none shown when `content` is
 * empty. Without `wrap` the tree is the same either way, so a child that must stay
 * mounted (a live region, #351) is not remounted when its hint comes and goes. `Tooltip`
 * brings its own provider, so a control renders the same outside AppShell (a test, a
 * template's shadow root). A disabled button gets no pointer events, so pass `wrap` to
 * hang the tooltip on a focusable span around it instead, there only while there is a
 * hint.
 */
export function Hint({
  content,
  children,
  side,
  wrap = false,
}: {
  content: ReactNode
  children: ReactElement
  side?: 'top' | 'right' | 'bottom' | 'left'
  wrap?: boolean
}) {
  const empty = content === null || content === undefined || content === false || content === ''
  return (
    <Tooltip {...(empty ? { open: false } : {})}>
      <TooltipTrigger asChild>
        {wrap && !empty ? (
          // The disabled control's reason has to be reachable by keyboard too.
          <span
            tabIndex={0}
            className="inline-flex rounded-lg outline-none focus-visible:ring-3 focus-visible:ring-ring/50"
          >
            {children}
          </span>
        ) : (
          children
        )}
      </TooltipTrigger>
      {empty ? null : (
        <TooltipContent side={side} className="whitespace-pre-line">
          {content}
        </TooltipContent>
      )}
    </Tooltip>
  )
}
