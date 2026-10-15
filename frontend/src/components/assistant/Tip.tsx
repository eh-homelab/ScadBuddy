import type { ReactElement, ReactNode } from 'react'
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from '@/components/ui/tooltip'

/**
 * #3673 — the assistant's hover and focus hints, in place of `title`: a tooltip on the one
 * element it wraps. `wrap` puts a focusable span around it, for a disabled button, which
 * takes no pointer or focus events of its own and would never show its hint (why it is
 * disabled). Its own provider, so the panel and the Settings sections work outside the
 * app shell's (tests, the ui gallery).
 */
export function Tip({
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
  if (content === undefined || content === null || content === '') return children
  return (
    <TooltipProvider delayDuration={400}>
      <Tooltip>
        <TooltipTrigger asChild>
          {wrap ? (
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
