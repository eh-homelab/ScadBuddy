import { cloneElement, type ComponentProps, type ReactElement, type ReactNode } from 'react'
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from '@/components/ui/tooltip'

/**
 * #3673 — the assistant's hover and focus hints, in place of `title`: a tooltip on the one
 * element it wraps. `wrap` puts a span around it, for a button that can be disabled, which
 * then takes no pointer or focus events of its own and would never show its hint (why it
 * is disabled): the span is focusable while `wrap` is true. Passed at all, the span is
 * there for good, so a button whose disabled state flips is never remounted and keeps
 * focus (review of #3721); while it is not focusable, the hint's `aria-describedby` goes on to
 * the element itself, so a screen reader focusing the button still reads the hint (review
 * of #3727). Its own provider, so the panel and the Settings sections work outside the
 * app shell's (tests, the ui gallery).
 */
export function Tip({
  content,
  children,
  side,
  wrap,
}: {
  content: ReactNode
  children: ReactElement<{ 'aria-describedby'?: string }>
  side?: 'top' | 'right' | 'bottom' | 'left'
  wrap?: boolean | undefined
}) {
  if (content === undefined || content === null || content === '') return children
  return (
    <TooltipProvider delayDuration={400}>
      <Tooltip>
        <TooltipTrigger asChild>
          {wrap !== undefined ? (
            <Wrap focusable={wrap}>{children}</Wrap>
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

/** Tip's span: Radix's trigger props land on it; unfocusable, it hands the description to its child. */
function Wrap({
  focusable,
  children,
  ...props
}: ComponentProps<'span'> & { focusable: boolean; children: ReactElement<{ 'aria-describedby'?: string }> }) {
  return (
    <span
      {...props}
      tabIndex={focusable ? 0 : undefined}
      aria-describedby={focusable ? props['aria-describedby'] : undefined}
      className="inline-flex rounded-lg outline-none focus-visible:ring-3 focus-visible:ring-ring/50"
    >
      {focusable
        ? children
        : cloneElement(children, {
            // The child's own description first (the mic's audio note), then the hint's while it shows.
            'aria-describedby':
              [children.props['aria-describedby'], props['aria-describedby']].filter(Boolean).join(' ') || undefined,
          })}
    </span>
  )
}
