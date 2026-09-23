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
 * Downloads have to survive the sandbox: an anchor with `download` on a blob URL
 * works, and when embedded it needs `target=_blank` so the popup escapes the frame
 * (`allow-popups-to-escape-sandbox`).
 */
export function triggerDownload(url: string, filename: string, embedded = isEmbedded()): void {
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = filename
  anchor.rel = 'noopener'
  if (embedded) {
    anchor.target = '_blank'
  }
  document.body.appendChild(anchor)
  anchor.click()
  anchor.remove()
}

/** Opens a Bambuddy deep link, escaping the sandbox when embedded. */
export function openExternal(url: string, embedded = isEmbedded()): void {
  window.open(url, embedded ? '_blank' : '_self', 'noopener')
}
