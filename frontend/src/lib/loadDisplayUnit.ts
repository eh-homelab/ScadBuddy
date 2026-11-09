import { useEffect } from 'react'
import { api } from '../api/client'
import { failDisplayUnitLoad, finishDisplayUnitLoad, startDisplayUnitLoad } from './units'

/**
 * Fetches the stored unit (`lib/units.ts`) once per page load; a failure leaves millimetres
 * showing. The back office only: it calls the back office's API (`api/client`), which the
 * public storefront page must not bundle (spec 2026-11-02 §5.1).
 */
export function loadDisplayUnit(): void {
  const started = startDisplayUnitLoad()
  if (started === null) return
  api.getSettings().then(
    (settings) => finishDisplayUnitLoad(started, settings.display_unit),
    () => failDisplayUnitLoad(),
  )
}

/** Mounted once, by the app shell. */
export function useLoadDisplayUnit(): void {
  useEffect(loadDisplayUnit, [])
}
