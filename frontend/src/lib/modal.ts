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
