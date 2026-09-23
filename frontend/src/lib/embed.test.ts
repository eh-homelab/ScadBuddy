import { beforeEach, describe, expect, it, vi } from 'vitest'
import { isEmbedded, openExternal, triggerDownload } from './embed'

describe('isEmbedded', () => {
  it('is false at the top level', () => {
    expect(isEmbedded()).toBe(false)
  })

  it('is true inside a frame', () => {
    const top = window.top
    Object.defineProperty(window, 'top', { value: {}, configurable: true })
    expect(isEmbedded()).toBe(true)
    Object.defineProperty(window, 'top', { value: top, configurable: true })
  })
})

describe('triggerDownload', () => {
  let clicked: HTMLAnchorElement | undefined

  beforeEach(() => {
    clicked = undefined
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (
      this: HTMLAnchorElement,
    ) {
      // eslint-disable-next-line @typescript-eslint/no-this-alias -- the spy's receiver is the anchor under test
      clicked = this
    })
  })

  it('uses a download anchor', () => {
    triggerDownload('blob:x', 'model.3mf', false)
    expect(clicked?.getAttribute('download')).toBe('model.3mf')
    expect(clicked?.target).toBe('')
  })

  it('escapes the sandbox when embedded', () => {
    triggerDownload('blob:x', 'model.3mf', true)
    expect(clicked?.target).toBe('_blank')
  })

  it('removes the anchor again', () => {
    triggerDownload('blob:x', 'model.3mf', false)
    expect(document.body.querySelector('a')).toBeNull()
  })
})

describe('openExternal', () => {
  it('opens a new tab only when embedded', () => {
    const open = vi.spyOn(window, 'open').mockReturnValue(null)
    openExternal('https://bambuddy.example/queue', true)
    expect(open).toHaveBeenLastCalledWith('https://bambuddy.example/queue', '_blank', 'noopener')
    openExternal('https://bambuddy.example/queue', false)
    expect(open).toHaveBeenLastCalledWith('https://bambuddy.example/queue', '_self', 'noopener')
    open.mockRestore()
  })
})
