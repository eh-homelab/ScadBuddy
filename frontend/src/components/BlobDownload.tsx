import { useState, type ReactNode } from 'react'
import { DownloadBlockedError, downloadBlob } from '../lib/embed'

/**
 * A download through lib/embed's `downloadBlob` (#612): a plain `<a download>` started
 * inside Bambuddy's sandboxed frame, which has no `allow-downloads`, is dropped by
 * Chromium without a word, `target=_blank` or not. A blocked or closed popup is said
 * beside it.
 */
export function BlobDownload({
  url,
  filename,
  children,
  className = 'text-accent underline',
}: {
  url: string
  filename: string
  children: ReactNode
  className?: string
}) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function download() {
    setBusy(true)
    setError(null)
    try {
      await downloadBlob(async () => {
        const response = await fetch(url)
        if (!response.ok) throw new Error(`HTTP ${response.status}`)
        return await response.blob()
      }, filename)
    } catch (cause) {
      // DownloadWindowClosedError is a DownloadBlockedError, with its own message.
      setError(cause instanceof DownloadBlockedError ? cause.message : 'Download failed.')
    } finally {
      setBusy(false)
    }
  }

  return (
    <>
      <button type="button" className={className} onClick={() => void download()} disabled={busy} aria-busy={busy}>
        {children}
      </button>
      {error && (
        <span role="alert" className="text-[12px] text-warn">
          {error}
        </span>
      )}
    </>
  )
}
