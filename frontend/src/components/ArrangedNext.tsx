import { api } from '../api/client'
import type { Output } from '../api/types'
import { BlobDownload } from './BlobDownload'
import { Button } from './ui/Button'

/**
 * #2287 — what follows an Arrange, where it ends: its result is saved, and nothing of it
 * has gone to Bambuddy yet. Print opens the print dialog with it; Download saves its 3MF.
 */
export function ArrangedNext({ output, sent, onPrint }: { output: Output; sent: boolean; onPrint: () => void }) {
  return (
    <div role="group" aria-label="Next step" className="mt-1.5 flex flex-wrap items-center gap-2">
      {!sent && <span className="text-[12px] text-muted">Not sent to Bambuddy yet: nothing is queued or in its library.</span>}
      <Button size="sm" variant="primary" onClick={onPrint}>
        Print
      </Button>
      <BlobDownload
        url={api.downloadUrl(output.id)}
        filename={`${output.slug}-${output.id}.3mf`}
        className="text-[13px] text-accent underline"
      >
        Download
      </BlobDownload>
    </div>
  )
}
