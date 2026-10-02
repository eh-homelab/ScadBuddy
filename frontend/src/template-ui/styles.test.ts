import { afterEach, describe, expect, it, vi } from 'vitest'
import { adoptAppStyles } from './styles'

function shadowRoot(): ShadowRoot {
  const el = document.createElement('div')
  document.body.append(el)
  return el.attachShadow({ mode: 'open' })
}

function rulesOf(root: ShadowRoot): string {
  return root.adoptedStyleSheets.flatMap((sheet) => Array.from(sheet.cssRules, (rule) => rule.cssText)).join('\n')
}

describe('adoptAppStyles', () => {
  const style = document.createElement('style')
  style.textContent = '.probe-rule { color: red; }'

  afterEach(() => {
    style.remove()
    vi.restoreAllMocks()
    vi.unstubAllGlobals()
  })

  it("copies the page's rules into the root as one constructed sheet", () => {
    document.head.append(style)
    const root = shadowRoot()
    adoptAppStyles(root)
    expect(root.adoptedStyleSheets).toHaveLength(1)
    expect(rulesOf(root)).toContain('.probe-rule')
  })

  it('skips a sheet whose rules cannot be read and copies the rest', () => {
    document.head.append(style)
    const readable = Array.from(document.styleSheets)
    const unreadable = {
      get cssRules(): CSSRuleList {
        throw new DOMException('cross-origin', 'SecurityError')
      },
    }
    vi.spyOn(document, 'styleSheets', 'get').mockReturnValue([unreadable, ...readable] as unknown as StyleSheetList)
    const root = shadowRoot()
    adoptAppStyles(root)
    expect(rulesOf(root)).toContain('.probe-rule')
  })

  it('does nothing where constructed stylesheets are missing', () => {
    vi.stubGlobal('CSSStyleSheet', undefined)
    const root = shadowRoot()
    expect(() => adoptAppStyles(root)).not.toThrow()
    expect(root.adoptedStyleSheets).toBeUndefined()
  })
})
