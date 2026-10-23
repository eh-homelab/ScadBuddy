import type { PrinterRow, Settings } from '../api/types'
import type { RealtimeSignal } from './realtime'

/**
 * The `printers` topic carries every registry write (`printer.changed`) and, while a LAN
 * printer is watched, its reports (`printer.report`, about once a second while it prints;
 * `printers/watch.py`). Only a write changes the registry, so only that, or a resync, reads
 * it again: a report must not re-read it each second.
 */
export const PRINTER_TOPICS = ['printers'] as const
export const registryNews = (signal: RealtimeSignal) => signal === 'resync' || signal.kind === 'printer.changed'

/**
 * #3818 — the backends the hot path needs (plan 2026-10-18 D2, `hot_path_native`): the
 * native slicer and library, on a store that is not the workers' local disk. Anything
 * else prints through Bambuddy as it always has.
 */
export function nativeBackends(
  settings: Pick<Settings, 'slicer_backend' | 'library_backend' | 'store_backend'> | null | undefined,
): boolean {
  return (
    settings?.slicer_backend === 'native' &&
    settings.library_backend === 'native' &&
    (settings.store_backend === 's3' || settings.store_backend === 'bambuddy')
  )
}

/**
 * #3818 — a row whose path is the LAN (plan 2026-10-16 Task 8's resolution, as far as the
 * row tells it): linked over the LAN with its access code, and either asked to print
 * natively or with no Bambuddy printer to print through. `auto` with both links is the
 * backend's call on the report's age, so it is left to the Bambuddy path here.
 */
export function printsNatively(row: PrinterRow): boolean {
  if (row.lan_host == null || !row.has_access_code) return false
  return row.preferred_path === 'native' || (row.preferred_path === 'auto' && row.bambuddy_printer_id == null)
}
