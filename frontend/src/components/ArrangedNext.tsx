import { api } from '../api/client'
import type { Output } from '../api/types'
import { isEmbedded } from '../lib/embed'
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
      {/* Bambuddy's iframe sandbox has no allow-downloads: there the file opens in a tab
          that escapes it, as History's file links do (lib/embed.ts). */}
      <a
        className="text-[13px] text-accent underline"
        href={api.downloadUrl(output.id)}
        download
        rel="noopener"
        target={isEmbedded() ? '_blank' : undefined}
      >
        Download
      </a>
    </div>
  )
}
