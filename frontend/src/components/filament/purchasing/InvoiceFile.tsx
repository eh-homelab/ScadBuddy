import { DownloadIcon, ExternalLinkIcon, FileUpIcon } from 'lucide-react'
import { useRef, useState } from 'react'
import { ApiError } from '../../../api/client'
import { getInvoiceFileBlob, invoiceFileUrl, uploadInvoiceFile, type FileRef } from '../../../api/filamentPurchasing'
import { Button } from '@/components/ui/button'
import { AppLink } from '../../AppLink'
import { acceptableInvoiceFile } from './invoiceLines'
import { downloadBlob, NEW_TAB } from '../../../lib/embed'

/**
 * #6266 — an invoice's file beside its lines: a PDF in an `<object>`, a photo in an `<img>`,
 * "Open" in a new tab and "Download" through `downloadBlob` (the app can run in a sandboxed
 * frame, which drops a plain download). The file can be added or replaced on a confirmed
 * invoice too. `onChanged` runs after an upload.
 */
export function InvoiceFile({ invoiceId, file, onChanged }: { invoiceId: string; file: FileRef | null | undefined; onChanged: () => void }) {
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const picker = useRef<HTMLInputElement>(null)
  const url = invoiceFileUrl(invoiceId)
  const isImage = file?.media_type.startsWith('image/') ?? false

  async function upload(chosen: File | undefined) {
    if (!chosen) return
    if (!acceptableInvoiceFile(chosen)) {
      setError('Choose a PDF or a photo.')
      return
    }
    setBusy(true)
    setError(null)
    try {
      await uploadInvoiceFile(invoiceId, chosen)
      onChanged()
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.detail : 'The file could not be uploaded. Try again.')
    } finally {
      setBusy(false)
    }
  }

  async function download() {
    setError(null)
    try {
      await downloadBlob(() => getInvoiceFileBlob(invoiceId), file?.name ?? 'invoice')
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'The file could not be downloaded. Try again.')
    }
  }

  return (
    <div className="flex flex-col gap-2">
      <div className="flex min-h-72 items-stretch overflow-hidden rounded-md border bg-muted/30 lg:min-h-[32rem]">
        {file ? (
          isImage ? (
            <img key={`${file.name}-${file.size}`} src={url} alt={`Photo of the invoice, ${file.name}`} className="max-h-[75vh] w-full object-contain" />
          ) : (
            <object key={`${file.name}-${file.size}`} data={url} type={file.media_type} title="Invoice file" className="min-h-72 w-full lg:min-h-[32rem]">
              <p className="p-4 text-[13px] text-muted-foreground">This browser cannot show the file here. Use Open or Download.</p>
            </object>
          )
        ) : (
          <div className="flex w-full flex-col items-center justify-center gap-2 p-6 text-center text-[13px] text-muted-foreground">
            <FileUpIcon aria-hidden className="size-6" />
            No file yet. Add the PDF or the photo of the invoice to read it beside the lines.
          </div>
        )}
      </div>
      <div className="flex flex-wrap items-center gap-2">
        {file && (
          <>
            <Button asChild variant="outline" size="sm">
              <AppLink href={url} {...NEW_TAB}>
                <ExternalLinkIcon aria-hidden />
                Open
              </AppLink>
            </Button>
            <Button type="button" variant="outline" size="sm" onClick={() => void download()}>
              <DownloadIcon aria-hidden />
              Download
            </Button>
          </>
        )}
        <Button type="button" variant="ghost" size="sm" disabled={busy} onClick={() => picker.current?.click()}>
          {file ? 'Replace file' : 'Add file'}
        </Button>
        <input
          ref={picker}
          type="file"
          accept="application/pdf,image/*"
          aria-label="Choose an invoice file"
          className="sr-only"
          tabIndex={-1}
          onChange={(event) => {
            void upload(event.target.files?.[0])
            event.target.value = ''
          }}
        />
        {file && <span className="text-[12px] text-muted-foreground">{file.name}</span>}
      </div>
      {error && (
        <p role="alert" className="text-[13px] text-warn">
          {error}
        </p>
      )}
    </div>
  )
}
