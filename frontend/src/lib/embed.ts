/**
 * Bambuddy renders External Links with `open_in_new_tab=false` inside a sandboxed
 * iframe at `/external/{id}` (spec §1). `allow-same-origin` is granted, so the
 * frame check never throws; it is kept in a try/catch anyway because a stricter
 * sandbox would make `window.top` a cross-origin access.
 */
export function isEmbedded(): boolean {
  try {
    return window.self !== window.top
  } catch {
    return true
  }
}

/** The escape popup was blocked inside the frame, so the file cannot be saved (#612). */
export class DownloadBlockedError extends Error {
  constructor() {
    super('Allow pop-ups for this page to download from inside Bambuddy.')
    this.name = 'DownloadBlockedError'
  }
}

/** The escape popup was closed before the file had loaded, so there is nowhere to save it. */
export class DownloadWindowClosedError extends DownloadBlockedError {
  constructor() {
    super()
    this.message = 'The download window was closed before the file was ready. Try again.'
    this.name = 'DownloadWindowClosedError'
  }
}

/**
 * `load` the file and save it as `filename`. Downloads have to survive the sandbox:
 * Bambuddy's frame has no `allow-downloads`, so Chromium silently drops a download
 * started inside it, `target=_blank` or not (#612). What it does allow is a popup that
 * escapes the sandbox (`allow-popups-to-escape-sandbox`): a blank one is same-origin
 * with the frame (`allow-same-origin`), so it can take the frame's blob URL, and a
 * download anchor clicked in it is not sandboxed. Outside a frame, a plain anchor.
 *
 * The popup is opened before `load` is awaited, while the click still allows one: a
 * large file can take longer to fetch than the browser keeps that permission. If it is
 * blocked, this rejects with `DownloadBlockedError`, and if it is closed before the file
 * has loaded, with `DownloadWindowClosedError`, rather than fall back to the frame's own
 * anchor, which would fail without a word.
 */
export async function downloadBlob(
  load: () => Promise<Blob>,
  filename: string,
  embedded = isEmbedded(),
): Promise<void> {
  const popup = embedded ? openDownloadWindow() : null
  // Blocked: no window, or (as some blockers do) one that is already closed.
  if (embedded && (!popup || popup.closed)) throw new DownloadBlockedError()
  let url: string | null = null
  try {
    const blob = await load()
    // Closed while the file loaded (a blank tab is easily closed): its `document` is
    // null now, and the frame's own anchor would be dropped silently (#612).
    if (popup && (popup.closed || !popup.document)) throw new DownloadWindowClosedError()
    url = URL.createObjectURL(blob)
    const host = popup ? popup.document : document
    const anchor = host.createElement('a')
    anchor.href = url
    anchor.download = filename
    anchor.rel = 'noopener'
    host.body.appendChild(anchor)
    anchor.click()
    anchor.remove()
    // The anchor has been clicked; give the browser time to read the blob.
    const clicked = url
    setTimeout(() => URL.revokeObjectURL(clicked), 60_000)
    url = null
    // The download has started; the blank window has nothing else to show.
    if (popup) setTimeout(() => popup.close(), 1000)
  } catch (cause) {
    popup?.close()
    throw cause
  } finally {
    // Anything that failed after the URL was made: nothing will read it.
    if (url !== null) URL.revokeObjectURL(url)
  }
}

function openDownloadWindow(): Window | null {
  try {
    return window.open('', '_blank')
  } catch {
    return null
  }
}

/**
 * An anchor's props for a page that is not ScadBuddy's, such as a cited source (#284).
 * A same-frame navigation would replace the app, embedded or not; `_blank` escapes the
 * sandbox (`allow-popups-to-escape-sandbox`, CLAUDE.md "Bambuddy iframe facts").
 */
export const NEW_TAB = { target: '_blank', rel: 'noopener noreferrer' } as const

/** Opens a Bambuddy deep link, escaping the sandbox when embedded. */
export function openExternal(url: string, embedded = isEmbedded()): void {
  window.open(url, embedded ? '_blank' : '_self', 'noopener')
}
