import type { PrintDetail, PrintSummary } from '../../api/types'
import type { Slide } from '../media/slides'

/** The print detail page (#311). */
export function printPath(archiveId: number): string {
  return `/prints/${archiveId}`
}

/** A printer Bambuddy gave no name for. */
export function printerLabel(id: number): string {
  return `Printer ${id}`
}

/** What a history item names a print by: its output's name, else its archive. */
export function printLabel(print: Pick<PrintSummary, 'archive_id' | 'output_name'>): string {
  return print.output_name ?? `Print #${print.archive_id}`
}

/** The one slide a history item can show before the print's detail has loaded. */
export function coverSlides(print: PrintSummary): Slide[] {
  if (!print.cover) return []
  return [{ key: 'cover', kind: 'image', src: print.cover.url, alt: printLabel(print) }]
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
      poster: media.timelapse.poster_frames[0]?.data_url,
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
      alt: `${label}, plate ${plate.index}`,
      caption: `Plate ${plate.index}`,
    })
  }
  return slides.length > 0 ? slides : coverSlides(print)
}
