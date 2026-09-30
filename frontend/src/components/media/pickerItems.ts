import { api } from '../../api/client'
import type { MediaView } from '../../api/types'
import type { PickerItem } from './MediaPicker'

export const MEDIA_KEY = 'media:'

/**
 * A template's media as picker items, in order, the first marked as the cover. Missing
 * files are left out, and videos too unless `videos` (then shown by their poster).
 */
export function mediaPickerItems(
  slug: string,
  media: MediaView[] | undefined,
  { videos = false }: { videos?: boolean } = {},
): PickerItem[] {
  return (media ?? []).flatMap((item, index) => {
    if (item.missing) return []
    if (item.kind === 'video' && !videos) return []
    const src = item.kind === 'image' ? api.mediaUrl(slug, item) : api.mediaPosterUrl(slug, item)
    return [
      {
        key: `${MEDIA_KEY}${item.id}`,
        src,
        label: item.caption || (item.kind === 'video' ? `Video ${index + 1}` : `Image ${index + 1}`),
        badge: index === 0 ? 'Cover' : undefined,
      },
    ]
  })
}

/** The media id a picker item stands for, or undefined for any other item. */
export function mediaIdOf(item: Pick<PickerItem, 'key'>): string | undefined {
  return item.key.startsWith(MEDIA_KEY) ? item.key.slice(MEDIA_KEY.length) : undefined
}
