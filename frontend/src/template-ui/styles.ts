/**
 * A shadow root inherits no stylesheet, so the host's widgets rendered into a template
 * UI would lose the app's look. The app's own rules are copied in as one constructed
 * sheet; custom properties (the theme tokens) inherit across the boundary on their own.
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
  const sheet = new CSSStyleSheet()
  sheet.replaceSync(rules.join('\n'))
  root.adoptedStyleSheets = [sheet]
}
