import { afterEach, describe, expect, it, vi } from 'vitest'
import { copyText } from './clipboard'

function node(text: string): HTMLElement {
  const element = document.createElement('code')
  element.textContent = text
  document.body.appendChild(element)
  return element
}

describe('copyText', () => {
  afterEach(() => {
    vi.restoreAllMocks()
    vi.unstubAllGlobals()
    document.body.innerHTML = ''
    window.getSelection()?.removeAllRanges()
  })

  it('uses the async Clipboard API when it is allowed', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    vi.stubGlobal('navigator', { ...navigator, clipboard: { writeText } })
    expect(await copyText('sbmcp_abc', node('sbmcp_abc'))).toBe(true)
    expect(writeText).toHaveBeenCalledWith('sbmcp_abc')
  })

  it('falls back to execCommand when the frame may not write the clipboard', async () => {
    const writeText = vi.fn().mockRejectedValue(new DOMException('denied', 'NotAllowedError'))
    vi.stubGlobal('navigator', { ...navigator, clipboard: { writeText } })
    const execCommand = vi.fn().mockReturnValue(true)
    Object.defineProperty(document, 'execCommand', { value: execCommand, configurable: true })
    const element = node('sbmcp_abc')
    expect(await copyText('sbmcp_abc', element)).toBe(true)
    expect(execCommand).toHaveBeenCalledWith('copy')
    expect(window.getSelection()?.toString()).toBe('sbmcp_abc')
  })

  it('leaves the text selected for a manual copy when nothing works', async () => {
    vi.stubGlobal('navigator', { ...navigator, clipboard: undefined })
    Object.defineProperty(document, 'execCommand', { value: () => false, configurable: true })
    expect(await copyText('sbmcp_abc', node('sbmcp_abc'))).toBe(false)
    expect(window.getSelection()?.toString()).toBe('sbmcp_abc')
  })

  it('says it failed when there is no fallback node', async () => {
    vi.stubGlobal('navigator', { ...navigator, clipboard: undefined })
    expect(await copyText('x')).toBe(false)
  })
})
