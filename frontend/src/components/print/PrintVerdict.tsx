import { CircleAlertIcon, TriangleAlertIcon } from 'lucide-react'
import { ApiError } from '../../api/client'
import type { PrintCheck } from '../../api/types'
import { Button } from '@/components/ui/button'
import { cn } from '@/lib/utils'

interface Props {
  verdict: PrintCheck | null
  /** The check itself failed; Print is not held, so say the verdict is missing. */
  error?: Error | undefined
  onRetry?: () => void
}

/** A callout, as shadcn's alert draws one: an icon beside the text, in a bordered card. */
const CALLOUT = 'flex items-start gap-2 rounded-lg border px-3 py-2 text-[12.5px] [&>svg]:mt-0.5 [&>svg]:size-4 [&>svg]:shrink-0'
const ERROR = 'border-destructive/40 bg-destructive/10 text-destructive'
const WARNING = 'border-accent/50 bg-accent/10 text-foreground [&>svg]:text-accent'

/**
 * #755, #760 — the run's own refusals and nozzle advisories, shown in Checks before
 * Print. Print is held while an error stands; a warning is advisory.
 */
export function PrintVerdict({ verdict, error, onRetry }: Props) {
  if (error) {
    return (
      <div role="alert" data-testid="print-verdict-failed" className={cn('mt-2', CALLOUT, ERROR)}>
        <CircleAlertIcon aria-hidden="true" />
        <p>
          The check before Print could not run:{' '}
          {error instanceof ApiError ? error.detail : 'ScadBuddy did not answer.'} Print still
          refuses what it cannot print.{' '}
          {onRetry && (
            <Button type="button" variant="link" onClick={onRetry} className="h-auto p-0 text-[12.5px] text-inherit underline">
              Check again
            </Button>
          )}
        </p>
      </div>
    )
  }
  const errors = verdict?.errors ?? []
  const warnings = verdict?.warnings ?? []
  if (errors.length === 0 && warnings.length === 0) return null
  return (
    <ul className="mt-2 space-y-2" aria-label="Before Print">
      {errors.map((message) => (
        <li key={message} role="alert" data-testid="print-verdict-error" className={cn(CALLOUT, ERROR)}>
          <CircleAlertIcon aria-hidden="true" />
          <span>{message}</span>
        </li>
      ))}
      {warnings.map((warning) => (
        <li
          key={`${warning.kind}:${warning.message}`}
          data-testid="print-verdict-warning"
          className={cn(CALLOUT, WARNING)}
        >
          <TriangleAlertIcon aria-hidden="true" />
          <span>{warning.message}</span>
        </li>
      ))}
    </ul>
  )
}
