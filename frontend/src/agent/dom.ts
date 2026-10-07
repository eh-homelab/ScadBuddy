/**
 * A small, deliberately partial reading of the accessibility tree: enough to name the
 * app's own controls the way Testing Library and Playwright do (explicit `role`, then the
 * element's implicit role; `aria-labelledby`, `aria-label`, `<label>`, then text), not a
 * full implementation of the accname spec. The fallback tools and `snapshot()` use it.
 */

const INTERACTIVE_SELECTOR = [
  'a[href]',
  'button',
  'input:not([type="hidden"])',
  'select',
  'textarea',
  '[role="button"]',
  '[role="link"]',
  '[role="tab"]',
  '[role="checkbox"]',
  '[role="radio"]',
  '[role="switch"]',
  '[role="menuitem"]',
  '[role="option"]',
  '[role="combobox"]',
  '[role="textbox"]',
  '[role="searchbox"]',
  '[role="slider"]',
  '[role="spinbutton"]',
].join(',')

/** Controls whose value `fill` can set. */
export const FILLABLE_ROLES = new Set(['textbox', 'searchbox', 'spinbutton', 'slider', 'combobox'])

export function roleOf(element: Element): string {
  const explicit = element.getAttribute('role')?.trim().split(/\s+/)[0]
  if (explicit) return explicit
  const tag = element.tagName.toLowerCase()
  switch (tag) {
    case 'a':
      return element.hasAttribute('href') ? 'link' : 'generic'
    case 'button':
      return 'button'
    case 'select':
      return (element as HTMLSelectElement).multiple ? 'listbox' : 'combobox'
    case 'textarea':
      return 'textbox'
    case 'input': {
      const type = (element.getAttribute('type') ?? 'text').toLowerCase()
      if (['button', 'submit', 'reset', 'image'].includes(type)) return 'button'
      if (type === 'checkbox') return 'checkbox'
      if (type === 'radio') return 'radio'
      if (type === 'range') return 'slider'
      if (type === 'number') return 'spinbutton'
      if (type === 'search') return 'searchbox'
      if (type === 'file') return 'button'
      return 'textbox'
    }
    case 'dialog':
      return 'dialog'
    default:
      return 'generic'
  }
}

function textOf(element: Element): string {
  return (element.textContent ?? '').replace(/\s+/g, ' ').trim()
}

export function nameOf(element: Element): string {
  const labelledBy = element.getAttribute('aria-labelledby')
  if (labelledBy) {
    const text = labelledBy
      .split(/\s+/)
      .map((id) => element.ownerDocument.getElementById(id))
      .filter((node): node is HTMLElement => node !== null)
      .map(textOf)
      .join(' ')
      .trim()
    if (text) return text
  }
  const label = element.getAttribute('aria-label')?.trim()
  if (label) return label

  if (
    element instanceof HTMLInputElement ||
    element instanceof HTMLSelectElement ||
    element instanceof HTMLTextAreaElement ||
    element instanceof HTMLButtonElement
  ) {
    // A `<label for>` names a button too: the parameter panel's switches are named so.
    const labels = element.labels ? [...element.labels].map(textOf).filter(Boolean) : []
    if (labels.length > 0) return labels.join(' ')
    if (element instanceof HTMLInputElement && ['button', 'submit', 'reset'].includes(element.type)) {
      return element.value
    }
    const placeholder = element.getAttribute('placeholder')?.trim()
    if (placeholder) return placeholder
  }

  if (element instanceof HTMLImageElement) return element.alt.trim()
  const role = roleOf(element)
  if (['button', 'link', 'tab', 'menuitem', 'option', 'radio', 'checkbox', 'switch'].includes(role)) {
    const text = textOf(element)
    if (text) return text
  }
  return element.getAttribute('title')?.trim() ?? ''
}

/** Rendered and not hidden from assistive technology. */
export function isVisible(element: Element): boolean {
  if (element.closest('[hidden], [aria-hidden="true"], [inert]')) return false
  const check = (element as HTMLElement & { checkVisibility?: () => boolean }).checkVisibility
  if (typeof check === 'function') return check.call(element)
  return true
}

export function isDisabled(element: Element): boolean {
  if ((element as HTMLButtonElement).disabled === true) return true
  if (element.closest('fieldset:disabled')) return true
  return element.getAttribute('aria-disabled') === 'true'
}

/** The open modal dialog, if any: while one is up, nothing behind it is reachable. */
export function activeDialog(root: ParentNode = document): Element | null {
  const dialogs = [...root.querySelectorAll('[role="dialog"], [role="alertdialog"], dialog[open]')]
  const modal = dialogs.filter(
    (dialog) =>
      isVisible(dialog) &&
      // `data-modal`: ui/Dialog, which drops aria-modal beside the assistant (#798) and
      // is no less modal to the agent for it.
      (dialog.getAttribute('aria-modal') === 'true' || dialog.hasAttribute('data-modal') || dialog.tagName === 'DIALOG'),
  )
  return modal.at(-1) ?? null
}

export function interactiveElements(root: ParentNode): Element[] {
  return [...root.querySelectorAll(INTERACTIVE_SELECTOR)].filter(isVisible)
}

/**
 * Elements with this role and accessible name, exact match ignoring case and runs of
 * whitespace. Inside an open modal dialog only the dialog is searched, as a user could
 * only reach what is in it.
 */
export function findByRole(role: string, name: string, root: ParentNode = document): Element[] {
  const scope = activeDialog(root) ?? root
  const wanted = normalise(name)
  return interactiveElements(scope).filter(
    (element) => roleOf(element) === role && normalise(nameOf(element)) === wanted,
  )
}

export function findField(label: string, root: ParentNode = document): Element[] {
  const scope = activeDialog(root) ?? root
  const wanted = normalise(label)
  return interactiveElements(scope).filter(
    (element) => FILLABLE_ROLES.has(roleOf(element)) && normalise(nameOf(element)) === wanted,
  )
}

function normalise(text: string): string {
  return text.replace(/\s+/g, ' ').trim().toLowerCase()
}

/**
 * Marks a control only the user may press: the confirmation of an outward action
 * (send, print, delete, settings or credential writes — AI design spec §8.1/§8.2).
 * The fallback `click` and `fill` refuse anything inside one.
 */
export const USER_ONLY = { 'data-agent-user-only': '' } as const

export function isUserOnly(element: Element): boolean {
  return element.closest('[data-agent-user-only]') !== null
}

/**
 * Set a controlled input's value so React sees it: through the prototype's setter (the
 * element's own one is React's value tracker) and then the event React listens to. This
 * is the same change a keystroke makes, so the component's own `onChange` runs.
 */
export function setControlValue(element: HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement, value: string) {
  const prototype = Object.getPrototypeOf(element) as object
  const setter = Object.getOwnPropertyDescriptor(prototype, 'value')?.set
  if (setter) setter.call(element, value)
  else element.value = value
  const eventName = element instanceof HTMLSelectElement ? 'change' : 'input'
  element.dispatchEvent(new Event(eventName, { bubbles: true }))
}
