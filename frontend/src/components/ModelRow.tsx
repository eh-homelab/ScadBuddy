import { useMemo } from 'react'
import { Link } from 'react-router'
import { api } from '../api/client'
import type { ModelSummary } from '../api/types'
import { modelPath } from '../lib/deeplink'
import { safeHttpUrl } from '../lib/safeUrl'
import { timeAgo } from '../lib/format'
import { DuplicatedFrom, DuplicateModelButton } from './DuplicateModelButton'
import { namedSlides, type Slide } from './media/slides'
import { ModelOrigin } from './ModelOrigin'
import { ModelThumbnail } from './ModelThumbnail'
import { UpstreamBadge } from './UpstreamUpdate'
import { PlayIcon } from 'lucide-react'
import { Badge } from '@/components/ui/badge'
import { Card } from '@/components/ui/card'

interface Props {
  model: ModelSummary
  /** The upstream's display name, for "Duplicated from", when the catalogue has it. */
  upstreamName?: string
  /** Opens the page's lightbox on the template's slides, at `index`. */
  onOpen: (slides: Slide[], index: number) => void
  onTag: (tag: string) => void
}

/**
 * #278 — one template in the catalogue's list mode: a small cover (which opens the
 * lightbox, never navigates), the name (which does), then the details a card shows.
 * No carousel. On narrow widths the description, badge, origin and time are dropped;
 * the duplicate's attribution and Duplicate stay.
 */
export function ModelRow({ model, upstreamName, onOpen, onTag }: Props) {
  const slides = useMemo(() => namedSlides(model), [model])
  // The backend's cover rule (`catalogue._cover`): a video with no poster has no frame
  // to show, so the cover is the first image or poster'd video. It only picks the
  // picture: poster-less videos alone still open, at the first one. As on a card, a
  // template with media shows only its media, never an output's plate or the preview
  // that `has_thumbnail` may name, so the picture is always the slide it opens on.
  const coverIndex = slides.findIndex((slide) => slide.kind === 'image' || slide.poster)
  const cover = coverIndex >= 0 ? slides[coverIndex] : undefined
  // The strip-sized copy for an 80 px tile, never the original (#1034).
  const src = cover?.thumbnail ?? (cover?.kind === 'image' ? cover.src : cover?.poster)
  const tags = model.tags ?? []
  // Only an http(s) origin is shown, so only one takes the provenance line.
  const origin = safeHttpUrl(model.origin_url)

  return (
    <li data-model-card={model.slug} data-model-row>
      <Card size="sm" className="flex-row items-center gap-3 px-2 py-2 transition-shadow hover:ring-foreground/25">
      <div className="relative w-20 shrink-0 sm:w-24">
        {slides.length > 0 ? (
          <button
            type="button"
            aria-label={`View media of ${model.name}${slides.length > 1 ? ` (${slides.length})` : ''}`}
            onClick={() => onOpen(slides, coverIndex >= 0 ? coverIndex : 0)}
            className="block w-full cursor-zoom-in rounded-lg outline-none focus-visible:ring-3 focus-visible:ring-ring/50"
          >
            {src ? (
              <ModelThumbnail src={src} alt="" />
            ) : (
              // Only poster-less videos: a neutral tile with a play badge, as a card
              // shows, rather than the never-generated placeholder.
              <span
                data-testid="video-tile"
                aria-hidden="true"
                className="relative flex aspect-[4/3] w-full items-center justify-center rounded-lg bg-ui-muted"
              >
                <Badge className="size-7 rounded-full bg-black/60 p-0 text-white">
                  <PlayIcon className="ml-0.5 fill-current" />
                </Badge>
              </span>
            )}
            {slides.length > 1 && (
              <Badge
                data-testid="media-count"
                aria-hidden="true"
                className="sb-num absolute right-1 bottom-1 h-4 rounded-sm bg-black/70 px-1 text-[10px] text-white"
              >
                {slides.length}
              </Badge>
            )}
          </button>
        ) : (
          <ModelThumbnail
            src={model.has_thumbnail ? api.modelThumbnailUrl(model) : undefined}
            alt={model.name}
          />
        )}
      </div>

      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2">
          <h2 className="min-w-0 truncate text-sm font-medium">
            <Link to={modelPath(model.slug)} className="underline-offset-4 hover:underline focus-visible:rounded-sm">
              {model.name}
            </Link>
          </h2>
          <UpstreamBadge state={model.upstream_state} />
          {model.origin === 'builtin' && (
            <Badge data-testid="builtin-badge" variant="secondary" className="hidden sm:inline-flex">
              Built-in — read-only
            </Badge>
          )}
        </div>
        {model.description && (
          <p className="mt-0.5 hidden truncate text-sm text-muted-foreground sm:block">
            {model.description}
          </p>
        )}
        {(model.upstream || origin) && (
          <div data-testid="row-provenance" className="mt-0.5 flex min-w-0 items-baseline gap-3">
            <DuplicatedFrom upstream={model.upstream} name={upstreamName} className="min-w-0 truncate" />
            <ModelOrigin url={origin} className="hidden min-w-0 sm:block" />
          </div>
        )}
        {tags.length > 0 && (
          <ul className="mt-1.5 flex flex-wrap gap-1">
            {tags.map((tag) => (
              <li key={tag}>
                <Badge variant="secondary" asChild>
                  <button
                    type="button"
                    aria-label={`Filter by ${tag}`}
                    onClick={() => onTag(tag)}
                    className="cursor-pointer hover:bg-ui-accent hover:text-accent-foreground"
                  >
                    {tag}
                  </button>
                </Badge>
              </li>
            ))}
          </ul>
        )}
      </div>

      <p className="hidden shrink-0 text-xs text-muted-foreground md:block">
        Updated {timeAgo(model.updated_at)}
      </p>
      <span className="shrink-0">
        <DuplicateModelButton slug={model.slug} name={model.name} />
      </span>
      </Card>
    </li>
  )
}
