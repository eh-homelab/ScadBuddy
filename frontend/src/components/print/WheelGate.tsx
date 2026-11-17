import { useEffect, useRef, type ReactNode } from 'react'

/**
 * Whether something around `el` scrolls vertically: the wheel then belongs to it. Not past
 * the dialog it is in: the page under a dialog is not what the wheel is for.
 */
function insideScroller(el: Element): boolean {
  for (let at = el.parentElement; at; at = at.parentElement) {
    const scrolls = at === document.scrollingElement || /auto|scroll/.test(getComputedStyle(at).overflowY)
    if (scrolls && at.scrollHeight > at.clientHeight) return true
    if (at.getAttribute('role') === 'dialog') return false
  }
  return false
}

/**
 * A 3D scene inside something that scrolls (the print dialog's body, under its sticky
 * preview): OrbitControls takes every wheel over the canvas for zoom, so a long list of
 * colours could not be scrolled with the pointer over it. A plain wheel scrolls instead,
 * until the scene is pressed (to turn or pan it), and zooms from then until the pointer
 * leaves it. Ctrl or Cmd with the wheel, and a trackpad pinch (a Ctrl wheel), always
 * zoom. Where nothing around it scrolls, the wheel zooms as before.
 */
export function WheelGate({ children }: { children: ReactNode }) {
  const ref = useRef<HTMLDivElement>(null)
  useEffect(() => {
    const gate = ref.current
    if (!gate) return
    let engaged = false
    const engage = () => {
      engaged = true
    }
    const release = () => {
      engaged = false
    }
    // Captured here, before the scene's own listener: stopped, it never zooms, and the
    // browser scrolls as it would anywhere else.
    const onWheel = (event: WheelEvent) => {
      if (engaged || event.ctrlKey || event.metaKey || !insideScroller(gate)) return
      event.stopPropagation()
    }
    gate.addEventListener('pointerdown', engage, true)
    gate.addEventListener('pointerleave', release)
    gate.addEventListener('wheel', onWheel, { capture: true, passive: true })
    return () => {
      gate.removeEventListener('pointerdown', engage, true)
      gate.removeEventListener('pointerleave', release)
      gate.removeEventListener('wheel', onWheel, { capture: true })
    }
  }, [])
  return (
    <div ref={ref} className="size-full">
      {children}
    </div>
  )
}
