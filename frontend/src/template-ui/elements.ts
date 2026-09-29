export const ELEMENT_NAMES = ['sb-param', 'sb-preview', 'sb-generate'] as const

export interface ElementRegistry {
  add(el: HostElement): void
  remove(el: HostElement): void
  changed(el: HostElement): void
}

const REGISTRY = Symbol.for('scadbuddy.template-ui.registry')
type Owner = HTMLElement & { [REGISTRY]?: ElementRegistry }

/** Called by `TemplateUi` on the element whose shadow root the template mounts into. */
export function provideRegistry(owner: HTMLElement, registry: ElementRegistry | undefined): void {
  ;(owner as Owner)[REGISTRY] = registry
}

/** Outward through nested shadow roots, so an element a framework renders into its own
 * component's shadow root (spec §4.3, "under any framework") still finds the template's. */
function registryOf(el: Element): ElementRegistry | undefined {
  let root = el.getRootNode()
  while (root instanceof ShadowRoot) {
    const found = (root.host as Owner)[REGISTRY]
    if (found) return found
    root = root.host.getRootNode()
  }
  return undefined
}

/** A host widget placeholder (spec 2026-09-27 §4.3); the host renders into it. */
export class HostElement extends HTMLElement {
  static observedAttributes = ['name', 'file', 'bind']
  #registry: ElementRegistry | undefined

  connectedCallback(): void {
    this.#registry = registryOf(this)
    this.#registry?.add(this)
  }

  disconnectedCallback(): void {
    this.#registry?.remove(this)
    this.#registry = undefined
  }

  attributeChangedCallback(): void {
    this.#registry?.changed(this)
  }
}

export function defineHostElements(): void {
  for (const name of ELEMENT_NAMES) {
    if (!customElements.get(name)) customElements.define(name, class extends HostElement {})
  }
}

/** The row carrying `data-param="<name>"`: in the panel, or in a template's own interface,
 *  where `<sb-param>` renders it inside the template's (open) shadow roots, which
 *  `document.querySelector` does not enter. */
export function findParamRow(name: string, root: Document | ShadowRoot = document): Element | null {
  const hit = root.querySelector(`[data-param="${CSS.escape(name)}"]`)
  if (hit) return hit
  for (const element of root.querySelectorAll('*')) {
    const found = element.shadowRoot ? findParamRow(name, element.shadowRoot) : null
    if (found) return found
  }
  return null
}
