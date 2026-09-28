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

/**
 * Downloads have to survive the sandbox. Bambuddy's frame has no `allow-downloads`, so
 * Chromium silently drops a download started inside it, `target=_blank` or not
 * (measured in the #311 e2e). What it does allow is a popup that escapes the sandbox
 * (`allow-popups-to-escape-sandbox`): a blank one is same-origin with the frame
 * (`allow-same-origin`), so it can take the frame's blob URL, and a download anchor
 * clicked in it is not sandboxed. Outside a frame, a plain download anchor.
 */
export function triggerDownload(url: string, filename: string, embedded = isEmbedded()): void {
  const popup = embedded ? openDownloadWindow() : null
  clickDownload(url, filename, popup, embedded)
}

/**
 * `load` the file and save it as `filename`, through the escaped popup when embedded.
 * The popup is opened before `load` is awaited, while the click still allows one: a
 * large file can take longer to fetch than the browser keeps that permission.
 */
export async function downloadBlob(
  load: () => Promise<Blob>,
  filename: string,
  embedded = isEmbedded(),
): Promise<void> {
  const popup = embedded ? openDownloadWindow() : null
  try {
    const url = URL.createObjectURL(await load())
    clickDownload(url, filename, popup, embedded)
    // The anchor has been clicked; give the browser time to read the blob.
    setTimeout(() => URL.revokeObjectURL(url), 60_000)
  } catch (cause) {
    popup?.close()
    throw cause
  }
}

function openDownloadWindow(): Window | null {
  try {
    return window.open('', '_blank')
  } catch {
    return null
  }
}

function clickDownload(url: string, filename: string, popup: Window | null, embedded: boolean) {
  const host = popup?.document ?? document
  const anchor = host.createElement('a')
  anchor.href = url
  anchor.download = filename
  anchor.rel = 'noopener'
  // No popup (blocked): the frame's own anchor, which is all a browser that lets
  // sandboxed downloads through needs.
  if (embedded && !popup) anchor.target = '_blank'
  host.body.appendChild(anchor)
  anchor.click()
  anchor.remove()
  // The download has started; the blank window has nothing else to show.
  if (popup) setTimeout(() => popup.close(), 1000)
}

/** Opens a Bambuddy deep link, escaping the sandbox when embedded. */
export function openExternal(url: string, embedded = isEmbedded()): void {
  window.open(url, embedded ? '_blank' : '_self', 'noopener')
}
