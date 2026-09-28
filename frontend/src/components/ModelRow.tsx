import { useMemo } from 'react'
import { Link } from 'react-router'
import { api } from '../api/client'
import type { ModelSummary } from '../api/types'
import { modelPath } from '../lib/deeplink'
import { timeAgo } from '../lib/format'
import { DuplicatedFrom, DuplicateModelButton } from './DuplicateModelButton'
import { namedSlides, type Slide } from './media/slides'
import { ModelOrigin } from './ModelOrigin'
import { ModelThumbnail } from './ModelThumbnail'
import { UpstreamBadge } from './UpstreamUpdate'

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
  const src = cover?.kind === 'image' ? cover.src : cover?.poster
  const tags = model.tags ?? []

  return (
    <li
      data-model-card={model.slug}
      data-model-row
      className="flex items-center gap-3 rounded-[6px] border border-line bg-surface p-2 transition-colors hover:border-line-strong"
    >
      <div className="relative w-20 shrink-0 sm:w-24">
        {slides.length > 0 ? (
          <button
            type="button"
            aria-label={`View media of ${model.name}${slides.length > 1 ? ` (${slides.length})` : ''}`}
            onClick={() => onOpen(slides, coverIndex >= 0 ? coverIndex : 0)}
            className="block w-full cursor-zoom-in rounded-[4px] focus-visible:outline-2 focus-visible:outline-accent"
          >
            {src ? (
              <ModelThumbnail src={src} alt="" />
            ) : (
              // Only poster-less videos: a neutral tile with a play badge, as a card
              // shows, rather than the never-generated placeholder.
              <span
                data-testid="video-tile"
                aria-hidden="true"
                className="relative flex aspect-[4/3] w-full items-center justify-center rounded-[4px] bg-surface-2"
              >
                <span className="flex h-7 w-7 items-center justify-center rounded-full bg-black/60 text-white">
                  <svg viewBox="0 0 16 16" className="ml-0.5 h-3.5 w-3.5">
                    <path d="M4 2.5v11l9-5.5z" fill="currentColor" />
                  </svg>
                </span>
              </span>
            )}
            {slides.length > 1 && (
              <span
                data-testid="media-count"
                aria-hidden="true"
                className="sb-num absolute right-1 bottom-1 rounded-[3px] bg-black/70 px-1 text-[10px] leading-4 text-white"
              >
                {slides.length}
              </span>
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
          <h2 className="min-w-0 truncate text-[14px] font-medium">
            <Link to={modelPath(model.slug)} className="hover:underline focus-visible:rounded-[3px]">
              {model.name}
            </Link>
          </h2>
          <UpstreamBadge state={model.upstream_state} />
          {model.origin === 'builtin' && (
            <span
              data-testid="builtin-badge"
              className="hidden shrink-0 text-[11px] text-faint sm:inline"
            >
              Built-in — read-only
            </span>
          )}
        </div>
        {model.description && (
          <p className="mt-0.5 hidden truncate text-[13px] text-muted sm:block">
            {model.description}
          </p>
        )}
        {(model.upstream || model.origin_url) && (
          <div className="mt-0.5 flex min-w-0 items-baseline gap-3">
            <DuplicatedFrom upstream={model.upstream} name={upstreamName} className="min-w-0 truncate" />
            <ModelOrigin url={model.origin_url} className="hidden min-w-0 sm:block" />
          </div>
        )}
        {tags.length > 0 && (
          <ul className="mt-1.5 flex flex-wrap gap-1">
            {tags.map((tag) => (
              <li key={tag}>
                <button
                  type="button"
                  aria-label={`Filter by ${tag}`}
                  onClick={() => onTag(tag)}
                  className="rounded-[3px] bg-surface-2 px-1.5 py-0.5 text-[11px] text-muted transition-colors hover:bg-surface-3 hover:text-ink"
                >
                  {tag}
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>

      <p className="hidden shrink-0 text-[12px] text-faint md:block">
        Updated {timeAgo(model.updated_at)}
      </p>
      <span className="shrink-0">
        <DuplicateModelButton slug={model.slug} name={model.name} />
      </span>
    </li>
  )
}
