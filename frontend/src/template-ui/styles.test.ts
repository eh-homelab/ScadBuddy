import { afterEach, describe, expect, it, vi } from 'vitest'
import { adoptAppStyles, SHORT_WINDOW } from './styles'

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
    expect(root.adoptedStyleSheets).toHaveLength(3)
    expect(rulesOf(root)).toContain('.probe-rule')
  })

  // #736 — the template base sheet comes last and never outranks a template's own rule.
  it('adopts the template base sheet last, every selector of it zero specificity', () => {
    const root = shadowRoot()
    adoptAppStyles(root)
    const base = root.adoptedStyleSheets[2]
    expect(Array.from(base?.cssRules ?? [], (rule) => rule.cssText).join('\n')).toContain(':where(.sb-stack)')
    const selectors = Array.from(base?.cssRules ?? []).flatMap((rule) =>
      rule instanceof CSSMediaRule ? Array.from(rule.cssRules) : [rule],
    )
    expect(selectors.length).toBeGreaterThan(0)
    for (const rule of selectors) {
      expect(rule).toBeInstanceOf(CSSStyleRule)
      // `:where(…)` alone, or `:where(…):not(:where(…) *)`: both are (0, 0, 0).
      expect((rule as CSSStyleRule).selectorText).toMatch(/^:where\([^]*\)(:not\(:where\([^]*\) \*\))?$/)
    }
  })

  // #1738 — a template's preview keeps the page's own bound on a short window.
  it('bounds <sb-preview> on a short window, below any rule of the template', () => {
    const root = shadowRoot()
    adoptAppStyles(root)
    const media = Array.from(root.adoptedStyleSheets[0]?.cssRules ?? []).find(
      (rule): rule is CSSMediaRule => rule instanceof CSSMediaRule,
    )
    expect(media?.media.mediaText).toBe(SHORT_WINDOW)
    expect(media?.cssRules[0]?.cssText).toMatch(/^:where\(sb-preview\) \{.*height: max\(16rem, 60vh\)/)
  })

  it("matches index.css's short variant", async () => {
    // `node:fs` as ui/copies.test.ts reads it: vitest serves `.css?raw` empty.
    const specifier: string = 'node:fs'
    const fs = (await import(/* @vite-ignore */ specifier)) as { readFileSync(path: URL, encoding: 'utf8'): string }
    // A variable, not a literal: vite rewrites `new URL('<literal>', import.meta.url)` to an http URL.
    const path = '../index.css'
    const indexCss = fs.readFileSync(new URL(path, import.meta.url), 'utf8')
    expect(indexCss).toContain(`@custom-variant short (@media ${SHORT_WINDOW});`)
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
