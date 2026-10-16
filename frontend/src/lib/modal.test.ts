import { afterEach, describe, expect, it, vi } from 'vitest'
import { closeOnUntakenEscape, escapeTaken, leaveToCompanion } from './modal'

// #3636 — a dialog closes on an Escape only once it reaches `document` untaken.

/** Dispatches Escape at `target`; `onCapture` runs where Radix hears it, on document's capture phase. */
function escape(target: HTMLElement, onCapture: (event: KeyboardEvent) => void): KeyboardEvent {
  const event = new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true })
  const capture = (seen: Event) => onCapture(seen as KeyboardEvent)
  document.addEventListener('keydown', capture, { capture: true, once: true })
  target.dispatchEvent(event)
  return event
}

describe('closeOnUntakenEscape', () => {
  afterEach(() => {
    document.body.innerHTML = ''
  })

  function inside() {
    const field = document.createElement('div')
    document.body.append(field)
    return field
  }

  it('closes on a key nothing took, and the key reads as taken to anyone else', () => {
    const close = vi.fn()
    const event = escape(inside(), (seen) => closeOnUntakenEscape(seen, close))
    expect(close).toHaveBeenCalledTimes(1)
    expect(escapeTaken(event)).toBe(true)
  })

  it('stays open when something inside stops the key', () => {
    const close = vi.fn()
    const field = inside()
    field.addEventListener('keydown', (event) => event.stopPropagation())
    escape(field, (seen) => closeOnUntakenEscape(seen, close))
    expect(close).not.toHaveBeenCalled()
  })

  it('stays open when something inside takes the key with preventDefault', () => {
    const close = vi.fn()
    const field = inside()
    field.addEventListener('keydown', (event) => event.preventDefault())
    escape(field, (seen) => closeOnUntakenEscape(seen, close))
    expect(close).not.toHaveBeenCalled()
  })

  it('does not close on a later key once a stopped one has passed', async () => {
    const close = vi.fn()
    const field = inside()
    const stop = (event: Event) => event.stopPropagation()
    field.addEventListener('keydown', stop)
    escape(field, (seen) => closeOnUntakenEscape(seen, close))
    field.removeEventListener('keydown', stop)
    await new Promise((resolve) => setTimeout(resolve, 0))
    field.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true }))
    expect(close).not.toHaveBeenCalled()
  })
})

describe('leaveToCompanion', () => {
  it('reads as untaken until the companion takes it', () => {
    const event = new KeyboardEvent('keydown', { key: 'Escape', cancelable: true })
    leaveToCompanion(event)
    expect(event.defaultPrevented).toBe(true)
    expect(escapeTaken(event)).toBe(false)
    event.preventDefault()
    expect(escapeTaken(event)).toBe(true)
  })
})
