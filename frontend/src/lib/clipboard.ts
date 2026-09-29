import { isEmbedded } from './embed'

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

/** What `copyImage` did: copied, or why not, in words for the person who asked. */
export type CopyImageResult = { ok: true } | { ok: false; message: string }

export const COPY_REFUSED = 'The browser would not put the image on the clipboard. Save it instead.'
export const COPY_DENIED =
  'Clipboard access is denied for this site. Allow the clipboard for it in the browser’s site settings, then try again, or use Save PNG.'
export const COPY_FRAMED =
  'Bambuddy’s frame blocks clipboard access, so the image cannot be copied here. Use Save PNG instead.'
export const COPY_UNDRAWN = 'The viewer could not draw the image.'

/**
 * Puts the PNG `image` resolves to on the clipboard, from a click handler (#722).
 *
 * The write starts at once, before anything is awaited, so it stays inside the user
 * gesture: that is what lets the browser ask for clipboard access while the permission
 * is still `prompt`. The `ClipboardItem` takes the promise rather than the blob, which
 * Safari needs to keep the gesture while the image is drawn.
 *
 * When the write is refused, it is retried once if Chromium refused only because the
 * document had lost focus, and otherwise the reason is worked out for the message: a
 * cross-origin frame (Bambuddy, which is not known to grant `allow="clipboard-write"`),
 * a site permission the person denied, or a refusal with no known cause. The
 * permission is read only after the write, never before it: awaiting it first would
 * spend the gesture.
 */
export async function copyImage(
  image: Promise<Blob>,
  crossOriginFrame = isCrossOriginFrame(),
): Promise<CopyImageResult> {
  // Settled either way, so a failed drawing is never an unhandled rejection.
  const drawn = image.then(
    () => true,
    () => false,
  )
  const write = () => navigator.clipboard.write([new ClipboardItem({ 'image/png': image })])
  try {
    await write()
    return { ok: true }
  } catch (refusal) {
    if (!(await drawn)) return { ok: false, message: COPY_UNDRAWN }
    if (lostFocus(refusal)) {
      window.focus()
      try {
        await write()
        return { ok: true }
      } catch {
        // Refused again; explained below.
      }
    }
  }
  if (crossOriginFrame) return { ok: false, message: COPY_FRAMED }
  if ((await clipboardWriteState()) === 'denied') return { ok: false, message: COPY_DENIED }
  return { ok: false, message: COPY_REFUSED }
}

/** Chromium's refusal of a write from a document without focus. */
function lostFocus(refusal: unknown): boolean {
  if (!(refusal instanceof DOMException) || refusal.name !== 'NotAllowedError') return false
  return !document.hasFocus() || /focus/i.test(refusal.message)
}

/**
 * The `clipboard-write` permission's state, or undefined where it cannot be read:
 * no Permissions API, or a browser (Firefox, Safari) that throws on that name.
 */
export async function clipboardWriteState(): Promise<PermissionState | undefined> {
  try {
    const status = await navigator.permissions?.query({
      name: 'clipboard-write' as PermissionName,
    })
    return status?.state
  } catch {
    return undefined
  }
}

/** Whether this page is framed by a page of another origin, as in Bambuddy. */
export function isCrossOriginFrame(): boolean {
  if (!isEmbedded()) return false
  try {
    return window.top?.location.origin !== window.location.origin
  } catch {
    // Reading another origin's location throws.
    return true
  }
}
