import type { PrinterSyncResult, Settings } from '../api/types'
import type { RealtimeSignal } from './realtime'

/**
 * The `printers` topic carries every registry write (`printer.changed`) and, while a LAN
 * printer is watched, its reports (`printer.report`, about once a second while it prints;
 * `printers/watch.py`). Only a write changes the registry, so only that, or a resync, reads
 * it again: a report must not re-read it each second.
 */
export const PRINTER_TOPICS = ['printers'] as const
export const registryNews = (signal: RealtimeSignal) => signal === 'resync' || signal.kind === 'printer.changed'

/** #4439 — the Bambuddy printer sync's counts, in words. */
export function syncSummary(result: PrinterSyncResult): string {
  const parts = [
    `${result.created} added`,
    `${result.linked} linked`,
    `${result.already_linked} already linked`,
    `${result.skipped} skipped`,
    `${result.conflicts} conflicts`,
  ]
  if (result.lan_linked) parts.push(`${result.lan_linked} LAN links filled`)
  if (result.lan_failed) parts.push(`${result.lan_failed} LAN links not filled`)
  // #4850 review: such a VP was still imported (its action says how); only its target is unknown.
  const missing = result.entries.filter((entry) => entry.target_missing).length
  if (missing) parts.push(`${missing} virtual printers forwarding to a printer not here`)
  return parts.join(', ')
}

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
 * #3818 (plan 2026-10-18 D6) — whether a page with no printer chosen (the Customize page's
 * picker) lists ScadBuddy's own folders rather than Bambuddy's projects: the hot path's
 * backends (`nativeBackends`, the same rule the print dialog applies) and no Bambuddy to
 * hold projects at all. The print dialog decides per printer instead, by the path the
 * server reports for it (`ChoicesView.lan_printers`). With a Bambuddy URL set the page
 * keeps listing projects, and a dialog on a native printer still lists folders of its own.
 */
export function listsNativeFolders(
  settings: Pick<Settings, 'slicer_backend' | 'library_backend' | 'store_backend' | 'bambuddy_url'> | null | undefined,
): boolean {
  return nativeBackends(settings) && !settings?.bambuddy_url
}
