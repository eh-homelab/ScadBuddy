import { api } from '../api/client'
import { useAsync } from '../lib/useAsync'

/**
 * #2282 — an arranged output's plates: how many, and a picture of each, read from its
 * 3MF (`GET /outputs/{id}/plates`). Shown under the result of an Arrange and on an
 * arranged History row, so the layout can be seen without downloading the 3MF. Nothing
 * while the plates are read, or when they cannot be.
 */
export function ArrangedPlates({ outputId, size = 'md' }: { outputId: string; size?: 'sm' | 'md' }) {
  const plates = useAsync(() => api.getOutputPlates(outputId).catch(() => []), [outputId]).data ?? []
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
              {plate.has_thumbnail ? (
                <img
                  src={api.outputPlateThumbnailUrl(outputId, plate.index)}
                  alt={label}
                  loading="lazy"
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
