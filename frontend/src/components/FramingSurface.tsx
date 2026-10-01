import { useEffect, useEffectEvent, useRef, type CSSProperties, type KeyboardEvent, type PointerEvent, type ReactNode } from 'react'
import { orbitView, panView, wheelFactor, zoomView, type CameraView } from '../lib/framing'

interface Props {
  /** The framing now, or null when there is no camera to frame (nothing moves then). */
  view: CameraView | null
  onChange: (view: CameraView) => void
  className?: string
  style?: CSSProperties
  children: ReactNode
}

/** Pixels an arrow key turns or slides the framing by, as a drag of that length would. */
const KEY_STEP = 24

/**
 * #722 — a surface that frames a copy of the viewer's camera, with the viewer's own
 * gestures: drag to orbit; Shift-drag, right-drag or a two-finger drag to pan; the
 * wheel or a pinch to zoom. From the keyboard: arrows orbit, Shift+arrows pan, + and −
 * zoom. It only reports the new framing; drawing it is the caller's.
 */
export function FramingSurface({ view, onChange, className, style, children }: Props) {
  const ref = useRef<HTMLDivElement>(null)
  const pointers = useRef(new Map<number, { x: number; y: number }>())
  const panning = useRef(false)

  const height = () => ref.current?.clientHeight ?? 1

  // Not React's onWheel: that listener is passive, so it could not keep the dialog
  // from scrolling while the wheel zooms.
  const onWheel = useEffectEvent((event: WheelEvent) => {
    if (!view) return
    event.preventDefault()
    onChange(zoomView(view, wheelFactor(event.deltaY)))
  })
  useEffect(() => {
    const element = ref.current
    if (!element) return
    const listener = (event: WheelEvent) => onWheel(event)
    element.addEventListener('wheel', listener, { passive: false })
    return () => element.removeEventListener('wheel', listener)
  }, [])

  function down(event: PointerEvent<HTMLDivElement>) {
    if (!view) return
    event.currentTarget.setPointerCapture?.(event.pointerId)
    pointers.current.set(event.pointerId, { x: event.clientX, y: event.clientY })
    panning.current = event.button === 2 || event.shiftKey || event.ctrlKey || event.metaKey
  }

  function move(event: PointerEvent<HTMLDivElement>) {
    const last = pointers.current.get(event.pointerId)
    if (!view || !last) return
    const all = [...pointers.current.values()]
    const next = { x: event.clientX, y: event.clientY }
    if (all.length >= 2) {
      // Two fingers: the midpoint pans, the spread zooms.
      const other = [...pointers.current.entries()].find(([id]) => id !== event.pointerId)?.[1]
      if (!other) return
      const before = Math.hypot(last.x - other.x, last.y - other.y)
      const after = Math.hypot(next.x - other.x, next.y - other.y)
      let framed = panView(view, (next.x - last.x) / 2, (next.y - last.y) / 2, height())
      if (before > 0 && after > 0) framed = zoomView(framed, before / after)
      onChange(framed)
    } else {
      const dx = next.x - last.x
      const dy = next.y - last.y
      onChange(panning.current ? panView(view, dx, dy, height()) : orbitView(view, dx, dy, height()))
    }
    pointers.current.set(event.pointerId, next)
  }

  function up(event: PointerEvent<HTMLDivElement>) {
    pointers.current.delete(event.pointerId)
  }

  function key(event: KeyboardEvent<HTMLDivElement>) {
    if (!view) return
    const arrows: Record<string, [number, number]> = {
      ArrowLeft: [-KEY_STEP, 0],
      ArrowRight: [KEY_STEP, 0],
      ArrowUp: [0, -KEY_STEP],
      ArrowDown: [0, KEY_STEP],
    }
    const step = arrows[event.key]
    let framed: CameraView | null = null
    if (step) {
      framed = event.shiftKey
        ? panView(view, step[0], step[1], height())
        : orbitView(view, step[0], step[1], height())
    } else if (event.key === '+' || event.key === '=') {
      framed = zoomView(view, 0.9)
    } else if (event.key === '-' || event.key === '_') {
      framed = zoomView(view, 1 / 0.9)
    }
    if (!framed) return
    event.preventDefault()
    onChange(framed)
  }

  return (
    <div
      ref={ref}
      role="application"
      aria-roledescription="image framing"
      aria-label="Frame the image: drag to orbit, Shift-drag or right-drag to pan, scroll to zoom; arrows, Shift+arrows, + and − from the keyboard"
      tabIndex={view ? 0 : -1}
      data-testid="image-framing"
      className={`${className ?? ''} ${view ? 'cursor-grab touch-none active:cursor-grabbing' : ''}`}
      style={style}
      onPointerDown={down}
      onPointerMove={move}
      onPointerUp={up}
      onPointerCancel={up}
      onContextMenu={(event) => {
        if (view) event.preventDefault()
      }}
      onKeyDown={key}
    >
      {children}
    </div>
  )
}
