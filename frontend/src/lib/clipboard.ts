/**
 * Copies `text` to the clipboard, from a click handler.
 *
 * The async Clipboard API is tried first. Inside Bambuddy's iframe (CLAUDE.md,
 * "Bambuddy iframe facts") a cross-origin frame needs `allow="clipboard-write"`
 * for it, which Bambuddy is not known to set, so `writeText` may reject. The
 * fallback selects `node`'s text and runs `document.execCommand('copy')`, which
 * works from a user gesture without that permission. If both fail, `node`'s
 * text is left selected so the user can press Ctrl+C / Cmd+C.
 *
 * Returns whether the text reached the clipboard.
 */
export async function copyText(text: string, node?: HTMLElement | null): Promise<boolean> {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text)
      return true
    }
  } catch {
    // Refused by the permissions policy or the browser; fall through.
  }
  if (!node) return false
  selectContents(node)
  try {
    return typeof document.execCommand === 'function' && document.execCommand('copy')
  } catch {
    return false
  }
}

/** Selects all of `node`'s text, as a user dragging across it would. */
export function selectContents(node: HTMLElement): void {
  const selection = window.getSelection()
  if (!selection) return
  const range = document.createRange()
  range.selectNodeContents(node)
  selection.removeAllRanges()
  selection.addRange(range)
}
