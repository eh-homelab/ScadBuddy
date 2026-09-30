import { useEffect, useSyncExternalStore } from 'react'
import { api } from '../api/client'
import type { Settings } from '../api/types'

/**
 * The global "show dimensions in" setting. Display only: every API value — the bounding
 * box, the plate sizes, a fit's overshoot — is in millimetres, as OpenSCAD and Bambu
 * Studio work in them, and is converted here at the last moment.
 */
export type DisplayUnit = Settings['display_unit']

export const MM_PER_INCH = 25.4

let current: DisplayUnit = 'mm'
let loaded = false
/** Bumped by every {@link setDisplayUnit}, so a fetch that started earlier cannot undo it. */
let generation = 0
const listeners = new Set<() => void>()

export function getDisplayUnit(): DisplayUnit {
  return current
}

/** The settings page calls this after a save so every open view follows at once. */
export function setDisplayUnit(unit: DisplayUnit): void {
  loaded = true
  generation += 1
  if (unit === current) return
  current = unit
  for (const listener of listeners) listener()
}

/** Tests only: back to millimetres, and the next {@link loadDisplayUnit} fetches again. */
export function resetDisplayUnit(): void {
  setDisplayUnit('mm')
  loaded = false
}

/** Fetches the stored unit once per page load. A failure leaves millimetres showing. */
export function loadDisplayUnit(): void {
  if (loaded) return
  loaded = true
  const started = generation
  api.getSettings().then(
    // A unit set while this was in flight (a Settings save) is newer than what it read.
    (settings) => {
      if (generation === started) setDisplayUnit(settings.display_unit)
    },
    () => {
      loaded = false
    },
  )
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener)
  return () => listeners.delete(listener)
}

export function useDisplayUnit(): DisplayUnit {
  return useSyncExternalStore(subscribe, getDisplayUnit)
}

/** Mounted once, by the app shell. */
export function useLoadDisplayUnit(): void {
  useEffect(loadDisplayUnit, [])
}

/** Millimetres to the display unit, as a bare number: one decimal in mm, two in inches. */
export function length(millimetres: number, unit: DisplayUnit): string {
  return unit === 'in' ? (millimetres / MM_PER_INCH).toFixed(2) : millimetres.toFixed(1)
}

/**
 * A plate's footprint. Millimetres keep the table's own whole numbers (`256 × 256 mm`)
 * rather than {@link length}'s one decimal.
 */
export function plateSize([width, depth]: readonly number[], unit: DisplayUnit): string {
  const show = (value: number) => (unit === 'in' ? length(value, unit) : String(value))
  return `${show(width ?? 0)} × ${show(depth ?? 0)} ${unit}`
}
