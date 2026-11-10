import type { PrinterNow, PrintSummary, QueueItem } from '../../api/types'
import { formatDuration } from '../../lib/format'
import type { NowCard } from './nowCards'

/** Pending items listed before the rest are left to Bambuddy's queue page. */
export const QUEUE_SHOWN = 8

/**
 * #3745 — a print that has no outcome yet, as a row of the Prints list: a printer's
 * working job (printing or paused), or a Bambuddy queue item still to print.
 *
 * One identity per print, `key`, from what the page's reads already carry:
 * - `archive:<id>` once the print has an archive: a printer's `archive_id` (Bambuddy's
 *   `current_archive_id`), else the archive ScadBuddy linked to the job (#3741
 *   `scadbuddy.archive_id`); for a queue item only that linked archive, since a pending
 *   reprint's own `archive_id` names the archive it reprints, a finished print.
 * - else `queue:<item id>` for a queue item, and `printer:<registry id>` for a job with
 *   no archive yet (a LAN start before ScadBuddy linked it).
 * A history row is `archive:<archive_id>`, and names its queue item (`queue_item_id`).
 */
export type LiveJob =
  | { kind: 'working'; key: string; card: NowCard }
  | { kind: 'queued'; key: string; item: QueueItem; place: number }

function working(printer: PrinterNow): boolean {
  return printer.activity === 'printing' || printer.activity === 'paused'
}

function archiveKey(id: number | null | undefined): string | null {
  return id == null ? null : `archive:${id}`
}

/** Bambuddy's queue order, items with no position last, then by id (#2919). */
function byPosition(a: QueueItem, b: QueueItem): number {
  const [pa, pb] = [a.position ?? Infinity, b.position ?? Infinity]
  if (pa !== pb) return pa < pb ? -1 : 1
  return a.id - b.id
}

/**
 * The jobs still without an outcome: every working printer's job, in the printers'
 * order, then the first {@link QUEUE_SHOWN} pending items in Bambuddy's order. A queued
 * item whose linked archive a printer already prints is that printer's job: the printers
 * are read more often than the queue, so the printer's is the newer copy.
 */
export function liveJobs(cards: readonly NowCard[], queue: readonly QueueItem[]): LiveJob[] {
  const jobs: LiveJob[] = []
  const keys = new Set<string>()
  for (const card of cards) {
    const { printer } = card
    if (!working(printer)) continue
    const key = archiveKey(printer.archive_id ?? printer.scadbuddy?.archive_id) ?? `printer:${printer.id}`
    if (keys.has(key)) continue
    keys.add(key)
    jobs.push({ kind: 'working', key, card })
  }
  const pending = queue.filter((item) => item.status === 'pending').sort(byPosition)
  pending.slice(0, QUEUE_SHOWN).forEach((item, index) => {
    const key = archiveKey(item.scadbuddy?.archive_id) ?? `queue:${item.id}`
    if (keys.has(key)) return
    keys.add(key)
    jobs.push({ kind: 'queued', key, item, place: index + 1 })
  })
  return jobs
}

/**
 * #5940 — a working job as `usePrintQueue`'s `started` names it: the registry printer
 * it is on, and its {@link LiveJob} key. #5946 — a pair, never one string, so no
 * character in a printer's id can be taken for the line between the two.
 */
export interface StartedJob {
  printerId: string
  key: string
}

export function startedOn(printerId: string, key: string): StartedJob {
  return { printerId, key }
}

/**
 * #3745, #5940 — whether a printers read shows a job that started since the read
 * `before`: a key a printer did not have then. A job that ended is no start. Nor is a
 * job that had no archive on its printer then (`printer:<id>`) and has one now:
 * ScadBuddy linked it, so it is the same job. A job that ended and another that started
 * with an archive between two reads of the printers looks the same, and its queue item
 * then leaves at the queue's next poll.
 */
export function newStart(now: readonly StartedJob[], before: readonly StartedJob[]): boolean {
  const was = new Map(before.map((job) => [job.printerId, job.key]))
  return now.some(({ printerId, key }) => {
    const last = was.get(printerId)
    if (last === key) return false
    const unlinked = `printer:${printerId}`
    return !(last === unlinked && key !== unlinked)
  })
}

/** How many pending items {@link liveJobs} leaves to Bambuddy's queue page. */
export function queueTail(queue: readonly QueueItem[]): number {
  return Math.max(0, queue.filter((item) => item.status === 'pending').length - QUEUE_SHOWN)
}

/**
 * The job's state in words, never colour alone: "Queued · position 3", "Printing 66% ·
 * 54m left", "Paused: <its first HMS error>" (or "Paused 32%" with none).
 */
export function liveLabel(job: LiveJob): string {
  if (job.kind === 'queued') return `Queued · position ${job.place}`
  const { printer } = job.card
  const progress = printer.progress == null ? null : `${Math.round(Math.max(0, Math.min(100, printer.progress)))}%`
  if (printer.activity === 'paused') {
    const hms = printer.hms?.[0]
    if (hms) return `Paused: ${hms.description ?? `HMS ${hms.code}`}`
    return progress ? `Paused ${progress}` : 'Paused'
  }
  const head = progress ? `Printing ${progress}` : 'Printing'
  const left = printer.remaining_minutes != null && printer.remaining_minutes > 0
  return left ? `${head} · ${formatDuration(printer.remaining_minutes! * 60)} left` : head
}

/** A row on top of the list: a live job, with its archived print once the history has it. */
export interface LiveRow {
  key: string
  live: LiveJob
  print: PrintSummary | null
}

/** A history row, with the live state of its working job when it has one. */
export interface HistoryRow {
  print: PrintSummary
  live: LiveJob | null
}

/**
 * #3745 — the one list: each print once. On the unfiltered list the live jobs come
 * first (working, then queued), each with its print when the loaded history has it,
 * and the history follows without those prints. A queued item the history already has
 * (by `queue_item_id`) has started since the queue was read, so the history's row
 * stands for it. A filter narrows the archived prints only: no live row is added, and a
 * listed print that is a working job shows its live state where it is.
 */
export function printRows(
  jobs: readonly LiveJob[],
  prints: readonly PrintSummary[],
  filtered: boolean,
): { top: LiveRow[]; rest: HistoryRow[] } {
  const byArchive = new Map(prints.map((print) => [`archive:${print.archive_id}`, print]))
  const started = new Set(prints.flatMap((print) => (print.queue_item_id == null ? [] : [print.queue_item_id])))
  const shown = jobs.filter((job) => job.kind === 'working' || !started.has(job.item.id))
  if (filtered) {
    const live = new Map(shown.filter((job) => job.kind === 'working').map((job) => [job.key, job]))
    return {
      top: [],
      rest: prints.map((print) => ({ print, live: live.get(`archive:${print.archive_id}`) ?? null })),
    }
  }
  const top = shown.map((live) => ({ key: live.key, live, print: byArchive.get(live.key) ?? null }))
  const lifted = new Set(top.flatMap((row) => (row.print ? [row.print.archive_id] : [])))
  return {
    top,
    rest: prints.filter((print) => !lifted.has(print.archive_id)).map((print) => ({ print, live: null })),
  }
}
