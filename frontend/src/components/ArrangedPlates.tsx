import { useEffect, useRef, useState } from 'react'
import { api } from '../api/client'
import { useAsync } from '../lib/useAsync'

/**
 * #2282 — an arranged output's plates: how many, and a picture of each, read from its
 * 3MF (`GET /outputs/{id}/plates`). Shown under the result of an Arrange and on an
 * arranged History row, so the layout can be seen without downloading the 3MF. Nothing
 * while the plates are read; a muted note when they cannot be, so a failed read does not
 * look like an arrange with no plates.
 */
export function ArrangedPlates({ outputId, size = 'md' }: { outputId: string; size?: 'sm' | 'md' }) {
  const read = useAsync(() => api.getOutputPlates(outputId), [outputId])
  /**
   * Plates whose picture failed to load: shown as the placeholder a plate with none gets.
   * Kept per output, so a component reused for another output starts clean (#3555).
   */
  const [failed, setFailed] = useState<{ outputId: string; plates: ReadonlySet<number> }>({
    outputId,
    plates: new Set(),
  })
  const broken = failed.outputId === outputId ? failed.plates : new Set<number>()
  if (read.error) return <p className="mt-1.5 text-[12px] text-faint">Plates unavailable</p>
  const plates = read.data ?? []
  if (plates.length === 0) return null
  const box = size === 'sm' ? 'size-14' : 'size-24'
  return (
    <figure aria-label="Plates" className="mt-1.5">
      <figcaption className="sb-num text-[12px] text-muted">
        {plates.length} {plates.length === 1 ? 'plate' : 'plates'}
      </figcaption>
      <ul className="mt-1 flex flex-wrap gap-2">
        {plates.map((plate) => {
          const label = `Plate ${plate.index}${plate.name ? `: ${plate.name}` : ''}`
          return (
            <li key={plate.index} title={label}>
              {plate.has_thumbnail && !broken.has(plate.index) ? (
                <img
                  src={api.outputPlateThumbnailUrl(outputId, plate.index)}
                  alt={label}
                  loading="lazy"
                  onError={() =>
                    setFailed((was) => ({
                      outputId,
                      plates: new Set(was.outputId === outputId ? was.plates : []).add(plate.index),
                    }))
                  }
                  className={`${box} rounded-[4px] border border-line bg-surface-2 object-contain`}
                />
              ) : (
                <span
                  className={`${box} sb-num flex items-center justify-center rounded-[4px] border border-dashed border-line text-[11px] text-faint`}
                >
                  {label}
                </span>
              )}
            </li>
          )
        })}
      </ul>
    </figure>
  )
}

/** How far outside the viewport a row starts to read its plates. */
const NEAR_VIEWPORT = '200px'

/**
 * #2754 review — {@link ArrangedPlates} once its place nears the viewport, so a long
 * History does not read every arranged row's plates as it mounts. Without
 * IntersectionObserver it reads at once.
 */
export function LazyArrangedPlates(props: { outputId: string; size?: 'sm' | 'md' }) {
  const [near, setNear] = useState(() => typeof IntersectionObserver === 'undefined')
  const spot = useRef<HTMLDivElement>(null)
  useEffect(() => {
    const element = spot.current
    if (near || !element) return
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((entry) => entry.isIntersecting)) setNear(true)
      },
      { rootMargin: NEAR_VIEWPORT },
    )
    observer.observe(element)
    return () => observer.disconnect()
  }, [near])
  return near ? <ArrangedPlates {...props} /> : <div ref={spot} data-testid="arranged-plates-pending" />
}
