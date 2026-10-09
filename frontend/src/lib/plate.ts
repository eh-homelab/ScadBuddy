import type { Job, Param, Plate, PlateFit } from '../api/types'
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
  return [{ plate: null, size: job.bbox_mm.size, colours: coloursPrinted(job) }]
}

/**
 * The filaments a part prints with. `colors` is slot order, and an arranged output can
 * keep a planned slot no part uses (#428), so a slot counts only when a part names its
 * extruder: the two lists are paired by `PartInfo.extruder`, never by index.
 */
function coloursPrinted(job: Job): number {
  const colors = job.colors ?? []
  const parts = job.parts ?? []
  if (parts.length === 0) return colors.length || 1
  const used = new Set(parts.map((part) => part.extruder))
  return colors.filter((_, index) => used.has(index + 1)).length || 1
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

/** A plate's limit on an axis, as `GET /plate/fit` judges it: X and Y where every extruder reaches. */
function plateLimit(plate: Plate, axis: NonNullable<Param['plate_max']>): number {
  if (axis === 'x') return plate.usable.max_x - plate.usable.min_x
  if (axis === 'y') return plate.usable.max_y - plate.usable.min_y
  return plate.height
}

/**
 * #81 — the schema with each `// plate name = x` parameter's max shrunk to the plate in
 * view, so its widget's range is what fits that printer. Never under the parameter's
 * min: a plate too small for even that is the fit warning's to say. The same object
 * back when nothing changes.
 */
export function boundByPlate<S extends { parameters: Param[] }>(schema: S, plate: Plate | undefined): S {
  if (!plate || !schema.parameters.some((param) => param.plate_max)) return schema
  return {
    ...schema,
    parameters: schema.parameters.map((param) => {
      if (!param.plate_max) return param
      const limit = Math.max(plateLimit(plate, param.plate_max), param.min ?? -Infinity)
      return param.max != null && param.max <= limit ? param : { ...param, max: limit }
    }),
  }
}
