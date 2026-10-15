import { createContext } from 'react'

const FOCUSABLE = [
  'a[href]',
  'button:not([disabled])',
  'input:not([disabled]):not([type="hidden"])',
  'select:not([disabled])',
  'textarea:not([disabled])',
  '[tabindex]:not([tabindex="-1"])',
  '[contenteditable="true"]',
].join(',')

/** What Tab can reach inside `root`, in document order; hidden ones left out. */
export function focusables(root: HTMLElement): HTMLElement[] {
  return Array.from(root.querySelectorAll<HTMLElement>(FOCUSABLE)).filter(
    // `checkVisibility` is absent in jsdom, where nothing is laid out anyway.
    (element) => !element.closest('[hidden], [inert]') && (element.checkVisibility?.() ?? true),
  )
}

/** The topmost open dialog (or alert dialog, as agent/dom `activeDialog` counts them): one opened from inside another comes later in the document. */
export function topmostDialog(): HTMLElement | null {
  const dialogs = document.querySelectorAll<HTMLElement>('[role="dialog"], [role="alertdialog"]')
  return dialogs[dialogs.length - 1] ?? null
}

/** Focus into `dialog`: its first focusable element, or the dialog itself. */
export function focusInto(dialog: HTMLElement) {
  ;(focusables(dialog)[0] ?? dialog).focus()
}

/**
 * #798 — a region that stays usable beside a modal dialog: the assistant panel, so the
 * user can ask about the Print dialog the agent just opened. A dialog leaves room for
 * it, does not cover it, does not take focus from it, and is not `aria-modal` while it
 * is there. AppShell provides the panel's element while the panel is open.
 */
export const ModalCompanionContext = createContext<HTMLElement | null>(null)

/** The narrowest a dialog's side of the page gets beside a companion; narrower, it covers it. */
const MIN_BESIDE = 360

/**
 * The room to keep for `companion` at the right edge, in CSS pixels: 0 when there is
 * none to spare (a phone), so the dialog covers it; null when it is not laid out
 * (jsdom), where it counts as beside.
 */
export function roomFor(companion: HTMLElement): number | null {
  const { left, width } = companion.getBoundingClientRect()
  if (width === 0) return null
  return left >= MIN_BESIDE ? window.innerWidth - left : 0
}

/** Whether `companion` is usable beside a dialog, measured now. */
export function besideNow(companion: HTMLElement | null): companion is HTMLElement {
  return companion !== null && roomFor(companion) !== 0
}

/** Escapes a dialog left to the companion beside it, with whether anyone took them since. */
const leftToCompanion = new WeakMap<Event, () => boolean>()

/**
 * #3653 — an Escape pressed in the companion beside a dialog is the companion's (it
 * closes the assistant), not the dialog's. Radix closes the topmost dialog on any
 * Escape it is not stopped from taking, and the only way to stop it is
 * `preventDefault`, which the companion reads as "taken": {@link escapeTaken} tells
 * the two apart.
 */
export function leaveToCompanion(event: Event): void {
  event.preventDefault()
  let taken = false
  const prevent = event.preventDefault.bind(event)
  event.preventDefault = () => {
    taken = true
    prevent()
  }
  leftToCompanion.set(event, () => taken)
}

/** Whether something already handled this key: `defaultPrevented`, but not by a dialog leaving it to the companion. */
export function escapeTaken(event: Event): boolean {
  return leftToCompanion.get(event)?.() ?? event.defaultPrevented
}

/**
 * #1897 — a Radix dialog's `onInteractOutside`: a press on the shell's Assistant button
 * (raised above an open dialog, `data-modal-companion-toggle`) opens the assistant beside
 * the dialog rather than closing it.
 */
export function keepOpenOnCompanionToggle(event: { target: EventTarget | null; preventDefault(): void }) {
  const target = event.target
  if (target instanceof Element && target.closest('[data-modal-companion-toggle]')) event.preventDefault()
}
