/** Placeholder for a model that has never been generated: an empty build plate. */
export function ModelThumbnail({ src, alt }: { src?: string; alt: string }) {
  if (src) {
    return (
      <img
        src={src}
        alt={alt}
        className="aspect-[4/3] w-full rounded-[4px] bg-surface-2 object-cover"
      />
    )
  }
  return (
    <div
      role="img"
      aria-label={`${alt} — not generated yet`}
      className="flex aspect-[4/3] w-full items-center justify-center rounded-[4px] bg-surface-2"
    >
      <svg viewBox="0 0 120 72" className="h-full w-full text-line-strong" aria-hidden="true">
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
