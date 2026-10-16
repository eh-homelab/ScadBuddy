import { useState, type ReactNode } from 'react'
import { DownloadBlockedError, downloadBlob } from '../lib/embed'
import { Button } from '@/components/ui/button'
import { cn } from '@/lib/utils'
import { Spinner } from './ui/Spinner'

/**
 * The name a `Content-Disposition` gives the file: `filename*` (RFC 5987) before
 * `filename`. None when there is none.
 */
function sentName(header: string | null): string | null {
  if (!header) return null
  const encoded = /filename\*\s*=\s*utf-8''([^;]+)/i.exec(header)?.[1]
  if (encoded) {
    try {
      return decodeURIComponent(encoded.trim())
    } catch {
      // A malformed escape: the plain `filename`, if there is one, still names it.
    }
  }
  const plain = /filename\s*=\s*(?:"([^"]*)"|([^;]+))/i.exec(header)
  return (plain?.[1] ?? plain?.[2]?.trim()) || null
}

/**
 * A download through lib/embed's `downloadBlob` (#612): a plain `<a download>` started
 * inside Bambuddy's sandboxed frame, which has no `allow-downloads`, is dropped by
 * Chromium without a word, `target=_blank` or not. A blocked or closed popup is said
 * beside it. Saved under the name the server's `Content-Disposition` gives, so a file
 * downloaded here is named as every other download of it is; `filename` when it gives
 * none.
 */
export function BlobDownload({
  url,
  filename,
  children,
  className,
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
      let name = filename
      await downloadBlob(
        async () => {
          const response = await fetch(url)
          if (!response.ok) throw new Error(`HTTP ${response.status}`)
          name = sentName(response.headers.get('content-disposition')) ?? filename
          return await response.blob()
        },
        () => name,
      )
    } catch (cause) {
      // DownloadWindowClosedError is a DownloadBlockedError, with its own message.
      setError(cause instanceof DownloadBlockedError ? cause.message : 'Download failed.')
    } finally {
      setBusy(false)
    }
  }

  return (
    <>
      <Button
        variant="link"
        className={cn('h-auto px-0 py-0 text-[length:inherit] underline', className)}
        onClick={() => void download()}
        disabled={busy}
        aria-busy={busy}
      >
        {busy && <Spinner />}
        {children}
      </Button>
      {error && (
        <span role="alert" className="text-[12px] text-destructive">
          {error}
        </span>
      )}
    </>
  )
}
