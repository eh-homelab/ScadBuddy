import { ApiError } from '../../api/client'
import type { PrintCheck } from '../../api/types'

interface Props {
  verdict: PrintCheck | null
  /** The check itself failed; Print is not held, so say the verdict is missing. */
  error?: Error | undefined
  onRetry?: () => void
}

/**
 * #755, #760 — the run's own refusals and nozzle advisories, shown in Checks before
 * Print. Print is held while an error stands; a warning is advisory.
 */
export function NozzleVerdict({ verdict, error, onRetry }: Props) {
  if (error) {
    return (
      <p
        role="alert"
        data-testid="nozzle-verdict-failed"
        className="mt-2 rounded-[6px] border border-warn/50 bg-warn/10 px-2.5 py-2 text-[12.5px] text-warn"
      >
        The check before Print could not run:{' '}
        {error instanceof ApiError ? error.detail : 'ScadBuddy did not answer.'} Print still
        refuses what it cannot print.{' '}
        {onRetry && (
          <button type="button" onClick={onRetry} className="underline underline-offset-2">
            Check again
          </button>
        )}
      </p>
    )
  }
  const errors = verdict?.errors ?? []
  const warnings = verdict?.warnings ?? []
  if (errors.length === 0 && warnings.length === 0) return null
  return (
    <ul className="mt-2 space-y-2" aria-label="Nozzles">
      {errors.map((message) => (
        <li
          key={message}
          role="alert"
          data-testid="nozzle-verdict-error"
          className="rounded-[6px] border border-warn/50 bg-warn/10 px-2.5 py-2 text-[12.5px] text-warn"
        >
          {message}
        </li>
      ))}
      {warnings.map((warning) => (
        <li
          key={`${warning.kind}:${warning.message}`}
          data-testid="nozzle-verdict-warning"
          className="rounded-[6px] border border-accent/50 bg-accent/10 px-2.5 py-2 text-[12.5px] text-ink"
        >
          {warning.message}
        </li>
      ))}
    </ul>
  )
}
