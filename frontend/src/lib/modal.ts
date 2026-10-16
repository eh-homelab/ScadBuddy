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

/** The topmost open dialog: one opened from inside another comes later in the document. */
export function topmostDialog(): HTMLElement | null {
  const dialogs = document.querySelectorAll<HTMLElement>('[role="dialog"]')
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

/**
 * #1897 — a Radix dialog's `onInteractOutside`: a press on the shell's Assistant button
 * (raised above an open dialog, `data-modal-companion-toggle`) opens the assistant beside
 * the dialog rather than closing it. ui/sheet and ui/dialog content call it for every
 * dialog, but that alone leaves the panel unusable: a dialog that stays modal traps focus
 * and hides the panel from assistive tech. A dialog meant to sit beside the assistant also
 * takes `ModalCompanionContext` and `roomFor`, sets `modal={!beside}`, and leaves the
 * companion's focus and Escape alone (`onOpenAutoFocus`, `onEscapeKeyDown`), as
 * components/activity/ActivityDrawer does.
 */
export function keepOpenOnCompanionToggle(event: { target: EventTarget | null; preventDefault(): void }) {
  const target = event.target
  if (target instanceof Element && target.closest('[data-modal-companion-toggle]')) event.preventDefault()
}
