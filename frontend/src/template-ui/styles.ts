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

/** The host's widgets: their own markup keeps the app's look, never the base sheet's. */
const WIDGETS = ':where(sb-param, sb-generate, sb-preview) *'

/** A template's own controls: zero specificity, outside the host's widgets. */
function own(selectors: string): string {
  return `:where(${selectors}):not(${WIDGETS})`
}

const FIELD = "input:not([type='checkbox'], [type='radio'], [type='range'], [type='color'], [type='file'])"
const CONTROL = `button, select, textarea, ${FIELD}, .sb-button, .sb-input`
const PRESSED = "button[aria-pressed='true'], .sb-button[aria-pressed='true'], .sb-primary"

/**
 * The shared base sheet every template UI gets (#736; the contract is spec 2026-09-27
 * §4.2 "Styles"). Tailwind never scans `models/`, so the page's utilities do not exist for
 * a template's markup; this is the small, fixed set a template can rely on instead.
 * Colours are the page's `--sb-*` tokens, which cross the shadow boundary, so light and
 * dark follow the page; font and text colour inherit from the page.
 *
 * Every selector is wrapped in `:where()`, so any rule of the template's own (a `<style>`
 * or a sheet under `ui/`, which the adopted sheets would otherwise follow in the cascade)
 * wins over it. The element rules style a template's own controls, never the markup of
 * the host's widgets inside `<sb-param>`, `<sb-generate>` or `<sb-preview>`.
 */
export const TEMPLATE_BASE_CSS = `${own(CONTROL)} {
  box-sizing: border-box; min-height: 32px; padding: 0 8px; font: inherit; color: var(--sb-text);
  background: var(--sb-surface-2); border: 1px solid var(--sb-line-strong); border-radius: 4px;
}
${own('textarea')} { padding: 6px 8px; }
${own('button, .sb-button')} { padding: 0 12px; cursor: pointer; }
${own(CONTROL.split(', ').map((selector) => `${selector}:hover`).join(', '))} {
  background: var(--sb-surface-3); border-color: var(--sb-faint);
}
${own(PRESSED)} {
  background: var(--sb-accent); border-color: var(--sb-accent); color: var(--sb-accent-ink); font-weight: 600;
}
${own(CONTROL.split(', ').map((selector) => `${selector}:disabled`).join(', '))} { opacity: 0.5; cursor: not-allowed; }
@media (pointer: coarse) {
  ${own(CONTROL)} { min-height: 44px; }
}
:where(.sb-stack) { display: flex; flex-direction: column; gap: var(--sb-gap, 8px); }
:where(.sb-row) { display: flex; flex-wrap: wrap; align-items: center; gap: var(--sb-gap, 8px); }
:where(.sb-pad) { padding: 12px; }
:where(.sb-label) { display: flex; align-items: center; justify-content: space-between; gap: 8px; font-size: 13px; }
:where(.sb-heading) {
  margin: 0; font-size: 11px; font-weight: 500; text-transform: uppercase; letter-spacing: 0.05em; color: var(--sb-faint);
}
:where(.sb-muted) { font-size: 12px; color: var(--sb-muted); }
:where(.sb-list) { list-style: none; margin: 0; padding: 0; }
:where(.sb-list > li) { border-top: 1px solid var(--sb-line); }`

/**
 * A shadow root inherits no stylesheet, so the host's widgets rendered into a template
 * UI would lose the app's look. The app's own rules are copied in as one constructed
 * sheet, after the host elements' defaults, and the template base sheet follows them;
 * custom properties (the theme tokens) inherit across the boundary on their own.
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
  const base = new CSSStyleSheet()
  base.replaceSync(TEMPLATE_BASE_CSS)
  root.adoptedStyleSheets = [host, sheet, base]
}
