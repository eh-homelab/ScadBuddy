import type { PrintDetail, PrintSummary, ScadBuddyJob } from '../../api/types'
import { editPath } from '../../lib/deeplink'
import type { Slide } from '../media/slides'

/** The print detail page (#311). */
export function printPath(archiveId: number): string {
  return `/prints/${archiveId}`
}

/** #3744 — the page of one Bambuddy queue item. */
export function queueItemPath(itemId: number): string {
  return `/prints/queue/${itemId}`
}

/**
 * #3743 — the ScadBuddy page of a job ScadBuddy queued or started: its print once
 * ScadBuddy linked its archive; else, for a queue item, the item's page (#3744); else
 * (a printer's job with no queue item, a LAN start) its subject's page: the output's,
 * or the library file's in the Library.
 */
export function scadbuddyJobPath(job: ScadBuddyJob, queueItemId?: number): string {
  if (job.archive_id != null) return printPath(job.archive_id)
  if (queueItemId != null) return queueItemPath(queueItemId)
  if (job.output_id != null) return editPath(job.output_id)
  if (job.library_file_id != null) return `/library?file=${job.library_file_id}`
  // A subject names an output or a library file; this is for a key of neither.
  return '/prints'
}

/** #1755 — the history of one Bambuddy library file's prints. */
export function libraryPrintsPath(fileId: number): string {
  return `/prints?file=${fileId}`
}

/**
 * #6041 — the Activity list of a model's prints from LAN-only printers: each is a
 * `print_auto_import` row with the model as its subject. They have no Bambuddy archive, so
 * they are not on the model's Prints tab.
 */
export function lanPrintsActivityPath(slug: string): string {
  return `/activity?${new URLSearchParams({ kind: 'print_auto_import', subject: `model:${slug}` }).toString()}`
}

/** A printer Bambuddy gave no name for. */
export function printerLabel(id: number): string {
  return `Printer ${id}`
}

/** What a history item names a print by: its output's name, else its archive. */
export function printLabel(print: Pick<PrintSummary, 'archive_id' | 'output_name'>): string {
  return print.output_name ?? `Print #${print.archive_id}`
}

/**
 * The one slide a history item can show before the print's detail has loaded: the cover
 * whole, not the card's small copy of it (#2355).
 */
export function coverSlides(print: PrintSummary): Slide[] {
  if (!print.cover) return []
  return [{ key: 'cover', kind: 'image', src: print.cover.full_url, alt: printLabel(print) }]
}

/**
 * #310 — a print's media for the lightbox (#275): the finish photo, the other photos,
 * the timelapse, ScadBuddy-side attachments (#309), then the plate images. A print
 * with none of those keeps its cover.
 */
export function printSlides(print: PrintDetail): Slide[] {
  const label = printLabel(print)
  const { media } = print
  const slides: Slide[] = []
  if (media.finish_photo) {
    slides.push({
      key: `photo:${media.finish_photo.name}`,
      kind: 'image',
      src: media.finish_photo.url,
      alt: `${label}, finish photo`,
      caption: 'Finish photo',
    })
  }
  for (const photo of media.photos) {
    slides.push({ key: `photo:${photo.name}`, kind: 'image', src: photo.url, alt: `${label}, photo` })
  }
  if (media.timelapse) {
    slides.push({
      key: 'timelapse',
      kind: 'video',
      src: media.timelapse.url,
      poster: media.timelapse.poster_url,
      alt: `${label}, timelapse`,
      caption: 'Timelapse',
      contentType: 'video/mp4',
    })
  }
  for (const attachment of media.attachments) {
    slides.push({
      key: `attachment:${attachment.id}`,
      kind: attachment.kind === 'video' ? 'video' : 'image',
      src: attachment.url,
      alt: attachment.caption ?? `${label}, ${attachment.kind}`,
      caption: attachment.caption ?? undefined,
    })
  }
  for (const plate of media.plate_thumbnails) {
    slides.push({
      key: `plate:${plate.index}`,
      kind: 'image',
      src: plate.url,
      alt: `${label}, ${plate.name ?? `plate ${plate.index}`}`,
      // #2055 — by what the plate holds, as the print dialog names it.
      caption: plate.name ?? `Plate ${plate.index}`,
    })
  }
  return slides.length > 0 ? slides : coverSlides(print)
}
