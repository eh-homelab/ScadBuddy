import { api } from '../api/client'
import type { Output } from '../api/types'
import { ArrangedPlates } from './ArrangedPlates'
import { BlobDownload } from './BlobDownload'
import { Button } from '@/components/ui/button'

/**
 * #2287 — what follows an Arrange, where it ends: its result is saved, and nothing of it
 * has gone to Bambuddy yet. Print opens the print dialog with it; Download saves its 3MF.
 * #2282 — the result itself comes first: its plates, so the layout is seen before it prints.
 */
export function ArrangedNext({ output, sent, onPrint }: { output: Output; sent: boolean; onPrint: () => void }) {
  return (
    <>
      <ArrangedPlates outputId={output.id} />
      <div role="group" aria-label="Next step" className="mt-1.5 flex flex-wrap items-center gap-2">
        {!sent && <span className="text-[12px] text-muted">Not sent to Bambuddy yet: nothing is queued or in its library.</span>}
        <Button type="button" size="sm" onClick={onPrint}>
          Print
        </Button>
        <BlobDownload
          url={api.downloadUrl(output.id)}
          filename={`${output.slug}-${output.id}.3mf`}
          className="text-[13px] font-medium text-primary underline-offset-4 hover:underline"
        >
          Download
        </BlobDownload>
      </div>
    </>
  )
}
