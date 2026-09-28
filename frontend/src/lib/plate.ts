import type { Job, PlateFit } from '../api/types'
import { length, type DisplayUnit } from './units'

/**
 * #81 — what `GET /plate/fit` found, as sentences. The judging is the server's: it runs
 * the placement the send runs, so an answer of "fits" here is the send's answer too —
 * prime-tower room and the filament cutter included, not only the axes.
 */
export function fitMessages(fit: PlateFit, unit: DisplayUnit = 'mm'): string[] {
  const target = fit.plate.model ? `the ${fit.plate.name}` : 'the default plate'
  const axes = fit.overshoots.map(
    (over) =>
      `${over.axis} is ${length(over.size - over.limit, unit)} ${unit} over ${target} (${length(over.size, unit)} of ${length(over.limit, unit)} ${unit})`,
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

/** One box to check against the printer: a plate of a multi-plate render, or the model. */
export interface FitTarget {
  /** The plate's index, or `null` for a one-plate render. */
  plate: number | null
  size: number[]
  colours: number
}

/**
 * #289 — what the customizer checks against the printer. A template that asks for more
 * than one plate (spec §6.4) is checked plate by plate, each with its own box and its own
 * colours (a plate of one colour needs no prime tower); anything else is the one model.
 */
export function fitTargets(job: Job | undefined): FitTarget[] {
  if (job?.status !== 'done' || !job.bbox_mm) return []
  const plates = job.plates ?? []
  if (plates.length > 1) {
    return plates.map((plate) => ({
      plate: plate.index,
      size: plate.bbox_mm.size,
      colours: plate.colors.length,
    }))
  }
  return [{ plate: null, size: job.bbox_mm.size, colours: job.colors?.length ?? 1 }]
}

/** Every plate's fit problems, each named by its plate when there is more than one. */
export function platesFitMessages(fits: PlateFit[], targets: FitTarget[], unit: DisplayUnit = 'mm'): string[] {
  return fits.flatMap((fit, index) => {
    const plate = targets[index]?.plate
    return fitMessages(fit, unit).map((message) => (plate == null ? message : `plate ${plate}: ${message}`))
  })
}

/** The fit the Print button reports: the first plate that does not fit, else the first. */
export function worstFit(fits: PlateFit[]): PlateFit | undefined {
  return fits.find((fit) => fitLabel(fit) !== null) ?? fits[0]
}
