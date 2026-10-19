import type { RealtimeSignal } from './realtime'

/**
 * The `printers` topic carries every registry write (`printer.changed`) and, while a LAN
 * printer is watched, its reports (`printer.report`, about once a second while it prints;
 * `printers/watch.py`). Only a write changes the registry, so only that, or a resync, reads
 * it again: a report must not re-read it each second.
 */
export const PRINTER_TOPICS = ['printers'] as const
export const registryNews = (signal: RealtimeSignal) => signal === 'resync' || signal.kind === 'printer.changed'
