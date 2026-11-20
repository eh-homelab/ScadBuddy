import { useEffect, useState } from 'react'

/**
 * #6269 R-T8-5 — the width of the element the returned ref is set on, measured with a
 * `ResizeObserver`, so a chart draws at its real width and its axis text keeps its size on a
 * phone. `fallback` until the first measure (and where nothing measures, as in jsdom).
 */
export function useWidth(fallback: number): [(node: HTMLElement | null) => void, number] {
  const [node, setNode] = useState<HTMLElement | null>(null)
  const [width, setWidth] = useState<number | null>(null)
  useEffect(() => {
    if (!node) return
    const observer = new ResizeObserver((entries) => {
      const measured = Math.round(entries[0]?.contentRect.width ?? 0)
      if (measured > 0) setWidth(measured)
    })
    observer.observe(node)
    return () => observer.disconnect()
  }, [node])
  return [setNode, width ?? fallback]
}

/** The left margin that holds the longest y-axis label at `fontSize` px, and at least `min`. */
export const axisLeft = (labels: readonly string[], fontSize: number, min: number) => Math.max(min, Math.ceil(Math.max(0, ...labels.map((one) => one.length)) * fontSize * 0.62) + 6)
