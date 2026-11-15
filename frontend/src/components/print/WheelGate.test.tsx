import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render } from '@testing-library/react'
import { useEffect, useRef } from 'react'
import { WheelGate } from './WheelGate'

afterEach(cleanup)

/** Stands in for the scene's canvas: OrbitControls listens for the wheel on it. */
function Scene({ onWheel }: { onWheel: () => void }) {
  const ref = useRef<HTMLDivElement>(null)
  useEffect(() => {
    const el = ref.current!
    el.addEventListener('wheel', onWheel)
    return () => el.removeEventListener('wheel', onWheel)
  }, [onWheel])
  return <div ref={ref} data-testid="scene" />
}

function setup({ room }: { room: number }) {
  const onWheel = vi.fn()
  const { getByTestId } = render(
    <div data-testid="body" style={{ overflowY: 'auto' }}>
      <WheelGate>
        <Scene onWheel={onWheel} />
      </WheelGate>
    </div>,
  )
  const body = getByTestId('body')
  Object.defineProperty(body, 'clientHeight', { value: 500 })
  Object.defineProperty(body, 'scrollHeight', { value: 500 + room })
  const scene = getByTestId('scene')
  const wheel = (init: WheelEventInit = {}) =>
    scene.dispatchEvent(new WheelEvent('wheel', { deltaY: 100, bubbles: true, cancelable: true, ...init }))
  return { onWheel, scene, gate: scene.parentElement!, wheel }
}

describe('WheelGate', () => {
  it('leaves a plain wheel to the body that scrolls, not the scene', () => {
    const { onWheel, wheel } = setup({ room: 800 })
    wheel()
    expect(onWheel).not.toHaveBeenCalled()
  })

  it('zooms with Ctrl or Cmd held (a trackpad pinch is a Ctrl wheel)', () => {
    const { onWheel, wheel } = setup({ room: 800 })
    wheel({ ctrlKey: true })
    wheel({ metaKey: true })
    expect(onWheel).toHaveBeenCalledTimes(2)
  })

  it('zooms once the scene is pressed, until the pointer leaves it', () => {
    const { onWheel, gate, scene, wheel } = setup({ room: 800 })
    scene.dispatchEvent(new Event('pointerdown', { bubbles: true }))
    wheel()
    expect(onWheel).toHaveBeenCalledTimes(1)
    gate.dispatchEvent(new Event('pointerleave'))
    wheel()
    expect(onWheel).toHaveBeenCalledTimes(1)
  })

  it('zooms with a plain wheel in a dialog whose body fits, over a page that scrolls', () => {
    const onWheel = vi.fn()
    const { getByTestId } = render(
      <div data-testid="page" style={{ overflowY: 'auto' }}>
        <div role="dialog">
          <div data-testid="body" style={{ overflowY: 'auto' }}>
            <WheelGate>
              <Scene onWheel={onWheel} />
            </WheelGate>
          </div>
        </div>
      </div>,
    )
    const page = getByTestId('page')
    Object.defineProperty(page, 'clientHeight', { value: 500 })
    Object.defineProperty(page, 'scrollHeight', { value: 2000 })
    getByTestId('scene').dispatchEvent(new WheelEvent('wheel', { deltaY: 100, bubbles: true, cancelable: true }))
    expect(onWheel).toHaveBeenCalledTimes(1)
  })

  it('zooms with a plain wheel where nothing around it scrolls', () => {
    const { onWheel, wheel } = setup({ room: 0 })
    wheel()
    expect(onWheel).toHaveBeenCalledTimes(1)
  })
})
