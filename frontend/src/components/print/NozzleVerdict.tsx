import type { PrintCheck } from '../../api/types'

interface Props {
  verdict: PrintCheck | null
}

/**
 * #755 — the run's nozzle verdict, shown in Checks before Print. An error is what the
 * run would refuse, so Print is held while one stands; a warning is advisory.
 */
export function NozzleVerdict({ verdict }: Props) {
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
