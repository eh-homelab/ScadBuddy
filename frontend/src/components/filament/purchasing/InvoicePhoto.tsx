import { MinusIcon, PlusIcon, ScanIcon } from 'lucide-react'
import { useCallback, useEffect, useRef, useState, type PointerEvent as ReactPointerEvent } from 'react'
import { Button } from '@/components/ui/button'

export const MIN_SCALE = 1
export const MAX_SCALE = 8
const STEP = 1.5
const DOUBLE_CLICK_SCALE = 2.5

type View = { scale: number; x: number; y: number }
type Point = { x: number; y: number }

const FIT: View = { scale: 1, x: 0, y: 0 }
const clamp = (value: number, low: number, high: number) => Math.min(high, Math.max(low, value))

/**
 * #6889 — an invoice photo that zooms and pans: the wheel, the buttons, a double-click, a drag,
 * and on touch a pinch and a one-finger pan (pointer events, `touch-action: none`). The scale
 * stays in 1x..8x; the offset keeps the image over the viewport, so it never slides out of view.
 * The view is a CSS transform about the viewport's centre, where the fitted image sits.
 */
export function InvoicePhoto({ src, alt }: { src: string; alt: string }) {
  const viewport = useRef<HTMLDivElement>(null)
  const image = useRef<HTMLImageElement>(null)
  const [view, setView] = useState<View>(FIT)
  const pointers = useRef(new Map<number, Point>())
  const pinch = useRef<{ distance: number; mid: Point } | null>(null)

  /** The offset, about the centre, that keeps the scaled image over the viewport. */
  const bounded = useCallback((next: View): View => {
    const scale = clamp(next.scale, MIN_SCALE, MAX_SCALE)
    const box = viewport.current?.getBoundingClientRect()
    if (!box?.width || !box.height) return { scale, x: 0, y: 0 }
    const img = image.current
    // The fitted image (object-contain) inside the box, or the box until the image loads.
    const ratio = img?.naturalWidth && img.naturalHeight ? img.naturalWidth / img.naturalHeight : box.width / box.height
    const fitted = box.width / box.height > ratio ? { w: box.height * ratio, h: box.height } : { w: box.width, h: box.width / ratio }
    const slackX = Math.max(0, (fitted.w * scale - box.width) / 2)
    const slackY = Math.max(0, (fitted.h * scale - box.height) / 2)
    return { scale, x: clamp(next.x, -slackX, slackX), y: clamp(next.y, -slackY, slackY) }
  }, [])

  /** A client point as an offset from the viewport's centre. */
  const fromCentre = useCallback((client: Point): Point => {
    const box = viewport.current?.getBoundingClientRect()
    if (!box) return { x: 0, y: 0 }
    return { x: client.x - box.left - box.width / 2, y: client.y - box.top - box.height / 2 }
  }, [])

  /** Zooms to `scale`, keeping the image point under `at` (from the centre) where it is. */
  const zoomAt = useCallback(
    (current: View, scale: number, at: Point = { x: 0, y: 0 }): View => {
      const next = clamp(scale, MIN_SCALE, MAX_SCALE)
      const ratio = next / current.scale
      return bounded({ scale: next, x: at.x - (at.x - current.x) * ratio, y: at.y - (at.y - current.y) * ratio })
    },
    [bounded],
  )

  // A resize changes the box the offset is bounded by: clamp a zoomed photo again.
  useEffect(() => {
    const element = viewport.current
    if (!element) return
    const observer = new ResizeObserver(() => setView((current) => bounded(current)))
    observer.observe(element)
    return () => observer.disconnect()
  }, [bounded])

  // React's wheel listener is passive, so it cannot stop the page from scrolling.
  useEffect(() => {
    const element = viewport.current
    if (!element) return
    const onWheel = (event: WheelEvent) => {
      event.preventDefault()
      const at = fromCentre({ x: event.clientX, y: event.clientY })
      setView((current) => zoomAt(current, current.scale * Math.exp(-event.deltaY * 0.002), at))
    }
    element.addEventListener('wheel', onWheel, { passive: false })
    return () => element.removeEventListener('wheel', onWheel)
  }, [fromCentre, zoomAt])

  function pinchOf(): { distance: number; mid: Point } | null {
    const [a, b] = [...pointers.current.values()]
    if (!a || !b) return null
    return { distance: Math.hypot(a.x - b.x, a.y - b.y), mid: { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 } }
  }

  function onPointerDown(event: ReactPointerEvent<HTMLDivElement>) {
    if (event.pointerType === 'mouse' && event.button !== 0) return
    event.currentTarget.setPointerCapture?.(event.pointerId)
    pointers.current.set(event.pointerId, { x: event.clientX, y: event.clientY })
    pinch.current = pinchOf()
  }

  function onPointerMove(event: ReactPointerEvent<HTMLDivElement>) {
    const last = pointers.current.get(event.pointerId)
    if (!last) return
    const now = { x: event.clientX, y: event.clientY }
    pointers.current.set(event.pointerId, now)
    const before = pinch.current
    if (before) {
      const after = pinchOf()
      if (!after || before.distance === 0) return
      pinch.current = after
      const at = fromCentre(before.mid)
      setView((current) => {
        const zoomed = zoomAt(current, current.scale * (after.distance / before.distance), at)
        return bounded({ ...zoomed, x: zoomed.x + after.mid.x - before.mid.x, y: zoomed.y + after.mid.y - before.mid.y })
      })
      return
    }
    setView((current) => bounded({ ...current, x: current.x + now.x - last.x, y: current.y + now.y - last.y }))
  }

  function onPointerUp(event: ReactPointerEvent<HTMLDivElement>) {
    pointers.current.delete(event.pointerId)
    pinch.current = pinchOf()
  }

  const zoomed = view.scale > MIN_SCALE

  return (
    <div className="relative flex w-full">
      <div
        ref={viewport}
        data-testid="invoice-photo-viewport"
        className={`relative h-[75vh] min-h-72 w-full touch-none select-none overflow-hidden lg:min-h-[32rem] ${zoomed ? 'cursor-grab active:cursor-grabbing' : 'cursor-zoom-in'}`}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onPointerCancel={onPointerUp}
        onDoubleClick={(event) => {
          const at = fromCentre({ x: event.clientX, y: event.clientY })
          setView((current) => (current.scale > MIN_SCALE ? FIT : zoomAt(current, DOUBLE_CLICK_SCALE, at)))
        }}
      >
        <img
          ref={image}
          src={src}
          alt={alt}
          draggable={false}
          onLoad={() => setView((current) => bounded(current))}
          className="pointer-events-none size-full object-contain will-change-transform motion-safe:transition-transform motion-safe:duration-75"
          style={{ transform: `translate(${view.x}px, ${view.y}px) scale(${view.scale})` }}
        />
      </div>
      <div
        role="group"
        aria-label="Zoom"
        className="absolute right-2 bottom-2 flex items-center gap-0.5 rounded-md border bg-background/90 p-0.5 shadow-sm backdrop-blur"
      >
        <Button type="button" variant="ghost" size="icon-sm" aria-label="Zoom out" disabled={!zoomed} onClick={() => setView((current) => zoomAt(current, current.scale / STEP))}>
          <MinusIcon aria-hidden />
        </Button>
        <output aria-live="polite" aria-label="Zoom level" className="w-11 text-center text-[12px] tabular-nums text-muted-foreground">
          {Math.round(view.scale * 100)}%
        </output>
        <Button
          type="button"
          variant="ghost"
          size="icon-sm"
          aria-label="Zoom in"
          disabled={view.scale >= MAX_SCALE}
          onClick={() => setView((current) => zoomAt(current, current.scale * STEP))}
        >
          <PlusIcon aria-hidden />
        </Button>
        <Button type="button" variant="ghost" size="icon-sm" aria-label="Fit to view" disabled={!zoomed && view.x === 0 && view.y === 0} onClick={() => setView(FIT)}>
          <ScanIcon aria-hidden />
        </Button>
      </div>
    </div>
  )
}
