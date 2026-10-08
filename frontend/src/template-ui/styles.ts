/**
 * index.css's `short` variant (#971): the stacked layout on a short window, where the page
 * scrolls instead of splitting its height (styles.test.ts keeps the two the same).
 */
export const SHORT_WINDOW = '(max-width: 1023.98px) and (max-height: 640px)'

/**
 * The host elements' own defaults (#1738). On a short window the page scrolls and the
 * template's box has no height of its own, so a preview in a `1fr` or `100%` track would
 * grow with the canvas that sizes itself to it: `<sb-preview>` takes the page's own
 * preview rule there (CustomizePage's `short:grid-rows-[max(16rem,60vh)_auto]`).
 * `:where()` adds no specificity, so a template's own `sb-preview` rule still wins.
 */
export const HOST_ELEMENT_CSS = `@media ${SHORT_WINDOW} {
  :where(sb-preview) { display: block; height: max(16rem, 60vh); min-height: 0; }
}`

/**
 * A shadow root inherits no stylesheet, so the host's widgets rendered into a template
 * UI would lose the app's look. The app's own rules are copied in as one constructed
 * sheet, after the host elements' defaults; custom properties (the theme tokens) inherit
 * across the boundary on their own.
 */
export function adoptAppStyles(root: ShadowRoot): void {
  if (typeof CSSStyleSheet === 'undefined' || !('replaceSync' in CSSStyleSheet.prototype)) return
  const rules: string[] = []
  for (const sheet of Array.from(document.styleSheets)) {
    try {
      for (const rule of Array.from(sheet.cssRules)) rules.push(rule.cssText)
    } catch {
      // A cross-origin sheet's rules cannot be read; the page CSP allows none.
    }
  }
  const host = new CSSStyleSheet()
  host.replaceSync(HOST_ELEMENT_CSS)
  const sheet = new CSSStyleSheet()
  sheet.replaceSync(rules.join('\n'))
  root.adoptedStyleSheets = [host, sheet]
}
