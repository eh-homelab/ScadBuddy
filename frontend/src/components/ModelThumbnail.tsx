import { useState } from 'react'
import { Skeleton } from '@/components/ui/skeleton'

/**
 * A model's picture, a skeleton until it has loaded (#3670). With none, the placeholder
 * for a model that has never been generated: an empty build plate.
 */
export function ModelThumbnail({ src, alt }: { src?: string; alt: string }) {
  // Keyed by the picture, so a new one shows its skeleton again.
  const [loaded, setLoaded] = useState<string | null>(null)
  if (src) {
    return (
      <span className="relative block">
        <img
          src={src}
          alt={alt}
          onLoad={() => setLoaded(src)}
          onError={() => setLoaded(src)}
          className="aspect-[4/3] w-full rounded-lg bg-muted object-cover"
        />
        {loaded !== src && <Skeleton aria-hidden className="absolute inset-0 rounded-lg" />}
      </span>
    )
  }
  return (
    <div
      role="img"
      aria-label={`${alt} — not generated yet`}
      className="flex aspect-[4/3] w-full items-center justify-center rounded-lg bg-muted/60"
    >
      <svg viewBox="0 0 120 72" className="h-full w-full text-border" aria-hidden="true">
        <defs>
          <pattern id="plate" width="12" height="12" patternUnits="userSpaceOnUse">
            <path d="M12 0H0V12" fill="none" stroke="currentColor" strokeWidth="0.5" />
          </pattern>
        </defs>
        <rect x="18" y="12" width="84" height="48" fill="url(#plate)" opacity="0.7" />
        <rect
          x="18"
          y="12"
          width="84"
          height="48"
          fill="none"
          stroke="currentColor"
          strokeWidth="1"
        />
      </svg>
    </div>
  )
}
