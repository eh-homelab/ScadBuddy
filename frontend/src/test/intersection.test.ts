import { afterEach, describe, expect, it, vi } from 'vitest'
import { FakeIntersectionObserver, intersect, resetIntersections } from './intersection'

// The fake IntersectionObserver the page tests drive (#3606, #3607): an element said to be
// in view stays in view, so a target observed inside it afterwards is told at once, as a
// browser tells a new observer about a target already in view.

describe('the fake IntersectionObserver', () => {
  const made: Element[] = []
  const element = (parent: Element = document.body) => {
    const e = document.createElement('div')
    parent.appendChild(e)
    made.push(e)
    return e
  }
  afterEach(() => {
    made.splice(0).forEach((e) => e.remove())
    resetIntersections()
  })

  it('tells an observer that attaches after the intersection about a target already in view', () => {
    const list = element()
    const row = element(list)
    intersect(list)
    const seen = vi.fn()
    new FakeIntersectionObserver(seen).observe(row)
    expect(seen).toHaveBeenCalledTimes(1)
    expect(seen.mock.calls[0]![0]).toEqual([expect.objectContaining({ target: row, isIntersecting: true })])
  })

  it('tells nothing about a target outside what came into view, nor after a reset', () => {
    const list = element()
    const outside = element()
    intersect(list)
    const seen = vi.fn()
    new FakeIntersectionObserver(seen).observe(outside)
    resetIntersections()
    new FakeIntersectionObserver(seen).observe(element(list))
    expect(seen).not.toHaveBeenCalled()
  })

  it('still tells an observer already attached when its target comes into view', () => {
    const row = element()
    const seen = vi.fn()
    new FakeIntersectionObserver(seen).observe(row)
    expect(seen).not.toHaveBeenCalled()
    intersect(row)
    expect(seen).toHaveBeenCalledTimes(1)
  })
})
