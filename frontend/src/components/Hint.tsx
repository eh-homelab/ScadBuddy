import type { ReactElement, ReactNode } from 'react'
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from '@/components/ui/tooltip'

/**
 * A tooltip on `children` (#3672), in place of a `title`: nothing when `content` is
 * empty. It brings its own provider, so a control renders the same outside AppShell (a
 * test, a template's shadow root). A disabled button gets no pointer events, so pass
 * `wrap` to hang the tooltip on a focusable span around it instead.
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
  if (content === null || content === undefined || content === false || content === '') return children
  return (
    <TooltipProvider>
      <Tooltip>
        <TooltipTrigger asChild>
          {wrap ? (
            // The disabled control's reason has to be reachable by keyboard too.
            <span tabIndex={0} className="inline-flex rounded-lg outline-none focus-visible:ring-3 focus-visible:ring-ring/50">
              {children}
            </span>
          ) : (
            children
          )}
        </TooltipTrigger>
        <TooltipContent side={side} className="whitespace-pre-line">
          {content}
        </TooltipContent>
      </Tooltip>
    </TooltipProvider>
  )
}
