import type { PlateFit } from '../api/types'
import { mm } from './format'

/**
 * #81 — what `GET /plate/fit` found, as sentences. The judging is the server's: it runs
 * the placement the send runs, so an answer of "fits" here is the send's answer too —
 * prime-tower room and the filament cutter included, not only the axes.
 */
export function fitMessages(fit: PlateFit): string[] {
  const target = fit.plate.model ? `the ${fit.plate.name}` : 'the default plate'
  const axes = fit.overshoots.map(
    (over) =>
      `${over.axis} is ${mm(over.size - over.limit)} mm over ${target} (${mm(over.size)} of ${mm(over.limit)} mm)`,
  )
  return fit.problem ? [...axes, fit.problem] : axes
}

/** The Print button's short form, or `null` when the model fits. */
export function fitLabel(fit: PlateFit): string | null {
  if (fit.overshoots.length > 0) {
    return `Too big on ${fit.overshoots.map((over) => over.axis).join(', ')}`
  }
  return fit.problem ? 'Does not fit' : null
}
